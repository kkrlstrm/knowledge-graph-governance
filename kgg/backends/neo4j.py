"""Neo4j backend — the "real graph" adapter.

Nodes carry a shared `:KggNode` label plus `scope` / `kind` / `key` properties;
a composite uniqueness constraint on those three enforces identity in the graph
itself. `create` uses `CREATE` (not `MERGE`), so it fails closed against the
constraint if the node already exists. An apply runs inside one explicit
transaction. Supersession snapshots the prior belief into `:KggRevision` linked
by `[:SUPERSEDES]`; references become `[:REL]` edges carrying the field name.

Requires the optional extra:  pip install -e ".[neo4j]"
Auth from env: NEO4J_USER (default 'neo4j'), NEO4J_PASSWORD.
"""
from __future__ import annotations

import os
from datetime import date

from ..model import Schema, ExistingNode
from .base import Backend

_SCALAR = (str, int, float, bool)
_CONSTRAINT = ("CREATE CONSTRAINT kgg_node_identity IF NOT EXISTS "
               "FOR (n:KggNode) REQUIRE (n.scope, n.kind, n.key) IS UNIQUE")


def _clean(props: dict) -> dict:
    out = {}
    for k, v in props.items():
        if k in ("supersede",):
            continue
        if isinstance(v, _SCALAR) or v is None:
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, _SCALAR) for x in v):
            out[k] = v
    return out


class Neo4jBackend(Backend):
    def __init__(self, uri: str):
        from neo4j import GraphDatabase
        auth = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD"))
        self.driver = GraphDatabase.driver(uri, auth=auth, notifications_min_severity="OFF")
        self.driver.verify_connectivity()
        self._session = None
        self._tx = None
        try:
            self.driver.execute_query(_CONSTRAINT)
        except Exception as e:  # noqa: BLE001 - older servers lack composite constraints
            print(f"  (kgg) note: could not create uniqueness constraint: {str(e)[:80]}")

    # --- transactions ---
    def begin(self) -> None:
        self._session = self.driver.session()
        self._tx = self._session.begin_transaction()

    def commit(self) -> None:
        self._tx.commit(); self._session.close()
        self._tx = self._session = None

    def rollback(self) -> None:
        if self._tx is not None:
            self._tx.rollback(); self._session.close()
            self._tx = self._session = None

    def _run(self, query, **params):
        if self._tx is not None:
            return list(self._tx.run(query, **params))
        r, _, _ = self.driver.execute_query(query, **params)
        return r

    # --- reads ---
    def read_existing(self, schema: Schema, scope: str) -> dict:
        q = "MATCH (n:KggNode) "
        params = {}
        if schema.scoped:
            q += "WHERE n.scope = $scope "
            params["scope"] = scope
        q += ("RETURN n.kind AS kind, n.key AS key, n.is_protected AS is_protected, "
              "n.owner AS owner, n.status AS status, n.revision AS revision, "
              "n.scope AS scope, properties(n) AS props")
        out = {}
        for row in self._run(q, **params):
            nk = schema.kind(row["kind"])
            props = row["props"] or {}
            content = {f: props.get(f) for f in (nk.content_fields if nk else [])}
            out[(row["kind"], row["key"])] = ExistingNode(
                kind=row["kind"], key=row["key"], content=content,
                is_protected=bool(row["is_protected"]), owner=row["owner"] or "",
                status=row["status"] or "active", revision=row["revision"] or 1,
                scope=row["scope"] or "*")
        return out

    # --- writes ---
    def create(self, kind, key, props, prov, status, identity_scope) -> None:
        # CREATE (not MERGE) -> fails closed against the uniqueness constraint.
        self._run(
            "CREATE (n:KggNode {scope:$scope, kind:$kind, key:$key}) "
            "SET n += $props, n.status=$status, n.revision=1, n.valid_from=date($today)",
            scope=identity_scope, kind=kind.name, key=key, status=status,
            today=date.today().isoformat(), props=_clean({**props, **prov}))

    def supersede(self, kind, key, props, prov, run_id, identity_scope) -> int:
        r = self._run(
            "MATCH (n:KggNode {scope:$scope, kind:$kind, key:$key}) "
            "CREATE (rev:KggRevision) SET rev = properties(n), "
            "  rev.valid_until=date($today), rev.superseded_by_run=$run_id "
            "CREATE (n)-[:SUPERSEDES]->(rev) "
            "SET n += $props, n.status='active', n.revision=coalesce(n.revision,1)+1, "
            "  n.valid_from=date($today) RETURN n.revision AS revision",
            scope=identity_scope, kind=kind.name, key=key, run_id=run_id,
            today=date.today().isoformat(), props=_clean({**props, **prov}))
        return r[0]["revision"] if r else 1

    def link(self, src_kind, src_key, field, dst_kind, dst_key, identity_scope) -> None:
        self._run(
            "MATCH (a:KggNode {scope:$s, kind:$sk, key:$skey}) "
            "MATCH (b:KggNode {scope:$s, kind:$dk, key:$dkey}) "
            "MERGE (a)-[:REL {field:$field}]->(b)",
            s=identity_scope, sk=src_kind, skey=src_key, dk=dst_kind, dkey=dst_key, field=field)

    def close(self) -> None:
        self.rollback()
        self.driver.close()
