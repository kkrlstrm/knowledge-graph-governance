"""Neo4j backend — the "real graph" adapter.

Nodes carry a shared `:KggNode` label plus a `kind` property (labels can't be
parametrized in Cypher without APOC, and a shared label keeps reads uniform).
Supersession snapshots the prior belief into a `:KggRevision` linked by
`[:SUPERSEDES]`, mirroring the SQLite backend. References become `[:REL]` edges
carrying the source field name.

Requires the optional `neo4j` extra:  pip install knowledge-graph-governance[neo4j]
Auth from env: NEO4J_USER (default 'neo4j'), NEO4J_PASSWORD.
"""
from __future__ import annotations

import os
from datetime import date

from ..model import Schema, NodeKind, ExistingNode
from .base import Backend

_SCALAR = (str, int, float, bool)


def _clean(props: dict) -> dict:
    """Keep only Neo4j-storable values (scalars + lists of scalars)."""
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

    def read_existing(self, schema: Schema) -> dict:
        r, _, _ = self.driver.execute_query(
            "MATCH (n:KggNode) RETURN n.kind AS kind, n.key AS key, "
            "n.is_protected AS is_protected, n.owner AS owner, n.status AS status, "
            "n.revision AS revision, properties(n) AS props"
        )
        out = {}
        for row in r:
            nk = schema.kind(row["kind"])
            props = row["props"] or {}
            content = {f: props.get(f) for f in (nk.content_fields if nk else [])}
            out[(row["kind"], row["key"])] = ExistingNode(
                kind=row["kind"], key=row["key"], content=content,
                is_protected=bool(row["is_protected"]), owner=row["owner"] or "",
                status=row["status"] or "active", revision=row["revision"] or 1,
            )
        return out

    def create(self, kind: NodeKind, key: str, props: dict, prov: dict, status: str) -> None:
        self.driver.execute_query(
            "MERGE (n:KggNode {kind:$kind, key:$key}) "
            "SET n += $props, n.status=$status, n.revision=coalesce(n.revision,1), "
            "n.valid_from=date($today)",
            kind=kind.name, key=key, status=status, today=date.today().isoformat(),
            props={**_clean(props), **_clean(prov), "kind": kind.name, "key": key},
        )

    def supersede(self, kind: NodeKind, key: str, props: dict, prov: dict, run_id: str) -> int:
        r, _, _ = self.driver.execute_query(
            "MATCH (n:KggNode {kind:$kind, key:$key}) "
            "CREATE (rev:KggRevision) SET rev = properties(n), "
            "  rev.valid_until=date($today), rev.superseded_by_run=$run_id "
            "CREATE (n)-[:SUPERSEDES]->(rev) "
            "SET n += $props, n.status='active', n.revision=coalesce(n.revision,1)+1, "
            "  n.valid_from=date($today) "
            "RETURN n.revision AS revision",
            kind=kind.name, key=key, run_id=run_id, today=date.today().isoformat(),
            props={**_clean(props), **_clean(prov), "kind": kind.name, "key": key},
        )
        return r[0]["revision"] if r else 1

    def link(self, src_kind: str, src_key: str, field: str, dst_kind: str, dst_key: str) -> None:
        self.driver.execute_query(
            "MATCH (a:KggNode {kind:$sk, key:$skey}) "
            "MATCH (b:KggNode {kind:$dk, key:$dkey}) "
            "MERGE (a)-[r:REL {field:$field}]->(b)",
            sk=src_kind, skey=src_key, dk=dst_kind, dkey=dst_key, field=field,
        )

    def close(self) -> None:
        self.driver.close()
