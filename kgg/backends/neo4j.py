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

import json
import os
from datetime import date, datetime, timezone

from ..model import Schema, NodeKind, ExistingNode
from .base import Backend

_SCALAR = (str, int, float, bool)
_CONSTRAINTS = (
    ("CREATE CONSTRAINT kgg_node_identity IF NOT EXISTS "
     "FOR (n:KggNode) REQUIRE (n.scope, n.kind, n.key) IS UNIQUE"),
    ("CREATE CONSTRAINT kgg_contradiction_pair IF NOT EXISTS "
     "FOR (c:KggContradiction) REQUIRE c.pair_key IS UNIQUE"),
    ("CREATE CONSTRAINT kgg_gap_resolution IF NOT EXISTS "
     "FOR (g:KggGapResolution) REQUIRE g.slug IS UNIQUE"),
    ("CREATE CONSTRAINT kgg_gap_observation IF NOT EXISTS "
     "FOR (o:KggGapObservation) REQUIRE o.obs_key IS UNIQUE"),
)

_CONTRADICTION_FIELDS = (
    "left_id", "right_id", "slot", "detector", "reason",
    "left_snapshot", "right_snapshot", "left_owner", "right_owner",
    "left_source", "right_source", "left_run_id", "right_run_id",
    "state", "detected_run_id", "detected_ts", "resolved_ts", "resolution")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slot(kind: NodeKind, props: dict) -> str:
    from ..checks import slot_value
    return slot_value(kind, props) if getattr(kind, "claim_slot", None) else ""


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
        for stmt in _CONSTRAINTS:
            try:
                self.driver.execute_query(stmt)
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
                scope=row["scope"] or "*", slot=props.get("slot", "") or "")
        return out

    def _node_row(self, row) -> dict:
        props = dict(row["props"] or {})
        return {
            "kind": row["kind"], "key": row["key"], "scope": row.get("scope") or "*",
            "props": props, "owner": props.get("owner", "") or "",
            "status": props.get("status", "active") or "active",
            "revision": props.get("revision", 1) or 1,
            "is_protected": bool(props.get("is_protected")),
            "run_id": props.get("ingest_run_id", "") or "",
            "provenance_ts": props.get("provenance_ts", "") or "",
            "valid_from": str(props.get("valid_from", "") or ""),
            "slot": props.get("slot", "") or "",
            "source": props.get("source", "") or "",
        }

    def read_node(self, kind: str, key: str, identity_scope: str) -> dict | None:
        r = self._run(
            "MATCH (n:KggNode {scope:$s, kind:$k, key:$key}) "
            "RETURN n.kind AS kind, n.key AS key, n.scope AS scope, properties(n) AS props",
            s=identity_scope, k=kind, key=key)
        return self._node_row(r[0]) if r else None

    def read_revisions(self, kind: str, key: str, identity_scope: str,
                       limit: int = 20) -> list[dict]:
        r = self._run(
            "MATCH (:KggNode {scope:$s, kind:$k, key:$key})-[:SUPERSEDES]->(rev:KggRevision) "
            "RETURN properties(rev) AS props ORDER BY rev.revision DESC LIMIT $limit",
            s=identity_scope, k=kind, key=key, limit=limit)
        out = []
        for row in r:
            p = dict(row["props"] or {})
            out.append({"revision": p.get("revision", 1), "props": p,
                        "owner": p.get("owner", "") or "",
                        "valid_until": str(p.get("valid_until", "") or ""),
                        "superseded_by_run": p.get("superseded_by_run", "") or "",
                        "run_id": p.get("ingest_run_id", "") or ""})
        return out

    def read_edges(self, kind: str, key: str, identity_scope: str) -> list[dict]:
        r = self._run(
            "MATCH (:KggNode {scope:$s, kind:$k, key:$key})-[e:REL]->(b:KggNode) "
            "RETURN e.field AS field, b.kind AS dst_kind, b.key AS dst_key "
            "ORDER BY field, dst_key",
            s=identity_scope, k=kind, key=key)
        return [{"field": x["field"], "dst_kind": x["dst_kind"], "dst_key": x["dst_key"]}
                for x in r]

    def read_nodes(self, schema: Schema, scope: str) -> list[dict]:
        q = "MATCH (n:KggNode) "
        params = {}
        if schema.scoped:
            q += "WHERE n.scope = $scope "
            params["scope"] = scope
        q += "RETURN n.kind AS kind, n.key AS key, n.scope AS scope, properties(n) AS props"
        return [self._node_row(row) for row in self._run(q, **params)]

    # --- contradiction ledger ---
    def read_contradictions(self, state: str = "active") -> list[dict]:
        q = "MATCH (c:KggContradiction) "
        params = {}
        if state and state != "all":
            q += "WHERE c.state = $state "
            params["state"] = state
        q += "RETURN properties(c) AS p ORDER BY c.detected_ts DESC, c.pair_key"
        return [dict(row["p"] or {}) for row in self._run(q, **params)]

    def write_contradictions(self, upsert, resolve, reactivate) -> dict:
        now = _now()
        items = []
        for obs in list(upsert) + list(reactivate):
            d = obs.to_dict()
            d.update(detected_ts=now, state="active", resolved_ts="", resolution="")
            items.append(d)
        if items:
            self._run(
                "UNWIND $rows AS row "
                "MERGE (c:KggContradiction {pair_key: row.pair_key}) SET c += row",
                rows=items)
        for r in resolve:
            self._run(
                "MATCH (c:KggContradiction {pair_key:$k}) WHERE c.state='active' "
                "SET c.state='resolved', c.resolved_ts=$ts, c.resolution=$why",
                k=r["pair_key"], ts=now, why=r.get("resolution", ""))
        return {"upserted": len(items), "resolved": len(list(resolve)),
                "reactivated": len(list(reactivate))}

    # --- gap registry ---
    def record_gaps(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = [{**r, "obs_key": f"{r['slug']}|{r.get('item_id','')}|{r.get('run_id','')}",
                    "recorded_ts": _now()} for r in rows]
        self._run(
            "UNWIND $rows AS row "
            "MERGE (o:KggGapObservation {obs_key: row.obs_key}) "
            "ON CREATE SET o += row", rows=payload)
        return len(payload)

    def read_gap_rows(self) -> list[dict]:
        return [dict(row["p"] or {}) for row in self._run(
            "MATCH (o:KggGapObservation) RETURN properties(o) AS p ORDER BY o.recorded_ts")]

    def read_gap_resolutions(self) -> dict:
        out = {}
        for row in self._run("MATCH (g:KggGapResolution) RETURN properties(g) AS p"):
            p = dict(row["p"] or {})
            out[p.get("slug", "")] = p
        return out

    def set_gap_resolution(self, slug, status, promoted_to="", note="", actor="") -> None:
        self._run(
            "MERGE (g:KggGapResolution {slug:$slug}) "
            "SET g.status=$status, g.promoted_to=$to, g.note=$note, g.actor=$actor, g.ts=$ts",
            slug=slug, status=status, to=promoted_to, note=note, actor=actor, ts=_now())

    # --- writes ---
    def create(self, kind, key, props, prov, status, identity_scope) -> None:
        # CREATE (not MERGE) -> fails closed against the uniqueness constraint.
        self._run(
            "CREATE (n:KggNode {scope:$scope, kind:$kind, key:$key}) "
            "SET n += $props, n.status=$status, n.revision=1, n.valid_from=date($today), "
            "  n.slot=$slot",
            scope=identity_scope, kind=kind.name, key=key, status=status,
            today=date.today().isoformat(), slot=_slot(kind, props),
            props=_clean({**props, **prov}))

    def supersede(self, kind, key, props, prov, run_id, identity_scope) -> int:
        r = self._run(
            "MATCH (n:KggNode {scope:$scope, kind:$kind, key:$key}) "
            "CREATE (rev:KggRevision) SET rev = properties(n), "
            "  rev.valid_until=date($today), rev.superseded_by_run=$run_id "
            "CREATE (n)-[:SUPERSEDES]->(rev) "
            "SET n += $props, n.status='active', n.revision=coalesce(n.revision,1)+1, "
            "  n.valid_from=date($today), n.slot=$slot RETURN n.revision AS revision",
            scope=identity_scope, kind=kind.name, key=key, run_id=run_id,
            today=date.today().isoformat(), slot=_slot(kind, props),
            props=_clean({**props, **prov}))
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
