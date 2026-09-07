"""End-to-end tests against the real SQLite backend (stdlib only).

Proves the full pipeline — validate -> graded verdict -> resolve -> apply,
including provenance, refs, supersession history, protection, and the audit
chain — with no external services.
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgg import audit, proposals
from kgg.engine import ingest
from kgg.checks import CREATE, UNCHANGED, SUPERSEDE, CREATE_UNMAPPED, HELD, BLOCKED

SCHEMA = str(Path(__file__).resolve().parent.parent / "examples" / "gtm" / "schema.yaml")

CLEAN = """
scope: Northwind
source: test
date: 2026-07-03
declare:
  entity:
    - {name: Acme Robotics}
  persona:
    - {name: Chief Revenue Officer}
nodes:
  insight:
    - tag: acme_new_cro
      category: signal.new_hire
      statement: "New CRO at Acme."
      entities: [Acme Robotics]
      personas: [Chief Revenue Officer]
      evidence_strength: 5
"""


class Env:
    def __init__(self, d):
        self.d = Path(d)
        self.db = f"sqlite:{self.d/'g.db'}"
        self.props = str(self.d / "proposals")
        self.audit = str(self.d / "audit.jsonl")

    def write(self, text, name="in.yaml"):
        p = self.d / name
        p.write_text(text)
        return str(p)

    def run(self, path, apply=False, owner="agent", approve_all=False):
        return ingest(path, SCHEMA, self.db, apply=apply, owner=owner,
                      approve_all=approve_all, proposal_dir=self.props, audit_log=self.audit)

    def rows(self, sql):
        c = sqlite3.connect(str(self.d / "g.db")); c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql)]
        finally:
            c.close()

    def actions(self, result):
        return {p.key: p.action for p in result["plans"]}


def test_clean_apply_writes_nodes_edges_provenance():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        r = e.run(e.write(CLEAN), apply=True)
        acts = e.actions(r)
        assert acts["acme_new_cro"] == CREATE
        assert acts["Acme Robotics"] == CREATE and acts["Chief Revenue Officer"] == CREATE
        node = e.rows("SELECT * FROM nodes WHERE key='acme_new_cro'")[0]
        assert node["owner"] == "agent" and node["status"] == "active" and node["revision"] == 1
        assert node["ingest_run_id"] == r["run_id"] and node["provenance_ts"]  # provenance columns
        props = json.loads(node["props"])
        assert props["category"] == "signal.new_hire"  # item fields preserved in props
        edges = e.rows("SELECT * FROM edges")
        assert any(x["dst_key"] == "Acme Robotics" and x["field"] == "entities" for x in edges)
        assert audit.verify(e.audit)["ok"]


def test_unknown_category_blocks_only_that_item():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        bad = CLEAN + """    - tag: bad_one
      category: signal.not_real
      statement: "x"
      evidence_strength: 3
"""
        r = e.run(e.write(bad))
        acts = e.actions(r)
        assert acts["bad_one"] == BLOCKED
        assert acts["acme_new_cro"] == CREATE  # sibling proceeds


def test_undeclared_ref_blocks():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        txt = """
scope: N
source: t
date: 2026-07-03
nodes:
  insight:
    - tag: dangling
      category: signal.new_hire
      statement: "refs a ghost"
      entities: [Ghost Corp]
      evidence_strength: 3
"""
        r = e.run(e.write(txt))
        assert e.actions(r)["dangling"] == BLOCKED


def test_identical_reingest_is_noop():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        f = e.write(CLEAN)
        e.run(f, apply=True)
        r2 = e.run(f, apply=True)
        assert e.actions(r2)["acme_new_cro"] == UNCHANGED
        assert e.rows("SELECT revision FROM nodes WHERE key='acme_new_cro'")[0]["revision"] == 1


def test_conflict_held_then_approved_supersede():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        e.run(e.write(CLEAN), apply=True)
        changed = CLEAN.replace('"New CRO at Acme."', '"New CRO at Acme — now confirmed via LinkedIn."')
        f2 = e.write(changed, "in2.yaml")
        r_dry = e.run(f2)
        assert e.actions(r_dry)["acme_new_cro"] == HELD
        # approve the held item, then apply
        prop = proposals.find_for_hash(e.props, proposals.content_hash(f2))
        prop, _ = proposals.record_approval(prop, ["acme_new_cro"], approver="kai")
        proposals.save(e.props, prop)
        r_apply = e.run(f2, apply=True)
        assert e.actions(r_apply)["acme_new_cro"] == SUPERSEDE
        assert e.rows("SELECT revision FROM nodes WHERE key='acme_new_cro'")[0]["revision"] == 2
        revs = e.rows("SELECT * FROM node_revisions WHERE key='acme_new_cro'")
        assert len(revs) == 1 and revs[0]["revision"] == 1  # prior belief preserved


def test_supersede_flag_direct():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        e.run(e.write(CLEAN), apply=True)
        changed = CLEAN.replace("evidence_strength: 5", "evidence_strength: 4\n      supersede: true")
        r = e.run(e.write(changed, "in2.yaml"), apply=True)
        assert e.actions(r)["acme_new_cro"] == SUPERSEDE
        assert e.rows("SELECT revision FROM nodes WHERE key='acme_new_cro'")[0]["revision"] == 2


def test_protected_guard_blocks_foreign_owner():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        prot = CLEAN.replace("evidence_strength: 5", "evidence_strength: 5\n      protected: true")
        e.run(e.write(prot), apply=True, owner="kai")
        changed = prot.replace('"New CRO at Acme."', '"agent overwrite attempt"')
        r = e.run(e.write(changed, "in2.yaml"), owner="agent")
        assert e.actions(r)["acme_new_cro"] == BLOCKED


def test_unmapped_held_then_created():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        txt = CLEAN + """
unmapped_for_review:
  - tag: novel_thing
    kind: insight
    statement: "no home yet"
    unmapped_gap: ai-governance-clause
    unmapped_reason: "Nearest is procurement.compliance_gate, which is about a prerequisite we must satisfy."
"""
        r = e.run(e.write(txt))
        assert e.actions(r)["novel_thing"] == HELD
        r2 = e.run(e.write(txt), apply=True, approve_all=True)
        assert e.actions(r2)["novel_thing"] == CREATE_UNMAPPED
        assert e.rows("SELECT status FROM nodes WHERE key='novel_thing'")[0]["status"] == "unmapped"
        # re-applying the same file must be idempotent — an existing unmapped node
        # is UNCHANGED, not re-created (regression: fail-closed create would raise).
        r3 = e.run(e.write(txt), apply=True, approve_all=True)
        assert e.actions(r3)["novel_thing"] == UNCHANGED
        assert len(e.rows("SELECT * FROM nodes WHERE key='novel_thing'")) == 1


def test_halt_missing_source():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        r = e.run(e.write("date: 2026-07-03\nnodes: {}\n"))
        assert r.get("halted") and "plans" not in r


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
