"""Unit tests for the gate extensions: plugins, vocabulary curation, gaps,
contradictions, the read surface, and the three bugs the conformance suite
surfaced while it was being written.

The conformance cases cover the same ground from the outside, in the language of
the README's claims. These pin the mechanisms and the edge cases a product-level
case would not naturally reach.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgg import contradictions, gaps, proposals
from kgg.checks import resolve_actions, PlanContext, BLOCKED, CREATE, HELD
from kgg.engine import ingest
from kgg.explain import explain, VERIFIED, UNVERIFIED, INVALIDATED
from kgg.model import Schema, Vocabulary, validate_schema, EXACT, SYNONYM, DEPRECATED_HIT, UNKNOWN
from kgg.plugins import CheckSpecError, load_check_registries, load_registry
from kgg.verdicts import Finding, Verdict, RUN
from kgg.backends.base import open_backend

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


# ---------------------------------------------------------------------------
# Bug: run ids collided at second resolution, silently overwriting a proposal
# ---------------------------------------------------------------------------

def test_run_ids_are_unique_within_one_second():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.yaml"
        f.write_text("source: t\ndate: 2026-07-03\nnodes: {}\n")
        ids = {proposals.new_run_id(str(f)) for _ in range(200)}
        assert len(ids) == 200, "run id collides; a second proposal overwrites the first, " \
                                "taking any approvals recorded against it with it"


def test_two_dry_runs_of_one_file_keep_two_proposals():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        txt = ("scope: N\nsource: t\ndate: 2026-07-03\nnodes:\n  insight:\n"
               "    - tag: q\n      category: signal.new_hire\n      statement: \"s\"\n"
               "      evidence_strength: 3\n")
        f = e.write(txt)
        r1, r2 = e.run(f), e.run(f)
        assert r1["run_id"] != r2["run_id"]
        assert len(list(Path(e.props).glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# Bug: a run-level BLOCK blocked nothing
# ---------------------------------------------------------------------------

def test_run_level_block_condemns_every_item():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        txt = ("scope: N\nsource: t\ndate: not-a-date\ndeclare:\n"
               "  entity: [{name: Acme Robotics}]\nnodes:\n  insight:\n"
               "    - tag: a\n      category: signal.new_hire\n      statement: \"s\"\n"
               "      entities: [Acme Robotics]\n      evidence_strength: 3\n")
        r = e.run(e.write(txt), apply=True)
        acts = e.by_id(r)
        assert acts["insight:a"] == BLOCKED
        assert acts["entity:Acme Robotics"] == BLOCKED
        assert r["applied"]["created"] == 0
        detail = " ".join(p.detail for p in r["plans"])
        assert "run-level check" in detail


def test_run_level_allow_does_not_block():
    """The floor must be the WORST run finding, not the presence of any finding."""
    schema = Schema.load(GTM)
    ctx = PlanContext(schema=schema, scope="N", source="t", date="2026-07-03",
                      owner="o", run_id="r",
                      nodes={"insight": [{"tag": "a", "category": "signal.new_hire",
                                          "statement": "s", "evidence_strength": 3}]},
                      declared={}, unmapped=[], existing={})
    findings = [Finding("informational", RUN, Verdict.ALLOW, "fyi", severity="info")]
    plans = resolve_actions(ctx, findings)
    assert plans[0].action == CREATE


# ---------------------------------------------------------------------------
# Bug: approvals bound to policy on the ingest path only
# ---------------------------------------------------------------------------

def test_approved_ids_ignores_a_foreign_schema_approval():
    """Defence in depth for a hand-edited or hand-merged proposal file."""
    prop = {"schema_hash": "AAA", "content_hash": "CCC", "items": [],
            "approvals": [
                {"items": ["insight:a"], "schema_hash": "AAA", "content_hash": "CCC"},
                {"items": ["insight:b"], "schema_hash": "BBB", "content_hash": "CCC"},
                {"items": ["insight:c"], "schema_hash": "AAA", "content_hash": "DDD"},
            ]}
    assert proposals.approved_ids(prop) == {"insight:a"}


def test_resolve_ids_reports_every_kind_of_miss():
    prop = {"items": [
        {"id": "insight:held_one", "key": "held_one", "verdict": "REQUIRE_APPROVAL"},
        {"id": "entity:held_one", "key": "held_one", "verdict": "REQUIRE_APPROVAL"},
        {"id": "insight:applied", "key": "applied", "verdict": "ALLOW"},
    ]}
    ok, missed = proposals.resolve_ids_reporting(
        prop, ["insight:held_one", "held_one", "insight:applied", "insight:nope"])
    assert ok == ["insight:held_one"]
    reasons = dict(missed)
    assert "ambiguous" in reasons["held_one"]
    assert "not awaiting approval" in reasons["insight:applied"]
    assert "no such item" in reasons["insight:nope"]


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

def _pack(d: Path, body: str, name="pack.py") -> str:
    (d / name).write_text(body)
    return f"{name}:REGISTRY"


def test_plugin_loads_by_path_relative_to_the_schema():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        spec = _pack(p, "REGISTRY = [('mine', lambda ctx: [])]\n")
        reg = load_check_registries([spec], base_dir=str(p))
        assert [n for n, _ in reg] == ["mine"]


def test_plugin_cannot_shadow_a_core_check():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        spec = _pack(p, "REGISTRY = [('collision', lambda ctx: [])]\n")
        try:
            load_check_registries([spec], base_dir=str(p))
            assert False, "shadowing a kernel check must be refused"
        except CheckSpecError as exc:
            assert "shadows a kernel check" in str(exc)


def test_plugin_rejects_duplicates_and_bad_shapes():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        dup = _pack(p, "REGISTRY = [('x', lambda c: []), ('x', lambda c: [])]\n", "dup.py")
        for spec, needle in [
            (dup, "duplicate check name"),
            (_pack(p, "REGISTRY = 'nope'\n", "s.py"), "expected a list"),
            (_pack(p, "REGISTRY = [('x',)]\n", "t.py"), "not a (name, fn) pair"),
            (_pack(p, "REGISTRY = [('x', 3)]\n", "u.py"), "not callable"),
            (_pack(p, "raise RuntimeError('boom')\n", "v.py"), "raised on import"),
            ("no_colon_here", "not a check spec"),
        ]:
            try:
                load_check_registries([spec], base_dir=str(p))
                assert False, f"expected refusal containing {needle!r}"
            except CheckSpecError as exc:
                assert needle in str(exc), f"want {needle!r} in {exc}"


def test_missing_registry_attribute_is_refused():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "w.py").write_text("OTHER = []\n")
        try:
            load_registry("w.py:REGISTRY", base_dir=str(p))
            assert False
        except CheckSpecError as exc:
            assert "has no attribute" in str(exc)


# ---------------------------------------------------------------------------
# Vocabulary as a curated artifact
# ---------------------------------------------------------------------------

def _vocab(d: Path, text: str) -> Vocabulary:
    (d / "v.yaml").write_text(text)
    return Vocabulary.load("v", d / "v.yaml")


def test_flat_vocabulary_still_loads():
    with tempfile.TemporaryDirectory() as d:
        v = _vocab(Path(d), "terms:\n  a.b: 'A definition.'\n")
        assert v.resolve("a.b") == ("a.b", EXACT)
        assert v.resolve("nope") == (None, UNKNOWN)


def test_curated_vocabulary_resolves_four_ways():
    with tempfile.TemporaryDirectory() as d:
        v = _vocab(Path(d), """
