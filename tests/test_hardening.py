"""Hardening tests: composite identity, scoped mode, fail-closed create,
transactional rollback, schema validation, schema-bound approvals."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgg import proposals
from kgg.engine import ingest
from kgg.model import Schema, validate_schema
from kgg.backends.sqlite import SqliteBackend
from kgg.backends.base import GLOBAL_PARTITION
from kgg.checks import CREATE, BLOCKED, HELD, UNCHANGED, SUPERSEDE

GTM = str(Path(__file__).resolve().parent.parent / "examples" / "gtm" / "schema.yaml")


class Env:
    def __init__(self, d):
        self.d = Path(d)
        self.db = f"sqlite:{self.d/'g.db'}"
        self.props = str(self.d / "proposals")
        self.audit = str(self.d / "audit.jsonl")

    def write(self, text, name="in.yaml"):
        p = self.d / name; p.write_text(text); return str(p)

    def run(self, path, schema=GTM, apply=False, owner="agent", approve_all=False):
        return ingest(path, schema, self.db, apply=apply, owner=owner,
                      approve_all=approve_all, proposal_dir=self.props, audit_log=self.audit)

    def by_id(self, result):
        return {p.id: p.action for p in result["plans"]}

    def rows(self, sql):
        c = sqlite3.connect(str(self.d / "g.db")); c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql)]
        finally:
            c.close()


def _scoped_schema(d: Path) -> str:
    (d / "vocab.yaml").write_text("terms:\n  cat.a: 'A category'\n")
    s = d / "scoped_schema.yaml"
    s.write_text(
        "name: t\nidentity: scoped\nvocabularies: {tax: vocab.yaml}\n"
        "node_kinds:\n  insight:\n    key: tag\n    required: [statement]\n"
        "    vocab_fields: {category: tax}\n    content_fields: [statement]\n    supersedable: true\n")
    return str(s)


# ---- #1 composite identity: same key across kinds must not bleed ----

def test_composite_identity_no_bleed():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        # entity and insight share the key "shared"; the insight is blocked (bad
        # vocab), the entity (declared) must still be CREATE.
        txt = """
scope: N
source: t
date: 2026-07-03
declare:
  entity: [{name: shared}]
nodes:
  insight:
    - tag: shared
      category: signal.not_real
      statement: "bad category -> blocked"
      evidence_strength: 3
