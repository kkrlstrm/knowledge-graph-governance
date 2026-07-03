"""kgg command-line interface: ingest / approve / verify-audit."""
from __future__ import annotations

import argparse
import sys

from . import audit, proposals, provenance
from .model import Schema, validate_schema
from .verdicts import Verdict, RUN
from .checks import CREATE, UNCHANGED, SUPERSEDE, CREATE_UNMAPPED, HELD, BLOCKED
from .engine import ingest

_GLYPH = {CREATE: "＋", SUPERSEDE: "↻", CREATE_UNMAPPED: "?", UNCHANGED: "=",
          HELD: "⏸", BLOCKED: "✗"}
_ORDER = [CREATE, SUPERSEDE, CREATE_UNMAPPED, UNCHANGED, HELD, BLOCKED]


def _print_plan(result):
    ctx, plans, findings = result["ctx"], result["plans"], result["findings"]
    print("\n=== kgg plan ===")
    print(f"  schema={ctx.schema.name}  scope={ctx.scope}  source={ctx.source}")
    print(f"  owner={ctx.owner}  run_id={ctx.run_id}")
    run_findings = [f for f in findings if f.target == RUN]
    if run_findings:
        print("\n  Run-level findings:")
        for f in run_findings:
            print(f"    {f.verdict.glyph} [{f.verdict.label}] {f.check}: {f.message}")
    buckets = {}
    for p in plans:
        buckets.setdefault(p.action, []).append(p)
    print()
    for action in _ORDER:
        items = buckets.get(action, [])
        if not items:
            continue
        print(f"  {_GLYPH[action]} {action.upper()} ({len(items)}):")
        for p in items:
            extra = ""
            if action in (HELD, BLOCKED):
                msgs = "; ".join(f.message for f in p.findings if f.verdict >= Verdict.REQUIRE_APPROVAL)
                extra = f"  — {msgs}" if msgs else ""
            print(f"      {p.verdict.glyph} {p.kind}:{p.key}{extra}")
    print("\n  " + "  ".join(f"{a}={len(buckets.get(a, []))}" for a in _ORDER))


def cmd_ingest(argv):
    ap = argparse.ArgumentParser(prog="kgg ingest")
    ap.add_argument("file", help="ingest YAML")
    ap.add_argument("--schema", required=True, help="path to schema.yaml")
    ap.add_argument("--backend", default="sqlite:./kgg.db",
                    help="backend DSN: sqlite:<path> or neo4j:<uri> (default sqlite:./kgg.db)")
    ap.add_argument("--apply", action="store_true", help="write ALLOW + approved items")
    ap.add_argument("--owner", default=provenance.default_owner())
    ap.add_argument("--approve-all", action="store_true",
                    help="(trusted runs) treat all REQUIRE_APPROVAL items as approved")
    ap.add_argument("--proposals", default="./proposals", help="proposal artifact dir")
    ap.add_argument("--audit", default="./audit.jsonl", help="hash-chained audit log")
    args = ap.parse_args(argv)

    result = ingest(args.file, args.schema, args.backend, apply=args.apply,
                    owner=args.owner, approve_all=args.approve_all,
                    proposal_dir=args.proposals, audit_log=args.audit)

    if result.get("schema_invalid"):
        print("⛔ SCHEMA INVALID — fix the governance contract before ingesting:")
        for e in result["errors"]:
            print(f"    ✗ {e}")
        sys.exit(3)

    if result.get("halted") and "plans" not in result:
        print("⛔ HALT — file rejected before any graph read:")
        for f in result["findings"]:
            print(f"    {f.check}: {f.message}")
        sys.exit(2)

    _print_plan(result)
    if result["halted"]:
        print("\n⛔ HALT — aborting before write.")
        sys.exit(2)

    if not args.apply:
        held = [p for p in result["plans"] if p.action == HELD]
        print(f"\nDRY RUN — proposal saved: {args.proposals}/{result['run_id']}.json")
        if held:
            ids = ",".join(p.id for p in held)
            print(f"  {len(held)} item(s) need approval:")
            print(f"    kgg approve {result['run_id']} --items {ids} --approver <you> --proposals {args.proposals} --audit {args.audit}")
            print(f"    kgg ingest {args.file} --schema {args.schema} --backend {args.backend} --apply")
        else:
            print(f"  To apply:  kgg ingest {args.file} --schema {args.schema} --backend {args.backend} --apply")
        return

    s = result["applied"]
    print(f"\nAPPLIED  ＋{s['created']} created  ↻{s['superseded']} superseded  "
          f"?{s['unmapped']} unmapped  ={s['unchanged']} unchanged  "
          f"⏸{s['held']} held  ✗{s['blocked']} blocked")
    print(f"  audit: chained entry {result['audit_entry']['hash'][:12]}… -> {args.audit}")
    print(f"  proposal status: {result['proposal']['status']}")
    if s["held"]:
        print(f"  {s['held']} item(s) still held — approve, then re-run --apply.")


