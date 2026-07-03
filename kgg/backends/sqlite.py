"""Zero-infra SQLite backend — the quickstart store.

Everything (nodes, revision history, edges) lives in one file with no server to
run. Identity is (scope, kind, key) — the primary key — so a GLOBAL schema uses
scope "*" (globally unique) and a SCOPED schema partitions by the ingest scope.
`create` is a plain INSERT: it fails closed if the node already exists. Applies
run inside a single transaction, so a crash mid-apply leaves no partial writes.
Standard library only.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from ..model import Schema, NodeKind, ExistingNode
from .base import Backend

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    scope TEXT NOT NULL DEFAULT '*',
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
    PRIMARY KEY (scope, kind, key)
);
CREATE TABLE IF NOT EXISTS node_revisions (
    scope TEXT NOT NULL DEFAULT '*',
    kind TEXT NOT NULL,
    key  TEXT NOT NULL,
    props TEXT NOT NULL,
    revision INTEGER NOT NULL,
    owner TEXT DEFAULT '',
    valid_until TEXT DEFAULT '',
    superseded_by_run TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS edges (
    scope TEXT NOT NULL DEFAULT '*',
    src_kind TEXT NOT NULL, src_key TEXT NOT NULL,
    field TEXT NOT NULL,
    dst_kind TEXT NOT NULL, dst_key TEXT NOT NULL,
    UNIQUE (scope, src_kind, src_key, field, dst_kind, dst_key)
);
"""


class SqliteBackend(Backend):
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)  # explicit txns
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    # --- transactions ---
    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # --- reads ---
    def read_existing(self, schema: Schema, scope: str) -> dict:
        if schema.scoped:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE scope=?", (scope,))
        else:
            rows = self.conn.execute("SELECT * FROM nodes")
        out = {}
        for row in rows:
            props = json.loads(row["props"])
            nk = schema.kind(row["kind"])
            content = {f: props.get(f) for f in (nk.content_fields if nk else [])}
            out[(row["kind"], row["key"])] = ExistingNode(
                kind=row["kind"], key=row["key"], content=content,
                is_protected=bool(row["is_protected"]), owner=row["owner"] or "",
                status=row["status"] or "active", revision=row["revision"] or 1,
                scope=row["scope"] or "*",
            )
        return out

    # --- writes ---
    def create(self, kind, key, props, prov, status, identity_scope) -> None:
        # Plain INSERT — fails closed (IntegrityError) if the node already exists.
        self.conn.execute(
            "INSERT INTO nodes "
            "(scope, kind, key, props, is_protected, owner, status, revision, "
            " ingest_run_id, provenance_ts, valid_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity_scope, kind.name, key, json.dumps(props, default=str),
             int(bool(prov.get("is_protected"))), prov.get("owner", ""), status, 1,
             prov.get("ingest_run_id", ""), prov.get("provenance_ts", ""), date.today().isoformat()),
        )

    def supersede(self, kind, key, props, prov, run_id, identity_scope) -> int:
        cur = self.conn.execute(
            "SELECT props, revision, owner FROM nodes WHERE scope=? AND kind=? AND key=?",
            (identity_scope, kind.name, key)).fetchone()
        if cur is None:
            raise KeyError(f"supersede target missing: {identity_scope}:{kind.name}:{key}")
        old_rev = cur["revision"]
        self.conn.execute(
            "INSERT INTO node_revisions (scope, kind, key, props, revision, owner, valid_until, superseded_by_run) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (identity_scope, kind.name, key, cur["props"], old_rev, cur["owner"] or "",
             date.today().isoformat(), run_id))
        new_rev = old_rev + 1
        self.conn.execute(
            "UPDATE nodes SET props=?, is_protected=?, owner=?, status='active', revision=?, "
            "ingest_run_id=?, provenance_ts=?, valid_from=? WHERE scope=? AND kind=? AND key=?",
            (json.dumps(props, default=str), int(bool(prov.get("is_protected"))),
             prov.get("owner", ""), new_rev, prov.get("ingest_run_id", ""),
             prov.get("provenance_ts", ""), date.today().isoformat(), identity_scope, kind.name, key))
        return new_rev

    def link(self, src_kind, src_key, field, dst_kind, dst_key, identity_scope) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO edges (scope, src_kind, src_key, field, dst_kind, dst_key) "
            "VALUES (?,?,?,?,?,?)",
            (identity_scope, src_kind, src_key, field, dst_kind, dst_key))

    def close(self) -> None:
        self.conn.close()