"""
        r = e.run(e.write(txt))
        acts = e.by_id(r)
        assert acts["insight:shared"] == BLOCKED
        assert acts["entity:shared"] == CREATE   # key-only resolution would have blocked this too


# ---- #2 scoped identity: same key in two scopes are distinct nodes ----

def test_scoped_identity_two_scopes():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d); schema = _scoped_schema(Path(d))
        base = "source: t\ndate: 2026-07-03\nnodes:\n  insight:\n    - tag: dup\n      category: cat.a\n      statement: \"s\"\n"
        e.run(e.write("scope: alpha\n" + base, "a.yaml"), schema=schema, apply=True)
        e.run(e.write("scope: beta\n" + base, "b.yaml"), schema=schema, apply=True)
        scopes = sorted(r["scope"] for r in e.rows("SELECT scope FROM nodes WHERE key='dup'"))
        assert scopes == ["alpha", "beta"]   # two distinct nodes, one per scope


def test_global_identity_same_key_collapses():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        base = ("source: t\ndate: 2026-07-03\nnodes:\n  insight:\n    - tag: g\n"
                "      category: signal.new_hire\n      statement: \"s\"\n      evidence_strength: 3\n")
        e.run(e.write("scope: alpha\n" + base, "a.yaml"), apply=True)
        r2 = e.run(e.write("scope: beta\n" + base, "b.yaml"), apply=True)
        # global identity: (kind,key) is unique regardless of scope -> second is a no-op
        assert e.by_id(r2)["insight:g"] == UNCHANGED
        assert len(e.rows("SELECT * FROM nodes WHERE key='g'")) == 1


# ---- #3 transactional apply: a failure mid-apply leaves no partial writes ----

def test_transaction_rollback_no_partial():
    with tempfile.TemporaryDirectory() as d:
        b = SqliteBackend(str(Path(d) / "t.db"))

        class K:  # minimal NodeKind stand-in
            name = "insight"
        try:
            with b.transaction():
                b.create(K(), "a", {"x": 1}, {"owner": "kai"}, "active", GLOBAL_PARTITION)
                raise RuntimeError("boom mid-apply")
        except RuntimeError:
            pass
        rows = list(b.conn.execute("SELECT count(*) c FROM nodes"))
        assert rows[0]["c"] == 0    # rolled back — node 'a' did not persist
        b.close()


# ---- #4 fail-closed create: creating an existing node raises ----

def test_fail_closed_create_raises():
    with tempfile.TemporaryDirectory() as d:
        b = SqliteBackend(str(Path(d) / "t.db"))

        class K:
            name = "insight"
        with b.transaction():
            b.create(K(), "a", {"x": 1}, {"owner": "kai"}, "active", GLOBAL_PARTITION)
        raised = False
        try:
            with b.transaction():
                b.create(K(), "a", {"x": 2}, {"owner": "kai"}, "active", GLOBAL_PARTITION)
        except sqlite3.IntegrityError:
            raised = True
        assert raised    # storage layer refuses to overwrite via create
        b.close()


# ---- #5 schema self-validation ----

def test_validate_schema_catches_bad_contract():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "v.yaml").write_text("terms:\n  a: 'x'\n")
        bad = Path(d) / "bad.yaml"
        bad.write_text(
            "name: b\nidentity: sideways\nvocabularies: {v: v.yaml}\n"
            "node_kinds:\n  'in:sight': {key: tag, vocab_fields: {c: missingvocab}, refs: {e: ghostkind}}\n")
        errs = validate_schema(Schema.load(bad))
        joined = " ".join(errs)
        assert "identity" in joined            # bad identity mode
        assert "':'" in joined or "must not contain" in joined  # colon in kind name
        assert "unknown vocabulary" in joined  # missing vocab
        assert "unknown target kind" in joined  # missing ref target


def test_valid_schema_passes():
    assert validate_schema(Schema.load(GTM)) == []


# ---- #6 approvals are bound to the schema hash ----

def test_approval_ignored_when_schema_changes():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        # null category -> unmapped -> held. The gap slug is required: an unmapped
        # item that names no mechanic cannot be joined by a second writer.
        base = ("source: t\ndate: 2026-07-03\nnodes:\n  insight:\n    - tag: u\n"
                "      statement: \"no category\"\n"
                "      unmapped_gap: effective-size-proxy\n"
                "      unmapped_reason: \"Nearest tag bands by deal size, not fit.\"\n")
        f = e.write("scope: N\n" + base)
        r = e.run(f)                    # held under the GTM schema hash
        assert e.by_id(r)["insight:u"] == HELD
        # approve against the GTM-schema proposal
        prop = proposals.find_for_hash(e.props, proposals.content_hash(f), Schema.load(GTM).fingerprint())
        prop, granted = proposals.record_approval(prop, ["insight:u"], approver="kai")
        proposals.save(e.props, prop)
        assert granted == ["insight:u"]
        # now apply under a DIFFERENT schema — approval must not carry over
        other = _scoped_schema(Path(d))   # different fingerprint
        r2 = e.run(f, schema=other, apply=True)
        assert e.by_id(r2)["insight:u"] == HELD   # stale approval correctly ignored


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    p = f = 0
    for fn in fns:
        try:
            fn(); p += 1; print(f"  ✓ {fn.__name__}")
        except Exception:
            f += 1; print(f"  ✗ {fn.__name__}"); traceback.print_exc()
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
