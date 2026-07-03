"""Ingest orchestration — wires the kernel, checks, and a backend together.

    load ingest YAML
      -> structural HALT gate (source + date + a body)
    read existing nodes from the backend
      -> build a PlanContext
      -> run the named-check registry -> graded findings
    resolve per-item actions (create / unchanged / supersede / hold / block)
      -> write a staged proposal artifact
    on apply, for each ALLOW or approved item:
      -> stamp provenance, create/supersede via the backend, link refs
      -> append one hash-chained audit entry
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import audit, proposals, provenance
from .model import Schema
from .verdicts import Finding, Verdict, RUN, run_halts
from .checks import (
    PlanContext, run_checks, resolve_actions,
    CREATE, UNCHANGED, SUPERSEDE, CREATE_UNMAPPED, HELD, BLOCKED,
)
from .backends.base import Backend, open_backend

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


def build_context(data: dict, schema: Schema, existing: dict, owner: str, run_id: str) -> PlanContext:
    scope = data.get("scope") or data.get(schema.scope_label) or ""
    return PlanContext(
        schema=schema, scope=scope, source=data.get("source", ""),
        date=str(data.get("date", "")), owner=owner, run_id=run_id,
        nodes=data.get("nodes") or {},
        declared=data.get("declare") or {},
        unmapped=data.get("unmapped_for_review") or [],
        existing=existing,
    )


def _props(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in _CONTROL_KEYS}


def _link_refs(backend: Backend, ctx: PlanContext, plan) -> None:
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
            backend.link(plan.kind, plan.key, fieldname, target_kind, refkey)


def apply_plans(backend: Backend, ctx: PlanContext, plans: list) -> dict:
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
            rev = backend.supersede(nk, plan.key, _props(plan.item), prov, ctx.run_id)
            _link_refs(backend, ctx, plan)
            summary["superseded"] += 1
            actions.append({"action": "supersede", "kind": plan.kind, "key": plan.key, "revision": rev})
            continue
        status = "unmapped" if plan.action == CREATE_UNMAPPED else "active"
        backend.create(nk, plan.key, _props(plan.item), prov, status)
        _link_refs(backend, ctx, plan)
        if plan.action == CREATE_UNMAPPED:
            summary["unmapped"] += 1
        else:
            summary["created"] += 1
        actions.append({"action": plan.action, "kind": plan.kind, "key": plan.key})
    summary["actions"] = actions
    return summary


def ingest(file: str, schema_path: str, dsn: str, *, apply: bool, owner: str,
           approve_all: bool, proposal_dir: str, audit_log: str) -> dict:
    """Run the full pipeline. Returns a result dict for the CLI to render."""
    data = load_ingest(file)
    halts = structural_halt(data)
    run_id = proposals.new_run_id(file)
    if run_halts(halts):
        return {"halted": True, "findings": halts, "run_id": run_id}

    schema = Schema.load(schema_path)
    backend = open_backend(dsn)
    try:
        existing = backend.read_existing(schema)
        ctx = build_context(data, schema, existing, owner, run_id)
        findings = halts + run_checks(ctx)

        chash = proposals.content_hash(file)
        prior = proposals.find_for_hash(proposal_dir, chash)
        approvals = proposals.approved_keys(prior)
        plans = resolve_actions(ctx, findings, approvals=approvals, approve_all=approve_all)

        proposal = proposals.build(run_id, file, owner, ctx.scope, plans, findings)
        if prior:
            proposal["approvals"] = prior.get("approvals", [])
        proposals.save(proposal_dir, proposal)

        result = {"halted": run_halts(findings), "findings": findings, "plans": plans,
                  "ctx": ctx, "run_id": run_id, "proposal": proposal, "applied": None}
        if result["halted"] or not apply:
            return result

        summary = apply_plans(backend, ctx, plans)
        entry = audit.append(audit_log, {
            "event": "apply", "run_id": run_id, "file": file, "owner": owner,
            "scope": ctx.scope, "content_hash": chash,
            "summary": {k: v for k, v in summary.items() if k != "actions"},
            "actions": summary.get("actions", []),
        })
        proposal = proposals.mark_applied(proposal, {k: v for k, v in summary.items() if k != "actions"})
        proposals.save(proposal_dir, proposal)
        result["applied"] = summary
        result["audit_entry"] = entry
        return result
    finally:
        backend.close()
