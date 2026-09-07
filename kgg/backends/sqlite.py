"""Zero-infra SQLite backend — the quickstart store.

Everything (nodes, revision history, edges, the contradiction ledger, the gap
registry) lives in one file with no server to run. Identity is (scope, kind, key)
— the primary key — so a GLOBAL schema uses scope "*" (globally unique) and a
SCOPED schema partitions by the ingest scope. `create` is a plain INSERT: it
fails closed if the node already exists. Applies run inside a single
transaction, so a crash mid-apply leaves no partial writes. Standard library
only.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

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
    slot TEXT DEFAULT '',
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
    superseded_by_run TEXT DEFAULT '',
    ingest_run_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS edges (
    scope TEXT NOT NULL DEFAULT '*',
    src_kind TEXT NOT NULL, src_key TEXT NOT NULL,
    field TEXT NOT NULL,
    dst_kind TEXT NOT NULL, dst_key TEXT NOT NULL,
    UNIQUE (scope, src_kind, src_key, field, dst_kind, dst_key)
);
-- A derived observation ledger. One row per disagreement (pair_key is
-- order-independent), never a third truth value on either claim.
CREATE TABLE IF NOT EXISTS contradictions (
    pair_key TEXT PRIMARY KEY,
    left_id TEXT NOT NULL, right_id TEXT NOT NULL,
    slot TEXT DEFAULT '', detector TEXT DEFAULT '', reason TEXT DEFAULT '',
    left_snapshot TEXT DEFAULT '', right_snapshot TEXT DEFAULT '',
    left_owner TEXT DEFAULT '', right_owner TEXT DEFAULT '',
    left_source TEXT DEFAULT '', right_source TEXT DEFAULT '',
    left_run_id TEXT DEFAULT '', right_run_id TEXT DEFAULT '',
    state TEXT DEFAULT 'active',
    detected_run_id TEXT DEFAULT '', detected_ts TEXT DEFAULT '',
    resolved_ts TEXT DEFAULT '', resolution TEXT DEFAULT ''
);
-- One row per gap OBSERVATION. Promotion counts distinct scopes, so the raw
-- observations have to survive rather than being folded into a counter.
CREATE TABLE IF NOT EXISTS gap_observations (
    slug TEXT NOT NULL,
    means TEXT DEFAULT '',
    kind TEXT DEFAULT '', key TEXT DEFAULT '', item_id TEXT DEFAULT '',
    scope TEXT DEFAULT '', source TEXT DEFAULT '',
    run_id TEXT DEFAULT '', observed_on TEXT DEFAULT '',
    recorded_ts TEXT DEFAULT '',
    UNIQUE (slug, item_id, run_id)
);
CREATE TABLE IF NOT EXISTS gap_resolutions (
    slug TEXT PRIMARY KEY,
    status TEXT DEFAULT 'open',
    promoted_to TEXT DEFAULT '',
    note TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    ts TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nodes_slot ON nodes (scope, kind, slot);
CREATE INDEX IF NOT EXISTS idx_gap_slug ON gap_observations (slug);
"""

