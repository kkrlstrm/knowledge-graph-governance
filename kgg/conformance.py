"""`kgg conformance` — the guarantees, as executable refusals.

A write gate is a claim about what it *refuses*. Every README bullet in this repo
is such a claim, and prose cannot discharge one: "undefined terms are blocked" is
either demonstrable on a file that contains an undefined term, or it is marketing.

So each guarantee is a case file. A case declares the ingest files, the sequence
of operations, and the exact per-item verdict expected at each step. The runner
executes them against a real backend in a throwaway directory and reports a table
grouped by guarantee. CI runs it; the README publishes the result.

This is not a second test suite. The unit tests check that the code does what the
code intends. These check that the *product claims* hold, in the vocabulary a
reader of the README would use, and they fail loudly when a refactor quietly
narrows one.

Case format (see examples/conformance/):

    case: vocab_unknown_term_blocked
    guarantee: taxonomy-drift
    title: "An undefined vocabulary term is BLOCKed; its siblings still apply"
    schema: examples/gtm/schema.yaml     # repo path, or a name under `files`
    files:
      base: |
        source: t
        date: 2026-07-03
        ...
    steps:
      - ingest: {file: base}
        expect:
          items: {insight:bad: blocked, insight:good: create}
"""
from __future__ import annotations

import json
import shutil
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import audit, gaps as gapmod, proposals
from .engine import ingest
from .explain import explain
from .model import Schema, validate_schema
from .backends.base import open_backend, GLOBAL_PARTITION

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASE_DIR = REPO_ROOT / "examples" / "conformance"
DEFAULT_SCHEMA = "examples/gtm/schema.yaml"


class CaseFailure(AssertionError):
    """One expectation did not hold. The message names step, field, want, got."""


@dataclass
class Result:
    case: str
    guarantee: str
    title: str
    ok: bool
    detail: str = ""
    steps: int = 0

    def to_dict(self) -> dict:
        return {"case": self.case, "guarantee": self.guarantee, "title": self.title,
                "ok": self.ok, "detail": self.detail, "steps": self.steps}


# ---------------------------------------------------------------------------
# Expectation helpers
# ---------------------------------------------------------------------------

def _want(step_no: int, what: str, want, got):
    if want != got:
        raise CaseFailure(f"step {step_no}: {what} — want {want!r}, got {got!r}")


def _want_in(step_no: int, what: str, needle: str, hay: str):
    if needle.lower() not in (hay or "").lower():
        raise CaseFailure(f"step {step_no}: {what} — {needle!r} not found in {hay!r}")


# ---------------------------------------------------------------------------
# Environment for one case
# ---------------------------------------------------------------------------

