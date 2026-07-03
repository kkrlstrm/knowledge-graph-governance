"""Zero-infra SQLite backend — the quickstart store.

Everything (nodes, revision history, edges) lives in one file with no server
to run. Provenance is stored as real columns; supersession snapshots the prior
belief into `node_revisions` so history stays addressable, exactly like the
Neo4j backend. Uses only the Python standard library.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from ..model import Schema, NodeKind, ExistingNode
from .base import Backend

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    kind TEXT NOT NULL,
    key  TEXT NOT NULL,
    props TEXT NOT NULL,
    is_protected INTEGER DEFAULT 0,
    owner TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    revision INTEGER DEFAULT 1,
    ingest_run_id TEXT DEFAULT '',
    provenance_ts TEXT DEFAULT '',
    valid_from TEXT DEFAULT '',
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS node_revisions (
    kind TEXT NOT NULL,
    key  TEXT NOT NULL,
    props TEXT NOT NULL,
    revision INTEGER NOT NULL,
    owner TEXT DEFAULT '',
    valid_until TEXT DEFAULT '',
    superseded_by_run TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS edges (
    src_kind TEXT NOT NULL, src_key TEXT NOT NULL,
    field TEXT NOT NULL,
    dst_kind TEXT NOT NULL, dst_key TEXT NOT NULL,
    UNIQUE (src_kind, src_key, field, dst_kind, dst_key)
);
"""


class SqliteBackend(Backend):
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def read_existing(self, schema: Schema) -> dict:
        out = {}
        for row in self.conn.execute(
            "SELECT kind, key, props, is_protected, owner, status, revision FROM nodes"
        ):
            props = json.loads(row["props"])
            nk = schema.kind(row["kind"])
            content = {f: props.get(f) for f in (nk.content_fields if nk else [])}
            out[(row["kind"], row["key"])] = ExistingNode(
                kind=row["kind"], key=row["key"], content=content,
                is_protected=bool(row["is_protected"]), owner=row["owner"] or "",
                status=row["status"] or "active", revision=row["revision"] or 1,
            )
        return out

    def create(self, kind: NodeKind, key: str, props: dict, prov: dict, status: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes "
            "(kind, key, props, is_protected, owner, status, revision, ingest_run_id, provenance_ts, valid_from) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kind.name, key, json.dumps(props, default=str),
             int(bool(prov.get("is_protected"))), prov.get("owner", ""), status, 1,
             prov.get("ingest_run_id", ""), prov.get("provenance_ts", ""), date.today().isoformat()),
        )
        self.conn.commit()

    def supersede(self, kind: NodeKind, key: str, props: dict, prov: dict, run_id: str) -> int:
        cur = self.conn.execute(
            "SELECT props, revision, owner FROM nodes WHERE kind=? AND key=?", (kind.name, key)
        ).fetchone()
        old_rev = cur["revision"] if cur else 1
        if cur:
            self.conn.execute(
                "INSERT INTO node_revisions (kind, key, props, revision, owner, valid_until, superseded_by_run) "
                "VALUES (?,?,?,?,?,?,?)",
                (kind.name, key, cur["props"], old_rev, cur["owner"] or "",
                 date.today().isoformat(), run_id),
            )
        new_rev = old_rev + 1
        self.conn.execute(
            "UPDATE nodes SET props=?, is_protected=?, owner=?, status='active', revision=?, "
            "ingest_run_id=?, provenance_ts=?, valid_from=? WHERE kind=? AND key=?",
            (json.dumps(props, default=str), int(bool(prov.get("is_protected"))),
             prov.get("owner", ""), new_rev, prov.get("ingest_run_id", ""),
             prov.get("provenance_ts", ""), date.today().isoformat(), kind.name, key),
        )
        self.conn.commit()
        return new_rev

    def link(self, src_kind: str, src_key: str, field: str, dst_kind: str, dst_key: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO edges (src_kind, src_key, field, dst_kind, dst_key) VALUES (?,?,?,?,?)",
            (src_kind, src_key, field, dst_kind, dst_key),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