def cmd_approve(argv):
    ap = argparse.ArgumentParser(prog="kgg approve")
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--file", help="locate the pending proposal by this ingest file")
    ap.add_argument("--items", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--approver", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--proposals", default="./proposals")
    ap.add_argument("--audit", default="./audit.jsonl")
    args = ap.parse_args(argv)

    proposal = None
    if args.run_id:
        proposal = proposals.load(args.proposals, args.run_id)
    elif args.file:
        proposal = proposals.find_for_hash(args.proposals, proposals.content_hash(args.file))
    if not proposal:
        sys.exit("ERROR: proposal not found (bad run_id, or file edited since dry-run).")

    items = [t.strip() for t in args.items.split(",") if t.strip()]
    proposal, granted = proposals.record_approval(
        proposal, items, approver=args.approver, note=args.note, approve_all=args.all)
    proposals.save(args.proposals, proposal)
    audit.append(args.audit, {"event": "approve", "run_id": proposal["run_id"],
                              "approver": args.approver, "items": granted, "note": args.note})
    print(f"✓ Approved {len(granted)} item(s) on {proposal['run_id']}: "
          f"{', '.join(granted) or '(none matched a held item)'}")
    print(f"  Now re-run:  kgg ingest {proposal['file']} --schema <schema> --backend <dsn> --apply")


def cmd_validate_schema(argv):
    ap = argparse.ArgumentParser(prog="kgg validate-schema")
    ap.add_argument("--schema", required=True)
    args = ap.parse_args(argv)
    schema = Schema.load(args.schema)
    errs = validate_schema(schema)
    if errs:
        print(f"✗ schema invalid ({len(errs)} problem(s)):")
        for e in errs:
            print(f"    ✗ {e}")
        sys.exit(1)
    kinds = ", ".join(schema.kinds)
    print(f"✓ schema '{schema.name}' valid — identity={schema.identity}, "
          f"kinds: {kinds}, fingerprint {schema.fingerprint()[:12]}…")


def cmd_verify_audit(argv):
    ap = argparse.ArgumentParser(prog="kgg verify-audit")
    ap.add_argument("--audit", default="./audit.jsonl")
    ap.add_argument("--tail", type=int, default=0)
    args = ap.parse_args(argv)
    res = audit.verify(args.audit)
    print(f"{'✓' if res['ok'] else '✗'} audit chain: {res['reason']} ({res['entries']} entries)")
    if res["ok"] and res["entries"]:
        print(f"  head: {audit.last_hash(args.audit)}   (anchor this externally for tamper-proofing)")
    if not res["ok"]:
        print(f"  first broken link at line {res['broken_at']}")
    for e in audit.tail(args.audit, args.tail):
        print(f"    {e.get('ts','?')}  {e.get('event','?')}  {e.get('run_id','')}  {e.get('hash','')[:12]}…")
    sys.exit(0 if res["ok"] else 1)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("kgg <command>\n\n  ingest <file> --schema S [--backend DSN] [--apply]\n"
              "  approve <run_id|--file F> --approver NAME [--items k:a,k:b | --all]\n"
              "  validate-schema --schema S\n"
              "  verify-audit [--audit PATH] [--tail N]\n")
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "ingest":
        cmd_ingest(rest)
    elif cmd == "approve":
        cmd_approve(rest)
    elif cmd == "validate-schema":
        cmd_validate_schema(rest)
    elif cmd == "verify-audit":
        cmd_verify_audit(rest)
    else:
        # allow `kgg <file> ...` as shorthand for ingest
        cmd_ingest(argv)


if __name__ == "__main__":
    main()