class CaseEnv:
    def __init__(self, workdir: Path, files: dict, default_schema: str):
        self.d = workdir
        self.db = f"sqlite:{self.d / 'graph.db'}"
        self.props = str(self.d / "proposals")
        self.audit = str(self.d / "audit.jsonl")
        self.paths = {}
        for name, text in (files or {}).items():
            # A name carrying its own suffix is written verbatim, so a case can
            # ship a domain check pack (`domain_checks.py`) alongside its ingest
            # files. Bare names get `.yaml`.
            fname = name if Path(name).suffix else f"{name}.yaml"
            p = self.d / fname
            p.write_text(text if isinstance(text, str) else yaml.safe_dump(text))
            self.paths[name] = str(p)
            self.paths.setdefault(Path(name).stem, str(p))
        self.default_schema = default_schema

    def schema_path(self, name: str | None) -> str:
        name = name or self.default_schema
        if name in self.paths:
            return self.paths[name]
        p = Path(name)
        return str(p if p.is_absolute() else REPO_ROOT / p)

    def file_path(self, name: str) -> str:
        if name in self.paths:
            return self.paths[name]
        raise CaseFailure(f"case refers to file {name!r}, which it never declares under `files:`")


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _step_ingest(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    result = ingest(
        env.file_path(spec["file"]), env.schema_path(spec.get("schema")), env.db,
        apply=bool(spec.get("apply", False)), owner=spec.get("owner", "agent"),
        approve_all=bool(spec.get("approve_all", False)),
        proposal_dir=env.props, audit_log=env.audit)

    if "schema_invalid" in expect:
        _want(n, "schema_invalid", expect["schema_invalid"], bool(result.get("schema_invalid")))
    if "schema_errors_contain" in expect:
        _want_in(n, "schema errors", expect["schema_errors_contain"],
                 " ".join(result.get("errors", [])))
    if "halted" in expect:
        _want(n, "halted", expect["halted"], bool(result.get("halted")))
    if "halt_reason_contains" in expect:
        _want_in(n, "halt reason", expect["halt_reason_contains"],
                 " ".join(f.message for f in result.get("findings", [])))

    if "items" in expect:
        if "plans" not in result:
            raise CaseFailure(f"step {n}: expected per-item verdicts but the run produced no "
                              f"plan (halted={result.get('halted')}, "
                              f"schema_invalid={result.get('schema_invalid')})")
        actual = {p.id: p.action for p in result["plans"]}
        for _id, want_action in expect["items"].items():
            if _id not in actual:
                raise CaseFailure(f"step {n}: no item {_id!r} in the plan "
                                  f"(saw {sorted(actual)})")
            _want(n, f"action for {_id}", want_action, actual[_id])

    if "checks_fired" in expect:
        fired = {f.check for f in result.get("findings", [])}
        for name in expect["checks_fired"]:
            if name not in fired:
                raise CaseFailure(f"step {n}: check {name!r} did not fire "
                                  f"(fired: {sorted(fired)})")
    if "message_contains" in expect:
        _want_in(n, "a finding message", expect["message_contains"],
                 " ".join(f.message for f in result.get("findings", [])))
    if "detail_contains" in expect:
        _want_in(n, "a plan detail", expect["detail_contains"],
                 " ".join(p.detail for p in result.get("plans", [])))
    if "applied" in expect:
        got = result.get("applied") or {}
        for k, v in expect["applied"].items():
            _want(n, f"applied.{k}", v, got.get(k))
    if "gap_slugs" in expect:
        _want(n, "recorded gap slugs", sorted(expect["gap_slugs"]),
              sorted((result.get("gaps") or {}).get("slugs", [])))
    return {"result": result}


def _step_approve(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    schema = Schema.load(env.schema_path(spec.get("schema")))
    chash = proposals.content_hash(env.file_path(spec["file"]))
    want_schema = schema.fingerprint() if spec.get("bind_schema", True) else None

    candidates = proposals.find_all_for_hash(env.props, chash, want_schema)
    distinct = {p.get("schema_hash", "") for p in candidates}
    if expect.get("ambiguous"):
        _want(n, "distinct governance contracts for this file", True, len(distinct) > 1)
        return {}
    if not candidates:
        if expect.get("proposal_found") is False:
            return {}
        raise CaseFailure(f"step {n}: no proposal found for this file under this schema")
    prop = candidates[0]

    items = list(spec.get("items", []))
    _, unmatched = proposals.resolve_ids_reporting(prop, items)
    prop, granted = proposals.record_approval(
        prop, items, approver=spec.get("approver", "tester"),
        approve_all=bool(spec.get("all", False)))
    proposals.save(env.props, prop)
    audit.append(env.audit, {"event": "approve", "run_id": prop["run_id"],
                             "approver": spec.get("approver", "tester"), "items": granted,
                             "schema_hash": prop.get("schema_hash", ""),
                             "content_hash": prop.get("content_hash", "")})

    if "granted" in expect:
        _want(n, "granted items", sorted(expect["granted"]), sorted(granted))
    if "unmatched" in expect:
        _want(n, "unmatched tokens", sorted(expect["unmatched"]),
              sorted(t for t, _ in unmatched))
    return {"granted": granted}


def _step_tamper(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    """Edit the audit log the way an attacker would, to prove detection."""
    p = Path(env.audit)
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    how = spec.get("how", "edit_payload")
    idx = int(spec.get("line", 1)) - 1
    if not lines:
        raise CaseFailure(f"step {n}: audit log is empty, nothing to tamper with")

    if how == "edit_payload":
        rec = json.loads(lines[idx])
        rec["owner"] = "someone-else"
        lines[idx] = json.dumps(rec)
    elif how == "drop":
        lines.pop(idx)
    elif how == "reorder":
        if len(lines) < 2:
            raise CaseFailure(f"step {n}: need >=2 entries to reorder")
        lines[0], lines[1] = lines[1], lines[0]
    elif how == "append_forged":
        rec = json.loads(lines[-1])
        rec["run_id"] = "forged-run"
        lines.append(json.dumps(rec))
    else:
        raise CaseFailure(f"step {n}: unknown tamper mode {how!r}")
    p.write_text("\n".join(lines) + "\n")
    return {}


def _step_verify_audit(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    res = audit.verify(env.audit)
    if "ok" in expect:
        _want(n, "audit chain ok", expect["ok"], res["ok"])
    if "reason_contains" in expect:
        _want_in(n, "audit reason", expect["reason_contains"], res["reason"])
    if "broken_at" in expect:
        _want(n, "first broken line", expect["broken_at"], res["broken_at"])
    return {"verify": res}


def _step_explain(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    schema = Schema.load(env.schema_path(spec.get("schema")))
    backend = open_backend(env.db)
    try:
        x = explain(backend, schema, spec["id"], scope=spec.get("scope", ""),
                    audit_log=env.audit,
                    version_limit=int(spec.get("version_limit", 20)))
    finally:
        backend.close()

    if "integrity" in expect:
        _want(n, "integrity state", expect["integrity"], x["integrity"]["state"])
    if "integrity_reason_contains" in expect:
        _want_in(n, "integrity reason", expect["integrity_reason_contains"],
                 x["integrity"]["reason"])
    if "exists" in expect:
        _want(n, "node exists", expect["exists"], x["current"] is not None)
    if "revision" in expect:
        _want(n, "current revision", expect["revision"], (x["current"] or {}).get("revision"))
    if "versions" in expect:
        _want(n, "prior-belief count", expect["versions"], len(x["versions"]))
    if "versions_truncated" in expect:
        _want(n, "versions truncated", expect["versions_truncated"],
              x["traversal"]["versions_truncated"])
    if "prior_claims" in expect:
        _want(n, "prior claims", list(expect["prior_claims"]),
              [v.get("claim") for v in x["versions"]])
    if "contradictions_active" in expect:
        _want(n, "active competing claims", expect["contradictions_active"],
              sum(1 for c in x["contradictions"] if c["state"] == "active"))
    if "contradicts_with" in expect:
        _want(n, "competing claim ids", sorted(expect["contradicts_with"]),
              sorted(c["with"] for c in x["contradictions"]))
    if "audit_entry_found" in expect:
        _want(n, "audit entry located", expect["audit_entry_found"],
              x.get("audit", {}).get("entry") is not None)
    if "gap_slug" in expect:
        _want(n, "gap slug", expect["gap_slug"], (x.get("gap") or {}).get("slug"))
    return {"explain": x}


def _step_contradictions(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    backend = open_backend(env.db)
    try:
        rows = backend.read_contradictions(spec.get("state", "active"))
    finally:
        backend.close()
    if "count" in expect:
        _want(n, "contradiction count", expect["count"], len(rows))
    if "detectors" in expect:
        _want(n, "detectors", sorted(expect["detectors"]),
              sorted({r["detector"] for r in rows}))
    if "pairs" in expect:
        got = sorted("|".join(sorted((r["left_id"], r["right_id"]))) for r in rows)
        _want(n, "pairs", sorted("|".join(sorted(p)) for p in expect["pairs"]), got)
    if "states" in expect:
        _want(n, "states", sorted(expect["states"]), sorted({r["state"] for r in rows}))
    if "snapshots_retained" in expect:
        missing = [r["pair_key"] for r in rows
                   if not (r.get("left_snapshot") and r.get("right_snapshot"))]
        _want(n, "rows retaining both content snapshots", [], missing)
    return {"rows": rows}


def _step_gaps(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    backend = open_backend(env.db)
    try:
        rows = backend.read_gap_rows()
        res = backend.read_gap_resolutions()
    finally:
        backend.close()
    ranked = gapmod.aggregate(rows, res)
    floor = int(spec.get("floor", gapmod.DEFAULT_PROMOTION_FLOOR))

    if "count" in expect:
        _want(n, "distinct gaps", expect["count"], len(ranked))
    if "slugs" in expect:
        _want(n, "gap slugs", sorted(expect["slugs"]), sorted(g.slug for g in ranked))
    if "promotable" in expect:
        _want(n, "promotable gaps", sorted(expect["promotable"]),
              sorted(g.slug for g in ranked if g.promotable(floor)))
    if "independent_scopes" in expect:
        got = {g.slug: g.independent for g in ranked}
        for slug, want in expect["independent_scopes"].items():
            _want(n, f"independent scopes for {slug}", want, got.get(slug))
    if "known_gaps_slugs" in expect:
        _want(n, "known_gaps handed to a writer", sorted(expect["known_gaps_slugs"]),
              sorted(g["slug"] for g in gapmod.known_gaps(ranked)))
    return {"gaps": ranked}


def _step_validate_schema(env: CaseEnv, spec: dict, expect: dict, n: int) -> dict:
    errs = validate_schema(Schema.load(env.schema_path(spec.get("schema"))))
    if "valid" in expect:
        _want(n, "schema valid", expect["valid"], errs == [])
    if "errors_contain" in expect:
        _want_in(n, "schema errors", expect["errors_contain"], " ".join(errs))
    return {"errors": errs}


HANDLERS = {
    "ingest": _step_ingest,
    "approve": _step_approve,
    "tamper": _step_tamper,
    "verify_audit": _step_verify_audit,
    "explain": _step_explain,
    "contradictions": _step_contradictions,
    "gaps": _step_gaps,
    "validate_schema": _step_validate_schema,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_case(path: Path) -> Result:
    spec = yaml.safe_load(path.read_text()) or {}
    name = spec.get("case", path.stem)
    guarantee = spec.get("guarantee", "unclassified")
    title = spec.get("title", "")
    steps = spec.get("steps", []) or []

    workdir = Path(tempfile.mkdtemp(prefix=f"kgg-conf-{name}-"))
    try:
        env = CaseEnv(workdir, spec.get("files", {}), spec.get("schema", DEFAULT_SCHEMA))
        for i, step in enumerate(steps, start=1):
            expect = step.get("expect", {}) or {}
            verb = next((k for k in step if k in HANDLERS), None)
            if verb is None:
                raise CaseFailure(
                    f"step {i}: no known operation (expected one of {sorted(HANDLERS)})")
            payload = step[verb]
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise CaseFailure(f"step {i}: '{verb}' must be a mapping")
            HANDLERS[verb](env, payload, expect, i)
        return Result(name, guarantee, title, True, steps=len(steps))
    except CaseFailure as exc:
        return Result(name, guarantee, title, False, str(exc), steps=len(steps))
    except Exception as exc:                                   # noqa: BLE001
        return Result(name, guarantee, title, False,
                      f"unexpected {type(exc).__name__}: {exc}\n"
                      + traceback.format_exc(limit=4), steps=len(steps))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_all(case_dir: Path | str = DEFAULT_CASE_DIR, only: str = "") -> list[Result]:
    d = Path(case_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.yaml")):
        if only and only not in p.stem:
            continue
        out.append(run_case(p))
    return out


def render(results: list[Result], *, verbose: bool = False) -> str:
    by_g: dict = {}
    for r in results:
        by_g.setdefault(r.guarantee, []).append(r)

    L = ["\n=== kgg conformance ==="]
    for g in sorted(by_g):
        rs = by_g[g]
        passed = sum(1 for r in rs if r.ok)
        mark = "✓" if passed == len(rs) else "✗"
        L.append(f"\n  {mark} {g}  ({passed}/{len(rs)})")
        for r in rs:
            L.append(f"      {'✓' if r.ok else '✗'} {r.case}")
            if r.title and (verbose or not r.ok):
                L.append(f"           {r.title}")
            if not r.ok:
                for line in r.detail.strip().splitlines():
                    L.append(f"           {line}")
    total, passed = len(results), sum(1 for r in results if r.ok)
    L.append(f"\n  {passed}/{total} cases pass across {len(by_g)} guarantees.")
    if passed != total:
        L.append("  A failing case means a documented guarantee no longer holds.")
    return "\n".join(L)


def markdown_table(results: list[Result]) -> str:
    """The block the README publishes — claims next to their proof."""
    by_g: dict = {}
    for r in results:
        by_g.setdefault(r.guarantee, []).append(r)
    L = ["| Guarantee | Cases | Status |", "|---|---:|:---:|"]
    for g in sorted(by_g):
        rs = by_g[g]
        passed = sum(1 for r in rs if r.ok)
        L.append(f"| `{g}` | {len(rs)} | {'✅' if passed == len(rs) else f'❌ {passed}/{len(rs)}'} |")
    return "\n".join(L)
