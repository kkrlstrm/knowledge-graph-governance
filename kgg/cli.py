"""kgg command-line interface.

    write path   ingest · approve
    read path    explain · contradictions · gaps
    the contract validate-schema · verify-audit · conformance
"""
from __future__ import annotations

import argparse
import json
import sys

from . import audit, conformance, explain as explain_mod, gaps as gapmod, proposals, provenance
from .model import Schema, validate_schema
from .verdicts import Verdict, RUN
from .checks import CREATE, UNCHANGED, SUPERSEDE, CREATE_UNMAPPED, HELD, BLOCKED
from .engine import ingest
from .backends.base import open_backend, ReadSurfaceMissing

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
    g = result.get("gaps") or {}
    if g.get("slugs"):
        print(f"  gaps: recorded {g['recorded']} observation(s) — {', '.join(g['slugs'])}"
              f"   (kgg gaps)")
    c = result.get("contradictions") or {}
    if c.get("upserted") or c.get("resolved"):
        print(f"  contradictions: {c.get('upserted', 0)} recorded, "
              f"{c.get('resolved', 0)} resolved   (kgg contradictions)")
    print(f"  audit: chained entry {result['audit_entry']['hash'][:12]}… -> {args.audit}")
    print(f"  proposal status: {result['proposal']['status']}")
    if s["held"]:
        print(f"  {s['held']} item(s) still held — approve, then re-run --apply.")


def cmd_approve(argv):
    ap = argparse.ArgumentParser(prog="kgg approve")
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--file", help="locate the pending proposal by this ingest file")
    ap.add_argument("--schema", help="the governance contract the approval is granted under; "
                                     "required with --file when the file has proposals under "
                                     "more than one schema")
    ap.add_argument("--items", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--approver", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--proposals", default="./proposals")
    ap.add_argument("--audit", default="./audit.jsonl")
    args = ap.parse_args(argv)

    want_schema = None
    if args.schema:
        schema = Schema.load(args.schema)
        errs = validate_schema(schema)
        if errs:
            sys.exit(f"ERROR: schema invalid, refusing to bind an approval to it: {errs[0]}")
        want_schema = schema.fingerprint()

    proposal = None
    if args.run_id:
        proposal = proposals.load(args.proposals, args.run_id)
        if proposal and want_schema and proposal.get("schema_hash", "") != want_schema:
            sys.exit(f"ERROR: proposal {args.run_id} was built under a different governance "
                     f"contract than --schema {args.schema}.\n"
                     f"       Re-run the dry-run under the current schema, then approve.")
    elif args.file:
        # Bind on the SCHEMA as well as the content. Selecting by content alone
        # can attach the approval to a proposal built under a different contract;
        # `ingest` then filters on both hashes, finds no approvals, and leaves the
        # item held — an approval that succeeded and did nothing.
        candidates = proposals.find_all_for_hash(
            args.proposals, proposals.content_hash(args.file), want_schema)
        distinct = {p.get("schema_hash", "") for p in candidates}
        if len(distinct) > 1:
            sys.exit("ERROR: this ingest file has proposals under "
                     f"{len(distinct)} different governance contracts.\n"
                     "       Pass --schema <path> so the approval binds to one of them.")
        proposal = candidates[0] if candidates else None
    else:
        sys.exit("ERROR: give a run_id or --file.")

    if not proposal:
        sys.exit("ERROR: proposal not found (bad run_id, file edited since the dry-run, "
                 "or no proposal exists under this --schema).")

    items = [t.strip() for t in args.items.split(",") if t.strip()]
    if not items and not args.all:
        sys.exit("ERROR: nothing to approve — pass --items <id,...> or --all.")

    _, unmatched = proposals.resolve_ids_reporting(proposal, items)
    proposal, granted = proposals.record_approval(
        proposal, items, approver=args.approver, note=args.note, approve_all=args.all)
    proposals.save(args.proposals, proposal)
    audit.append(args.audit, {"event": "approve", "run_id": proposal["run_id"],
                              "approver": args.approver, "items": granted, "note": args.note,
                              "schema_hash": proposal.get("schema_hash", ""),
                              "content_hash": proposal.get("content_hash", ""),
                              "unmatched": [t for t, _ in unmatched]})
    print(f"✓ Approved {len(granted)} item(s) on {proposal['run_id']}: "
          f"{', '.join(granted) or '(none)'}")
    if unmatched:
        print(f"\n✗ {len(unmatched)} token(s) approved NOTHING:")
        for tok, why in unmatched:
            print(f"      {tok} — {why}")
    print(f"\n  Now re-run:  kgg ingest {proposal['file']} "
          f"--schema {args.schema or '<schema>'} --backend <dsn> --apply")
    if unmatched:
        sys.exit(1)


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
    for vname, vocab in schema.vocabularies.items():
        bits = [f"{len(vocab.terms)} terms"]
        if vocab.synonyms:
            bits.append(f"{len(vocab.synonyms)} aliases")
        dep = sum(1 for r in vocab.records.values() if r.status == "deprecated")
        if dep:
            bits.append(f"{dep} deprecated")
        ver = f" v{vocab.version}" if vocab.version else ""
        print(f"    vocabulary '{vname}'{ver}: {', '.join(bits)}")
    if schema.check_specs:
        from .plugins import load_check_registries
        reg = load_check_registries(schema.check_specs, base_dir=schema.base_dir)
        print(f"    domain checks ({len(reg)}): {', '.join(n for n, _ in reg)}")
    else:
        print("    domain checks: none declared (kernel rules only)")