version: "3"
categories:
  Cat:
    a.new:
      definition: "Current."
      synonyms: [a.old_alias]
    a.retired:
      definition: "Gone."
      status: deprecated
      replaced_by: a.new
""")
        assert v.version == "3"
        assert v.resolve("a.new") == ("a.new", EXACT)
        assert v.resolve("a.old_alias") == ("a.new", SYNONYM)
        assert v.resolve("a.retired") == ("a.new", DEPRECATED_HIT)
        assert v.resolve("a.invented") == (None, UNKNOWN)


def test_synonym_folds_and_is_reported_not_silent():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        txt = ("scope: N\nsource: t\ndate: 2026-07-03\nnodes:\n  insight:\n"
               "    - tag: folded\n      category: signal.new_leader\n"
               "      statement: \"s\"\n      evidence_strength: 3\n")
        r = e.run(e.write(txt), apply=True)
        assert e.by_id(r)["insight:folded"] == CREATE
        assert any(f.check == "vocab_synonym_folded" for f in r["findings"]), \
            "a fold must be reported — an unreported rewrite hides a curated alias " \
            "from an invented one"
        backend = open_backend(e.db)
        try:
            node = backend.read_node("insight", "folded", "*")
        finally:
            backend.close()
        assert node["props"]["category"] == "signal.new_hire"


def test_schema_validation_catches_vocabulary_defects():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "v.yaml").write_text(
            "categories:\n  C:\n    t.a:\n      definition: 'A'\n      synonyms: [t.b]\n"
            "    t.b:\n      definition: 'B'\n"
            "    t.c:\n      definition: 'C'\n      status: deprecated\n"
            "      replaced_by: t.ghost\n"
            "    t.d:\n      definition: 'D'\n      status: sideways\n")
        (p / "s.yaml").write_text(
            "name: s\nvocabularies: {v: v.yaml}\nnode_kinds:\n"
            "  insight: {key: tag, vocab_fields: {category: v}, content_fields: [statement]}\n")
        errs = " ".join(validate_schema(Schema.load(p / "s.yaml")))
        assert "cannot fold onto another term" in errs
        assert "not a term in this vocabulary" in errs
        assert "unknown status" in errs


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------

def test_slug_validation():
    assert gaps.validate_slug("effective-size-proxy") == []
    assert gaps.validate_slug("a-b-c-d-e-f") == []
    # shape
    assert gaps.validate_slug("Effective_Size_Proxy")
    assert gaps.validate_slug("oneword")
    assert gaps.validate_slug("a-b-c-d-e-f-g")          # 7 words
    # names nothing
    assert any("writer's state" in p for p in gaps.validate_slug("no-tag-for-this"))
    assert any("filler" in p for p in gaps.validate_slug("the-other-thing"))
    # names the instance
    assert any("names the scope" in p
               for p in gaps.validate_slug("northwind-tourist-town", scope="Northwind"))
    # a slug legitimately sharing words with its own item key is fine
    assert gaps.validate_slug("ai-governance-clause", scope="Northwind") == []


def test_promotion_counts_independent_scopes_not_observations():
    rows = [{"slug": "g", "scope": "A", "means": "m", "item_id": f"insight:{i}"}
            for i in range(9)]
    [one] = gaps.aggregate(rows)
    assert one.observations == 9 and one.independent == 1
    assert not one.promotable(gaps.DEFAULT_PROMOTION_FLOOR), \
        "nine observations from one scope is one loud source, not corroboration"

    rows.append({"slug": "g", "scope": "B", "means": "m2", "item_id": "insight:x"})
    [two] = gaps.aggregate(rows)
    assert two.independent == 2 and two.promotable(gaps.DEFAULT_PROMOTION_FLOOR)


def test_known_gaps_hides_rejected_and_promoted():
    rows = [{"slug": s, "scope": "A", "means": s, "item_id": f"i:{s}"}
            for s in ("open-one", "done-one", "dead-one")]
    ranked = gaps.aggregate(rows, {"done-one": {"status": "promoted", "promoted_to": "a.b"},
                                   "dead-one": {"status": "rejected"}})
    assert [g["slug"] for g in gaps.known_gaps(ranked)] == ["open-one"]
    assert {g["slug"] for g in gaps.known_gaps(ranked, include_promoted=True)} == \
        {"open-one", "done-one"}


def test_only_landed_items_vote_on_the_vocabulary():
    """A held or blocked observation has not entered the graph."""
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        txt = ("scope: N\nsource: t\ndate: 2026-07-03\nnodes:\n  insight:\n"
               "    - tag: held_gap\n      statement: \"s\"\n"
               "      unmapped_gap: effective-size-proxy\n"
               "      unmapped_reason: \"r\"\n      evidence_strength: 3\n")
        f = e.write(txt)
        e.run(f, apply=True)                      # held, not approved -> nothing lands
        backend = open_backend(e.db)
        try:
            assert backend.read_gap_rows() == []
        finally:
            backend.close()
        e.run(f, apply=True, approve_all=True)    # now it lands
        backend = open_backend(e.db)
        try:
            assert [r["slug"] for r in backend.read_gap_rows()] == ["effective-size-proxy"]
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------

def test_detector_needs_overlap_not_just_a_negator():
    assert contradictions.detect(
        "Acme has budget approved this fiscal year.",
        "Acme has not approved budget this fiscal year.")[0] == contradictions.NEGATION_MISMATCH
    # A negated claim about something else entirely is not a disagreement.
    assert contradictions.detect(
        "Acme has budget approved this fiscal year.",
        "Purchasing does not run through a regional cooperative.") is None
    # Two unnegated claims about one subject are not a disagreement either.
    assert contradictions.detect(
        "Acme has budget approved this fiscal year.",
        "Acme has budget approved for the next fiscal year.") is None


def test_antonym_detector():
    hit = contradictions.detect(
        "The finance director is reachable by phone during close.",
        "The finance director is unreachable by phone during close.")
    assert hit and hit[0] in (contradictions.ANTONYM_PAIR, contradictions.NEGATION_MISMATCH)


def test_pair_key_is_order_independent():
    a = contradictions.Observation("insight:a", "insight:b", "s", "d", "r")
    b = contradictions.Observation("insight:b", "insight:a", "s", "d", "r")
    assert a.pair_key == b.pair_key, "one disagreement must be one row, not two"


def test_reconcile_is_idempotent_and_resolution_retains():
    schema = Schema.load(GTM)
    nodes = [
        {"kind": "insight", "key": "a", "status": "active", "slot": "S", "owner": "o",
         "source": "call A", "run_id": "r1",
         "props": {"statement": "Acme has budget approved this year."}},
        {"kind": "insight", "key": "b", "status": "active", "slot": "S", "owner": "o",
         "source": "call B", "run_id": "r2",
         "props": {"statement": "Acme has not approved budget this year."}},
    ]
    first = contradictions.reconcile(schema, nodes, [], run_id="r2")
    assert len(first["upsert"]) == 1 and not first["resolve"]

    stored = [{**first["upsert"][0].to_dict(), "state": "active"}]
    again = contradictions.reconcile(schema, nodes, stored, run_id="r3")
    assert not again["upsert"] and not again["resolve"], "an unchanged graph must produce no churn"

    # One side is revised to agree -> resolve, and the stored snapshots stay put.
    nodes[1]["props"]["statement"] = "Acme has budget approved this year, after review."
    ended = contradictions.reconcile(schema, nodes, stored, run_id="r4")
    assert len(ended["resolve"]) == 1
    assert stored[0]["left_snapshot"] and stored[0]["right_snapshot"]


def test_a_kind_without_a_claim_slot_is_never_scanned():
    """Detection is opt-in per kind — no slot declared, no false positives."""
    schema = Schema.load(GTM)
    nodes = [
        {"kind": "entity", "key": "a", "status": "active", "slot": "", "props": {"name": "A"}},
        {"kind": "entity", "key": "b", "status": "active", "slot": "", "props": {"name": "B"}},
    ]
    assert contradictions.scan(schema, nodes) == []


# ---------------------------------------------------------------------------
# Explain / read integrity
# ---------------------------------------------------------------------------

def _seeded(e: Env):
    txt = ("scope: N\nsource: seed\ndate: 2026-07-03\ndeclare:\n"
           "  entity: [{name: Acme Robotics}]\nnodes:\n  insight:\n"
           "    - tag: solid\n      category: signal.new_hire\n"
           "      statement: \"A claim.\"\n      entities: [Acme Robotics]\n"
           "      evidence_strength: 4\n")
    return e.run(e.write(txt), apply=True)


def _explain(e: Env, target: str):
    backend = open_backend(e.db)
    try:
        return explain(backend, Schema.load(GTM), target, audit_log=e.audit)
    finally:
        backend.close()


def test_explain_verified_and_missing():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d); _seeded(e)
        x = _explain(e, "insight:solid")
        assert x["integrity"]["state"] == VERIFIED
        assert x["current"]["claim"] == "A claim."
        assert x["audit"]["entry"] is not None
        assert any(edge["dst_key"] == "Acme Robotics" for edge in x["edges"])

        gone = _explain(e, "insight:never_written")
        assert gone["integrity"]["state"] == INVALIDATED
        assert gone["current"] is None


def test_explain_unverified_when_provenance_cannot_be_checked():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d); _seeded(e)
        Path(e.audit).write_text("")          # the log is gone; the node is not
        x = _explain(e, "insight:solid")
        assert x["integrity"]["state"] == UNVERIFIED
        assert "no entry in the audit log" in x["integrity"]["reason"]


def test_explain_refuses_a_bare_key():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d); _seeded(e)
        for bad in ("solid", "ghostkind:solid"):
            try:
                _explain(e, bad)
                assert False, f"{bad!r} should not resolve"
            except ValueError:
                pass


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


# ---------------------------------------------------------------------------
# Near-duplicate keys
# ---------------------------------------------------------------------------

def test_similarity_calibration_pairs():
    """The four pairs the threshold was set against. Change one, re-read them all."""
    from kgg import similarity as sim
    def s(a, b):
        return sim.similarity(sim.normalize(a), sim.normalize(b))
    assert s("Acme Robotics", "Acme Robotics, Inc.") >= sim.DEFAULT_THRESHOLD
    assert s("Northwind Analytics", "Northwind Analytic") >= sim.DEFAULT_THRESHOLD
    assert s("Portland, OR", "Portland, ME") < sim.DEFAULT_THRESHOLD
    assert s("Q3 Report", "Q4 Report") < sim.DEFAULT_THRESHOLD


def test_entropy_gate_excludes_low_signal_keys():
    from kgg.similarity import is_comparable, normalize
    for bad in ("ACME", "Q3", "US", "aaaaaa", "ab"):
        assert not is_comparable(normalize(bad)), f"{bad!r} should be excluded"
    for good in ("Acme Robotics", "Northwind Analytics", "Portland, OR"):
        assert is_comparable(normalize(good)), f"{good!r} should be comparable"


def test_blocking_does_not_band_on_length():
    """Regression: a length band put the target pair in different blocks.

    A suffix addition changes length by definition, so banding on length excludes
    the exact case this check exists for.
    """
    from kgg.similarity import block_key, normalize
    assert block_key(normalize("Acme Robotics")) == block_key(normalize("Acme Robotics, Inc."))


def test_find_near_duplicates_skips_exact_and_self():
    from kgg.similarity import find_near_duplicates
    hits = list(find_near_duplicates(["Acme Robotics"], ["Acme Robotics"]))
    assert hits == [], "an identical key is the collision check's job, not this one"
    hits = list(find_near_duplicates(["Acme Robotics, Inc."], ["Acme Robotics"]))
    assert len(hits) == 1 and hits[0][1] == "Acme Robotics"


def test_near_duplicate_is_held_and_never_merges():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        seed = ("scope: N\nsource: t\ndate: 2026-07-03\ndeclare:\n"
                "  entity: [{name: Acme Robotics}]\nnodes:\n  insight:\n"
                "    - tag: a\n      category: signal.new_hire\n      statement: \"s\"\n"
                "      entities: [Acme Robotics]\n      evidence_strength: 3\n")
        e.run(e.write(seed), apply=True)

        near = ("scope: N\nsource: t\ndate: 2026-07-10\ndeclare:\n"
                "  entity: [{name: \"Acme Robotics, Inc.\"}]\nnodes:\n  insight:\n"
                "    - tag: b\n      category: signal.org_change\n      statement: \"s2\"\n"
                "      entities: [\"Acme Robotics, Inc.\"]\n      evidence_strength: 3\n")
        f = e.write(near, "near.yaml")
        r = e.run(f)
        assert e.by_id(r)["entity:Acme Robotics, Inc."] == HELD
        assert any(f_.check == "near_duplicate_key" for f_ in r["findings"])

        # Approving creates a SEPARATE node; the original is untouched.
        r2 = e.run(f, apply=True, approve_all=True)
        assert e.by_id(r2)["entity:Acme Robotics, Inc."] == CREATE
        backend = open_backend(e.db)
        try:
            orig = backend.read_node("entity", "Acme Robotics", "*")
            new = backend.read_node("entity", "Acme Robotics, Inc.", "*")
        finally:
            backend.close()
        assert orig and new and orig["revision"] == 1


def test_near_duplicate_check_is_off_unless_the_kind_opts_in():
    """`persona` does not enable it — distinct roles share most of their words."""
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        seed = ("scope: N\nsource: t\ndate: 2026-07-03\ndeclare:\n"
                "  persona: [{name: VP Sales Operations}]\n")
        e.run(e.write(seed), apply=True)
        near = ("scope: N\nsource: t\ndate: 2026-07-10\ndeclare:\n"
                "  persona: [{name: VP Sales Operation}]\n")
        r = e.run(e.write(near, "p.yaml"), apply=True)
        assert e.by_id(r)["persona:VP Sales Operation"] == CREATE
