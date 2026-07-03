"""Kernel unit tests: verdicts, audit chain, proposals. No backend, no deps."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgg import audit, proposals
from kgg.verdicts import Verdict, Finding, RUN, worst, item_verdict, run_halts


def test_verdict_precedence():
    fs = [Finding("a", "t", Verdict.ALLOW, ""), Finding("b", "t", Verdict.BLOCK, ""),
          Finding("c", "t", Verdict.REQUIRE_APPROVAL, "")]
    assert worst(fs) == Verdict.BLOCK
    assert item_verdict(fs, "t") == Verdict.BLOCK
    assert run_halts([Finding("x", RUN, Verdict.HALT, "")]) is True
    assert worst([]) == Verdict.ALLOW


def test_audit_chain_and_tamper():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "audit.jsonl"
        audit.append(log, {"event": "apply", "run_id": "r1"})
        audit.append(log, {"event": "apply", "run_id": "r2"})
        e3 = audit.append(log, {"event": "apply", "run_id": "r3"})
        res = audit.verify(log)
        assert res["ok"] and res["entries"] == 3
        assert audit.last_hash(log) == e3["hash"]
        lines = log.read_text().splitlines()
        lines[1] = lines[1].replace("r2", "r2_TAMPERED")
        log.write_text("\n".join(lines) + "\n")
        res2 = audit.verify(log)
        assert not res2["ok"] and res2["broken_at"] == 2


def test_proposal_hash_invalidation():
    with tempfile.TemporaryDirectory() as d:
        pdir = Path(d) / "proposals"
        yfile = Path(d) / "x.yaml"
        yfile.write_text("source: s\ndate: 2026-07-03\nnodes: {}\n")

        class _P:  # minimal plan-like object for build()
            def __init__(s):
                s.kind, s.key, s.id = "insight", "k", "insight:k"
                s.verdict, s.action, s.detail = Verdict.REQUIRE_APPROVAL, "held", ""
                s.approved, s.findings = False, []
        rid = proposals.new_run_id(yfile)
        prop = proposals.build(rid, str(yfile), "kai", "scope", [_P()], [], schema_hash="abc")
        proposals.save(pdir, prop)
        prop, granted = proposals.record_approval(prop, ["insight:k"], approver="kai")
        proposals.save(pdir, prop)
        assert "insight:k" in proposals.approved_ids(prop)
        assert proposals.find_for_hash(pdir, proposals.content_hash(yfile)) is not None
        yfile.write_text(yfile.read_text() + "\n# edit\n")
        assert proposals.find_for_hash(pdir, proposals.content_hash(yfile)) is None


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