def _open(dsn):
    try:
        return open_backend(dsn)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")


def cmd_explain(argv):
    ap = argparse.ArgumentParser(
        prog="kgg explain",
        description="Why does the graph believe this, and how far can that be trusted?")
    ap.add_argument("target", help="composite id: kind:key")
    ap.add_argument("--schema", required=True)
    ap.add_argument("--backend", default="sqlite:./kgg.db")
    ap.add_argument("--scope", default="", help="required for a SCOPED schema")
    ap.add_argument("--audit", default="./audit.jsonl")
    ap.add_argument("--versions", type=int, default=explain_mod.DEFAULT_VERSION_LIMIT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    schema = Schema.load(args.schema)
    backend = _open(args.backend)
    try:
        result = explain_mod.explain(backend, schema, args.target, scope=args.scope,
                                     audit_log=args.audit, version_limit=args.versions)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    finally:
        backend.close()

    print(json.dumps(result, indent=2, default=str) if args.json
          else explain_mod.render(result))
    # A read that could not verify is not a failure of the command, but it must
    # not exit 0 either — a script that greps for success would treat an
    # unverifiable node as a sound one.
    sys.exit({explain_mod.VERIFIED: 0, explain_mod.UNVERIFIED: 4,
              explain_mod.INVALIDATED: 5}.get(result["integrity"]["state"], 4))


def cmd_contradictions(argv):
    ap = argparse.ArgumentParser(
        prog="kgg contradictions",
        description="Competing active claims. Both sides are shown; neither wins.")
    ap.add_argument("--backend", default="sqlite:./kgg.db")
    ap.add_argument("--state", default="active", choices=["active", "resolved", "all"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    backend = _open(args.backend)
    try:
        rows = backend.read_contradictions(args.state)
    except ReadSurfaceMissing as exc:
        sys.exit(f"ERROR: {exc}")
    finally:
        backend.close()

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print(f"\n✓ no {args.state} contradictions.")
        return
    print(f"\n=== kgg contradictions ({args.state}: {len(rows)}) ===")
    print("  Advisory evidence. Both claims keep their normal status; the ledger")
    print("  records that sources disagree and does not choose between them.\n")
    for r in rows:
        mark = "●" if r["state"] == "active" else "○"
        print(f"  {mark} [{r['detector']}] {r['reason']}")
        print(f"      {r['left_id']}")
        print(f"          {r['left_snapshot']}")
        if r.get("left_source"):
            print(f"          source: {r['left_source']}   owner: {r.get('left_owner','')}")
        print(f"      {r['right_id']}")
        print(f"          {r['right_snapshot']}")
        if r.get("right_source"):
            print(f"          source: {r['right_source']}   owner: {r.get('right_owner','')}")
        if r["state"] == "resolved":
            print(f"      resolved {r.get('resolved_ts','')}: {r.get('resolution','')}")
        print()


def cmd_gaps(argv):
    ap = argparse.ArgumentParser(
        prog="kgg gaps",
        description="Named holes in the controlled vocabulary, ranked by independent evidence.")
    ap.add_argument("command", nargs="?", default="list",
                    choices=["list", "known", "promote", "reject"])
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--backend", default="sqlite:./kgg.db")
    ap.add_argument("--floor", type=int, default=gapmod.DEFAULT_PROMOTION_FLOOR,
                    help="independent scopes required to promote (default 2)")
    ap.add_argument("--term", help="the canonical term this gap becomes")
    ap.add_argument("--category", default="", help="vocabulary category to file it under")
    ap.add_argument("--definition", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--actor", default=provenance.default_owner())
    ap.add_argument("--apply", action="store_true",
                    help="record the promotion (you still edit the vocabulary file)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    backend = _open(args.backend)
    try:
        rows = backend.read_gap_rows()
        resolutions = backend.read_gap_resolutions()
        ranked = gapmod.aggregate(rows, resolutions)

        if args.command == "known":
            # The list to inject into an extraction prompt. Convergence on a
            # closed set is a lookup; on an open set it is telepathy.
            payload = gapmod.known_gaps(ranked)
            print(json.dumps(payload, indent=2) if args.json
                  else "\n".join(f"{g['slug']}: {g['means']}" for g in payload)
                  or "(no open gaps)")
            return

        if args.command in ("promote", "reject"):
            if not args.slug:
                sys.exit(f"ERROR: `kgg gaps {args.command}` needs a slug.")
            gap = next((g for g in ranked if g.slug == args.slug), None)
            if gap is None:
                sys.exit(f"ERROR: no gap named {args.slug!r}.")

            if args.command == "reject":
                if not args.apply:
                    print(f"Would mark '{gap.slug}' rejected ({args.note or 'no note'}). "
                          f"Re-run with --apply.")
                    return
                backend.set_gap_resolution(gap.slug, "rejected", note=args.note,
                                           actor=args.actor)
                print(f"✓ '{gap.slug}' marked rejected — it will stop appearing in "
                      f"`kgg gaps known`.")
                return

            if not args.term:
                sys.exit("ERROR: promoting needs --term (the canonical term it becomes).")
            if not gap.promotable(args.floor):
                sys.exit(f"ERROR: '{gap.slug}' has {gap.independent} independent scope(s), "
                         f"below the floor of {args.floor}.\n"
                         f"       Scopes: {', '.join(gap.scopes) or '(none)'}\n"
                         f"       Independence, not observation count, is the bar: three "
                         f"observations from one scope is one source with a loud voice.")

            block = gapmod.promotion_block(gap, args.term, args.category,
                                           args.definition or gap.means)
            print(f"\n=== promote '{gap.slug}' -> '{args.term}' ===")
            print(f"  evidence: {gap.independent} independent scopes "
                  f"({', '.join(gap.scopes)}), {gap.observations} observations")
            print("\n  Add to your vocabulary file:\n")
            cat = args.category or "<category>"
            print(f"    {cat}:")
            print(f"      {args.term}:")
            print(f"        definition: \"{block['entry']['definition']}\"")
            print(f"        promoted_from_gap: {gap.slug}")
            print("\n  Then bump the vocabulary `version`. Editing it changes the schema")
            print("  fingerprint, which invalidates approvals granted under the old")
            print("  vocabulary — a promotion is a policy change and gets re-reviewed.")
            if not args.apply:
                print(f"\n  Nothing recorded. Re-run with --apply once the term is in the file.")
                return
            backend.set_gap_resolution(gap.slug, "promoted", promoted_to=args.term,
                                       note=args.note, actor=args.actor)
            print(f"\n✓ '{gap.slug}' recorded as promoted to '{args.term}'.")
            return

        # list
        if args.json:
            print(json.dumps([g.to_dict() for g in ranked], indent=2))
            return
        if not ranked:
            print("\n  no gaps recorded — nothing has landed unmapped yet.")
            return
        print(f"\n=== kgg gaps ({len(ranked)}) — ranked by INDEPENDENT scopes ===")
        print(f"  Promotion floor: {args.floor} independent scopes. Observation count is")
        print("  not the bar — three observations from one scope is one loud source.\n")
        for g in ranked:
            mark = "▲" if g.promotable(args.floor) else ("✓" if g.status == "promoted" else " ")
            print(f"  {mark} {g.slug}   {g.independent} scope(s), {g.observations} obs"
                  + (f"   [{g.status}"
                     + (f" -> {g.promoted_to}" if g.promoted_to else "") + "]"
                     if g.status != "open" else ""))
            if g.means:
                print(f"        {g.means}")
            print(f"        scopes: {', '.join(g.scopes) or '(none)'}")
        promotable = [g for g in ranked if g.promotable(args.floor)]
        if promotable:
            print(f"\n  {len(promotable)} gap(s) ready to propose as vocabulary terms:")
            for g in promotable:
                print(f"      kgg gaps promote {g.slug} --term <new.term> --category <cat>")
    except ReadSurfaceMissing as exc:
        sys.exit(f"ERROR: {exc}")
    finally:
        backend.close()


def cmd_conformance(argv):
    ap = argparse.ArgumentParser(
        prog="kgg conformance",
        description="Run every documented guarantee as an executable refusal.")
    ap.add_argument("--dir", default=str(conformance.DEFAULT_CASE_DIR))
    ap.add_argument("--case", default="", help="substring filter on the case file name")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", action="store_true", help="the table the README publishes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    results = conformance.run_all(args.dir, only=args.case)
    if not results:
        sys.exit(f"ERROR: no conformance cases found in {args.dir}")
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    elif args.markdown:
        print(conformance.markdown_table(results))
    else:
        print(conformance.render(results, verbose=args.verbose))
    sys.exit(0 if all(r.ok for r in results) else 1)


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


_USAGE = """kgg <command>

  write path
    ingest <file> --schema S [--backend DSN] [--apply]
    approve <run_id|--file F> --approver NAME [--schema S] [--items k:a,k:b | --all]

  read path
    explain <kind:key> --schema S [--backend DSN] [--audit PATH] [--json]
        why the graph believes this, and whether that can be verified
    contradictions [--backend DSN] [--state active|resolved|all]
        competing active claims — both sides shown, neither chosen
    gaps [list|known|promote <slug>|reject <slug>] [--backend DSN] [--floor N]
        named holes in the vocabulary, ranked by INDEPENDENT scopes

  the contract
    validate-schema --schema S
    verify-audit [--audit PATH] [--tail N]
    conformance [--dir D] [--case SUBSTR] [--markdown]
        every documented guarantee, run as an executable refusal
"""

_COMMANDS = {
    "ingest": cmd_ingest,
    "approve": cmd_approve,
    "explain": cmd_explain,
    "contradictions": cmd_contradictions,
    "gaps": cmd_gaps,
    "validate-schema": cmd_validate_schema,
    "verify-audit": cmd_verify_audit,
    "conformance": cmd_conformance,
}


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(_USAGE)
        return
    cmd, rest = argv[0], argv[1:]
    handler = _COMMANDS.get(cmd)
    if handler:
        handler(rest)
    else:
        # allow `kgg <file> ...` as shorthand for ingest
        cmd_ingest(argv)


if __name__ == "__main__":
    main()
