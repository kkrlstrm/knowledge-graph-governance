"""The conformance suite, run as a test.

`kgg conformance` is the artifact a reader can run; this makes CI fail when a
documented guarantee stops holding, without anyone remembering to invoke it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgg import conformance


def test_every_documented_guarantee_holds():
    results = conformance.run_all()
    assert results, "no conformance cases found — the suite is the proof, so an empty run is a failure"
    failed = [r for r in results if not r.ok]
    assert not failed, "\n".join(
        f"\n[{r.guarantee}] {r.case}: {r.title}\n    {r.detail}" for r in failed)


def test_cases_declare_a_guarantee_and_a_title():
    """A case with no guarantee cannot be reported against a README claim."""
    import yaml
    bad = []
    for p in sorted(conformance.DEFAULT_CASE_DIR.glob("*.yaml")):
        spec = yaml.safe_load(p.read_text()) or {}
        if not spec.get("guarantee") or not spec.get("title") or not spec.get("steps"):
            bad.append(p.name)
    assert not bad, f"cases missing guarantee/title/steps: {bad}"


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
