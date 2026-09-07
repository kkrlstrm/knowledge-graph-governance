"""Ingest orchestration — wires the kernel, checks, and a backend together.

    load ingest YAML
      -> structural HALT gate (source + date + a body)
    load + validate the schema (the governance contract itself)
    load the schema's declared domain check registries        [plugins]
    read existing nodes from the backend (scope-filtered in a SCOPED schema)
      -> build a PlanContext -> normalise vocab -> run named checks
      -> graded findings
    resolve per-item actions (create / unchanged / supersede / hold / block)
      -> write a staged proposal (bound to content_hash AND schema_hash)
    on apply, inside one transaction:
      -> stamp provenance, create (fail-closed) / supersede / link
      -> record gap observations for anything that landed unmapped
      -> reconcile the contradiction ledger against the post-write graph
    on commit:
      -> append one hash-chained audit entry  (never before the writes land)
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import audit, contradictions, gaps, proposals, provenance
from .model import Schema, validate_schema
from .plugins import load_check_registries
from .verdicts import Finding, Verdict, RUN, run_halts
from .checks import (
    PlanContext, run_checks, resolve_actions,
    CREATE, UNCHANGED, SUPERSEDE, CREATE_UNMAPPED, HELD, BLOCKED,
)
from .backends.base import Backend, open_backend, GLOBAL_PARTITION, ReadSurfaceMissing

_SECTION_ORDER = {"declare": 0, "node": 1, "unmapped": 2}
_CONTROL_KEYS = {"supersede", "protected"}


def load_ingest(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def structural_halt(data) -> list[Finding]:
    out = []
    if not isinstance(data, dict):
        return [Finding("parse", RUN, Verdict.HALT, "file is not a YAML mapping")]
    for k in ("source", "date"):
        if not data.get(k):
            out.append(Finding("top_level_required", RUN, Verdict.HALT,
                               f"missing required top-level field: {k}"))
    if not (data.get("nodes") or data.get("declare") or data.get("unmapped_for_review")):
        out.append(Finding("top_level_required", RUN, Verdict.HALT,
                           "file has no nodes/declare/unmapped_for_review to ingest"))
    return out


def _scope_of(data: dict, schema: Schema) -> str:
    return data.get("scope") or data.get(schema.scope_label) or ""


def build_context(data: dict, schema: Schema, existing: dict, owner: str, run_id: str) -> PlanContext:
    return PlanContext(
        schema=schema, scope=_scope_of(data, schema), source=data.get("source", ""),
        date=str(data.get("date", "")), owner=owner, run_id=run_id,
        nodes=data.get("nodes") or {},
        declared=data.get("declare") or {},
        unmapped=data.get("unmapped_for_review") or [],
        existing=existing,
    )


def _props(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in _CONTROL_KEYS}


def _link_refs(backend: Backend, ctx: PlanContext, plan, identity_scope: str) -> None:
    nk = ctx.schema.kind(plan.kind)
    if not nk:
        return
    for fieldname, target_kind in nk.refs.items():
        vals = plan.item.get(fieldname)
        if vals is None:
            continue
        if not isinstance(vals, list):
            vals = [vals]
        for v in vals:
            refkey = v.get("key") if isinstance(v, dict) else v
            backend.link(plan.kind, plan.key, fieldname, target_kind, refkey, identity_scope)


def apply_plans(backend: Backend, ctx: PlanContext, plans: list, identity_scope: str) -> dict:
    """Perform the writes. Caller wraps this in backend.transaction()."""
    summary = {"created": 0, "superseded": 0, "unmapped": 0, "unchanged": 0,
               "held": 0, "blocked": 0}
    actions = []
    for plan in sorted(plans, key=lambda p: _SECTION_ORDER.get(p.section, 1)):
        nk = ctx.schema.kind(plan.kind)
        prov = provenance.stamp(ctx.owner, ctx.run_id, ctx.source,
                                is_protected=bool(plan.item.get("protected", False)))
        if plan.action in (HELD, BLOCKED):
            summary["held" if plan.action == HELD else "blocked"] += 1
            continue
        if plan.action == UNCHANGED:
            summary["unchanged"] += 1
            continue
        if plan.action == SUPERSEDE:
            rev = backend.supersede(nk, plan.key, _props(plan.item), prov, ctx.run_id, identity_scope)
            _link_refs(backend, ctx, plan, identity_scope)
            summary["superseded"] += 1
            actions.append({"action": "supersede", "id": plan.id, "revision": rev})
            continue
        status = "unmapped" if plan.action == CREATE_UNMAPPED else "active"
        backend.create(nk, plan.key, _props(plan.item), prov, status, identity_scope)
        _link_refs(backend, ctx, plan, identity_scope)
        summary["unmapped" if plan.action == CREATE_UNMAPPED else "created"] += 1
        actions.append({"action": plan.action, "id": plan.id})
    summary["actions"] = actions
    return summary


def record_gap_observations(backend: Backend, ctx: PlanContext, plans: list) -> dict:
    """Log the gaps this run's unmapped writes named.

    Best-effort by design: a backend without a gap registry still governs writes
    correctly, and refusing the whole apply because the registry is unavailable
    would trade a working gate for a bookkeeping feature.
    """
    rows = gaps.observations_from_plans(ctx, plans)
    if not rows:
        return {"recorded": 0, "slugs": []}
    try:
        n = backend.record_gaps(rows)
    except ReadSurfaceMissing:
        return {"recorded": 0, "slugs": [], "skipped": "backend has no gap registry"}
    return {"recorded": n, "slugs": sorted({r["slug"] for r in rows})}


def reconcile_contradictions(backend: Backend, schema: Schema, scope: str,
                             run_id: str) -> dict:
    """Re-derive the contradiction ledger from the graph as it now stands.

    Runs after the writes, inside the same transaction, so the ledger can never
    describe a graph that was rolled back. Idempotent: an unchanged graph
    produces no ledger changes.
    """
    try:
        nodes = backend.read_nodes(schema, scope)
        stored = backend.read_contradictions("all")
    except ReadSurfaceMissing:
        return {"skipped": "backend has no contradiction ledger"}
    plan = contradictions.reconcile(schema, nodes, stored, run_id=run_id)
    return backend.write_contradictions(
        plan["upsert"], plan["resolve"], plan["reactivate"])


def ingest(file: str, schema_path: str, dsn: str, *, apply: bool, owner: str,
           approve_all: bool, proposal_dir: str, audit_log: str) -> dict:
    data = load_ingest(file)
    run_id = proposals.new_run_id(file)

    halts = structural_halt(data)
    if run_halts(halts):
        return {"halted": True, "findings": halts, "run_id": run_id}

    schema = Schema.load(schema_path)
    schema_errs = validate_schema(schema)
    if schema_errs:
        return {"schema_invalid": True, "errors": schema_errs, "run_id": run_id}
    schema_hash = schema.fingerprint()
    domain_checks = load_check_registries(schema.check_specs, base_dir=schema.base_dir)

    backend = open_backend(dsn)
    try:
        scope = _scope_of(data, schema)
        identity_scope = scope if schema.scoped else GLOBAL_PARTITION
        existing = backend.read_existing(schema, scope)
        ctx = build_context(data, schema, existing, owner, run_id)
        findings = halts + run_checks(ctx, extra=domain_checks)

        chash = proposals.content_hash(file)
        prior = proposals.find_for_hash(proposal_dir, chash, schema_hash)
        approvals = proposals.approved_ids(prior)
        plans = resolve_actions(ctx, findings, approvals=approvals, approve_all=approve_all)

        proposal = proposals.build(run_id, file, owner, ctx.scope, plans, findings,
                                   schema_hash=schema_hash, identity=schema.identity,
                                   checks=[n for n, _ in domain_checks])
        if prior:
            proposal["approvals"] = prior.get("approvals", [])
        proposals.save(proposal_dir, proposal)

        result = {"halted": run_halts(findings), "findings": findings, "plans": plans,
                  "ctx": ctx, "run_id": run_id, "proposal": proposal, "applied": None}
        if result["halted"] or not apply:
            return result

        with backend.transaction():
            summary = apply_plans(backend, ctx, plans, identity_scope)
            gap_summary = record_gap_observations(backend, ctx, plans)
            contra_summary = reconcile_contradictions(backend, schema, scope, run_id)
        # Audit is written only after the transaction commits — a rolled-back
        # apply leaves neither graph writes nor an audit entry.
        entry = audit.append(audit_log, {
            "event": "apply", "run_id": run_id, "file": file, "owner": owner,
            "scope": ctx.scope, "identity": schema.identity, "content_hash": chash,
            "schema_hash": schema_hash,
            "summary": {k: v for k, v in summary.items() if k != "actions"},
            "gaps": gap_summary, "contradictions": contra_summary,
            "actions": summary.get("actions", []),
        })
        proposal = proposals.mark_applied(proposal, {k: v for k, v in summary.items() if k != "actions"})
        proposals.save(proposal_dir, proposal)
        result["applied"] = summary
        result["gaps"] = gap_summary
        result["contradictions"] = contra_summary
        result["audit_entry"] = entry
        return result
    finally:
        backend.close()