# Columns added after 0.1.0. SQLite has no "ADD COLUMN IF NOT EXISTS", and a
# pre-existing database from an older kgg would otherwise fail on first read
# with an opaque "no such column" rather than upgrading.
_MIGRATIONS = [
    ("nodes", "slot", "TEXT DEFAULT ''"),
    ("node_revisions", "ingest_run_id", "TEXT DEFAULT ''"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteBackend(Backend):
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)  # explicit txns
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, decl in _MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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
                scope=row["scope"] or "*", slot=row["slot"] or "",
            )
        return out

    def _row_to_node(self, row) -> dict:
        return {
            "kind": row["kind"], "key": row["key"], "scope": row["scope"] or "*",
            "props": json.loads(row["props"]), "owner": row["owner"] or "",
            "status": row["status"] or "active", "revision": row["revision"] or 1,
            "is_protected": bool(row["is_protected"]),
            "run_id": row["ingest_run_id"] or "", "provenance_ts": row["provenance_ts"] or "",
            "valid_from": row["valid_from"] or "", "slot": row["slot"] or "",
            "source": (json.loads(row["props"]) or {}).get("source", ""),
        }

    def read_node(self, kind: str, key: str, identity_scope: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE scope=? AND kind=? AND key=?",
            (identity_scope, kind, key)).fetchone()
        return self._row_to_node(row) if row else None

    def read_revisions(self, kind: str, key: str, identity_scope: str,
                       limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM node_revisions WHERE scope=? AND kind=? AND key=? "
            "ORDER BY revision DESC LIMIT ?",
            (identity_scope, kind, key, limit)).fetchall()
        return [{
            "revision": r["revision"], "props": json.loads(r["props"]),
            "owner": r["owner"] or "", "valid_until": r["valid_until"] or "",
            "superseded_by_run": r["superseded_by_run"] or "",
            "run_id": (r["ingest_run_id"] if "ingest_run_id" in r.keys() else "") or "",
        } for r in rows]

    def read_edges(self, kind: str, key: str, identity_scope: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT field, dst_kind, dst_key FROM edges "
            "WHERE scope=? AND src_kind=? AND src_key=? ORDER BY field, dst_key",
            (identity_scope, kind, key)).fetchall()
        return [dict(r) for r in rows]

    def read_nodes(self, schema: Schema, scope: str) -> list[dict]:
        if schema.scoped:
            rows = self.conn.execute("SELECT * FROM nodes WHERE scope=?", (scope,))
        else:
            rows = self.conn.execute("SELECT * FROM nodes")
        return [self._row_to_node(r) for r in rows]

    # --- writes ---
    def create(self, kind, key, props, prov, status, identity_scope) -> None:
        # Plain INSERT — fails closed (IntegrityError) if the node already exists.
        self.conn.execute(
            "INSERT INTO nodes "
            "(scope, kind, key, props, is_protected, owner, status, revision, "
            " ingest_run_id, provenance_ts, valid_from, slot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity_scope, kind.name, key, json.dumps(_stored(props, prov), default=str),
             int(bool(prov.get("is_protected"))), prov.get("owner", ""), status, 1,
             prov.get("ingest_run_id", ""), prov.get("provenance_ts", ""),
             date.today().isoformat(), _slot(kind, props)),
        )

    def supersede(self, kind, key, props, prov, run_id, identity_scope) -> int:
        cur = self.conn.execute(
            "SELECT props, revision, owner, ingest_run_id FROM nodes "
            "WHERE scope=? AND kind=? AND key=?",
            (identity_scope, kind.name, key)).fetchone()
        if cur is None:
            raise KeyError(f"supersede target missing: {identity_scope}:{kind.name}:{key}")
        old_rev = cur["revision"]
        self.conn.execute(
            "INSERT INTO node_revisions (scope, kind, key, props, revision, owner, "
            "valid_until, superseded_by_run, ingest_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (identity_scope, kind.name, key, cur["props"], old_rev, cur["owner"] or "",
             date.today().isoformat(), run_id, cur["ingest_run_id"] or ""))
        new_rev = old_rev + 1
        self.conn.execute(
            "UPDATE nodes SET props=?, is_protected=?, owner=?, status='active', revision=?, "
            "ingest_run_id=?, provenance_ts=?, valid_from=?, slot=? "
            "WHERE scope=? AND kind=? AND key=?",
            (json.dumps(_stored(props, prov), default=str), int(bool(prov.get("is_protected"))),
             prov.get("owner", ""), new_rev, prov.get("ingest_run_id", ""),
             prov.get("provenance_ts", ""), date.today().isoformat(), _slot(kind, props),
             identity_scope, kind.name, key))
        return new_rev

    def link(self, src_kind, src_key, field, dst_kind, dst_key, identity_scope) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO edges (scope, src_kind, src_key, field, dst_kind, dst_key) "
            "VALUES (?,?,?,?,?,?)",
            (identity_scope, src_kind, src_key, field, dst_kind, dst_key))

    # --- contradiction ledger ---
    def read_contradictions(self, state: str = "active") -> list[dict]:
        q = "SELECT * FROM contradictions"
        params: tuple = ()
        if state and state != "all":
            q += " WHERE state=?"
            params = (state,)
        q += " ORDER BY detected_ts DESC, pair_key"
        return [dict(r) for r in self.conn.execute(q, params)]

    def write_contradictions(self, upsert, resolve, reactivate) -> dict:
        now = _now()
        n_up = n_res = n_re = 0
        for obs in list(upsert) + list(reactivate):
            d = obs.to_dict()
            d["detected_ts"] = now
            d["state"] = "active"
            d["resolved_ts"] = ""
            d["resolution"] = ""
            cols = [c for c in d if c != "pair_key"]
            self.conn.execute(
                f"INSERT INTO contradictions (pair_key, {', '.join(cols)}) "
                f"VALUES (?{', ?' * len(cols)}) ON CONFLICT(pair_key) DO UPDATE SET "
                + ", ".join(f"{c}=excluded.{c}" for c in cols),
                tuple([d["pair_key"]] + [d[c] for c in cols]))
            n_up += 1
        n_re = len(list(reactivate))
        for r in resolve:
            # Resolution retains the row and both snapshots — "we disagreed and
            # here is how it ended" is the part worth keeping.
            self.conn.execute(
                "UPDATE contradictions SET state='resolved', resolved_ts=?, resolution=? "
                "WHERE pair_key=? AND state='active'",
                (now, r.get("resolution", ""), r["pair_key"]))
            n_res += 1
        return {"upserted": n_up, "resolved": n_res, "reactivated": n_re}

    # --- gap registry ---
    def record_gaps(self, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO gap_observations "
                "(slug, means, kind, key, item_id, scope, source, run_id, observed_on, recorded_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["slug"], r.get("means", ""), r.get("kind", ""), r.get("key", ""),
                 r.get("item_id", ""), r.get("scope", ""), r.get("source", ""),
                 r.get("run_id", ""), r.get("observed_on", ""), _now()))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return n

    def read_gap_rows(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM gap_observations ORDER BY recorded_ts")]

    def read_gap_resolutions(self) -> dict:
        return {r["slug"]: dict(r) for r in self.conn.execute("SELECT * FROM gap_resolutions")}

    def set_gap_resolution(self, slug, status, promoted_to="", note="", actor="") -> None:
        self.conn.execute(
            "INSERT INTO gap_resolutions (slug, status, promoted_to, note, actor, ts) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
            "status=excluded.status, promoted_to=excluded.promoted_to, "
            "note=excluded.note, actor=excluded.actor, ts=excluded.ts",
            (slug, status, promoted_to, note, actor, _now()))

    def close(self) -> None:
        self.conn.close()


def _slot(kind: NodeKind, props: dict) -> str:
    from ..checks import slot_value
    return slot_value(kind, props) if getattr(kind, "claim_slot", None) else ""


def _stored(props: dict, prov: dict) -> dict:
    """Item props merged with provenance, as one stored document.

    `owner`, `ingest_run_id` and `provenance_ts` also get their own columns
    because they are queried; `source` and `ingested_by` have none, and storing
    them only in the columns would drop them entirely. The Neo4j backend already
    merges the whole provenance stamp into the node, so merging here is what
    makes the two backends answer `kgg explain` the same way — a node's origin
    should not depend on which store it landed in.

    Item fields win on a key collision: a writer's own field is data, and letting
    the stamp overwrite it would silently corrupt content.
    """
    return {**prov, **props}
