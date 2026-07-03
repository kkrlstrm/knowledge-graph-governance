"""Named-check registry + action resolution — generic over a Schema.

Each check is a named function `(ctx) -> list[Finding]` emitting graded
verdicts. Every finding, plan, approval, and audit action is keyed by the
*composite* identity `iid(kind, key)` — never a bare key — so two kinds that
share a key can't bleed verdicts or approvals into each other.

The harder guarantees:
  collision          re-appearing (kind,key): identical -> no-op; changed ->
                     supersession (if supersedable) or a held conflict; never
                     a silent drop/overwrite
  refs_exist         a reference must resolve to an existing/declared node —
                     the no-implicit-create guarantee
  protected_guard    one owner can't overwrite another owner's protected node
  unmapped_review    an item with no controlled-vocabulary home is held, not
                     force-fit
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .model import Schema, NodeKind, ExistingNode, iid
from .verdicts import Finding, Verdict, RUN, item_verdict


# ---------------------------------------------------------------------------
# Plan context
# ---------------------------------------------------------------------------

@dataclass
class PlanContext:
    schema: Schema
    scope: str
    source: str
    date: str
    owner: str
    run_id: str
    nodes: dict                 # kind -> [item, ...]      (observations)
    declared: dict              # kind -> [item, ...]      (explicit prerequisite nodes)
    unmapped: list              # [item, ...] each carrying 'kind'
    existing: dict              # (kind, key) -> ExistingNode   (already scope-filtered)

    def key_of(self, kind: str, item: dict) -> str:
        nk = self.schema.kind(kind)
        if nk and nk.key in item:
            return item[nk.key]
        return item.get("key")

    def iid_of(self, kind: str, item: dict) -> str:
        return iid(kind, self.key_of(kind, item))

    def pending_keys(self, kind: str) -> set:
        ex = {k for (kd, k) in self.existing if kd == kind}
        dc = {self.key_of(kind, it) for it in self.declared.get(kind, [])}
        return ex | dc

    def sections(self):
        """(section, kind, item) over everything that can produce a write."""
        for kind, items in self.nodes.items():
            for it in items:
                yield ("node", kind, it)
        for kind, items in self.declared.items():
            for it in items:
                yield ("declare", kind, it)
        for it in self.unmapped:
            yield ("unmapped", it.get("kind", _default_kind(self.schema)), it)


def _default_kind(schema: Schema) -> str:
    for name, nk in schema.kinds.items():
        if nk.vocab_fields:
            return name
    return next(iter(schema.kinds), "node")


def _content_changed(item: dict, nk: NodeKind, ex: ExistingNode) -> bool:
    for f in nk.content_fields:
        new, old = item.get(f), ex.content.get(f)
        if isinstance(new, str) or isinstance(old, str):
            if str(new or "").strip() != str(old or "").strip():
                return True
        elif new != old:
            return True
    return False


# ---------------------------------------------------------------------------
# Checks  (targets are always iid(kind, key))
# ---------------------------------------------------------------------------

def check_date_format(ctx: PlanContext) -> list[Finding]:
    if not ctx.date:
        return []
    try:
        datetime.strptime(str(ctx.date), "%Y-%m-%d")
    except (ValueError, TypeError):
        return [Finding("date_format", RUN, Verdict.BLOCK,
                        f"date must be YYYY-MM-DD, got {ctx.date!r}")]
    return []


def check_unknown_kind(ctx: PlanContext) -> list[Finding]:
    out = []
    for section, kind, item in ctx.sections():
        if ctx.schema.kind(kind) is None:
            out.append(Finding("unknown_kind", ctx.iid_of(kind, item), Verdict.BLOCK,
                               f"node kind '{kind}' is not defined in the schema"))
    return out


def check_key_present(ctx: PlanContext) -> list[Finding]:
    out = []
    for section, kind, item in ctx.sections():
        if not ctx.key_of(kind, item):
            out.append(Finding("key_present", iid(kind, "?"), Verdict.BLOCK,
                               f"{section} {kind} item is missing its key"))
    return out


def check_required_fields(ctx: PlanContext) -> list[Finding]:
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk:
            continue
        for f in nk.required:
            if not item.get(f):
                out.append(Finding("required_fields", ctx.iid_of(kind, item), Verdict.BLOCK,
                                   f"{kind} '{ctx.key_of(kind, item)}' missing required field '{f}'"))
    return out


def check_vocab_membership(ctx: PlanContext) -> list[Finding]:
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk:
            continue
        for fieldname, vname in nk.vocab_fields.items():
            val = item.get(fieldname)
            if val is None:
                continue  # null handled by unmapped_review
            vocab = ctx.schema.vocabularies.get(vname)
            if vocab is not None and val not in vocab:
                out.append(Finding("vocab_membership", ctx.iid_of(kind, item), Verdict.BLOCK,
                                   f"{fieldname}='{val}' is not in vocabulary '{vname}'; "
                                   f"fix it, or set null and list under unmapped_for_review"))
    return out


def check_key_unique_in_file(ctx: PlanContext) -> list[Finding]:
    out = []
    seen = set()
    for section, kind, item in ctx.sections():
        k = (kind, ctx.key_of(kind, item))
        if k[1] and k in seen:
            out.append(Finding("key_unique_in_file", iid(*k), Verdict.BLOCK,
                               f"duplicate {kind} key within file: {k[1]}"))
        seen.add(k)
    return out


def check_refs_exist(ctx: PlanContext) -> list[Finding]:
    """No-implicit-create: every reference must resolve to an existing/declared node."""
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk:
            continue
        for fieldname, target_kind in nk.refs.items():
            vals = item.get(fieldname)
            if vals is None:
                continue
            if not isinstance(vals, list):
                vals = [vals]
            pending = ctx.pending_keys(target_kind)
            for v in vals:
                refkey = v.get("key") if isinstance(v, dict) else v
                if refkey not in pending:
                    out.append(Finding("refs_exist", ctx.iid_of(kind, item), Verdict.BLOCK,
                                       f"references {target_kind} '{refkey}' which doesn't exist; "
                                       f"declare it under `declare.{target_kind}` or fix"))
    return out


def check_implicit_create_guard(ctx: PlanContext) -> list[Finding]:
    """A closed kind (implicit_create: false) may only be created via `declare:`."""
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk or nk.implicit_create or section != "node":
            continue
        key = ctx.key_of(kind, item)
        if (kind, key) not in ctx.existing:
            out.append(Finding("implicit_create_guard", iid(kind, key), Verdict.BLOCK,
                               f"kind '{kind}' is closed (implicit_create: false); a new "
                               f"'{key}' must be declared under `declare.{kind}`"))
    return out


def check_collision(ctx: PlanContext) -> list[Finding]:
    """Re-appearing (kind,key): identical no-op / supersede / held conflict — never a drop."""
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk:
            continue
        key = ctx.key_of(kind, item)
        ex = ctx.existing.get((kind, key))
        if not ex:
            continue
        _id = iid(kind, key)
        if not _content_changed(item, nk, ex):
            out.append(Finding("collision", _id, Verdict.ALLOW,
                               f"'{key}' already present and unchanged — no-op",
                               severity="info", data={"action": "identical"}))
        elif not nk.supersedable:
            out.append(Finding("collision", _id, Verdict.BLOCK,
                               f"'{key}' exists and kind '{kind}' is not supersedable"))
        elif item.get("supersede") is True:
            out.append(Finding("collision", _id, Verdict.ALLOW,
                               f"'{key}' changed; supersede:true — will version the prior belief",
                               severity="warn", data={"action": "supersede"}))
        else:
            out.append(Finding("collision", _id, Verdict.REQUIRE_APPROVAL,
                               f"'{key}' exists with different content; set supersede:true or "
                               f"approve to version the prior belief",
                               severity="warn", data={"action": "conflict"}))
    return out


def check_protected_guard(ctx: PlanContext) -> list[Finding]:
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk:
            continue
        key = ctx.key_of(kind, item)
        ex = ctx.existing.get((kind, key))
        if not ex or not ex.is_protected:
            continue
        if _content_changed(item, nk, ex) and ex.owner and ex.owner != ctx.owner:
            out.append(Finding("protected_guard", iid(kind, key), Verdict.BLOCK,
                               f"'{key}' is protected and owned by '{ex.owner}'; "
                               f"'{ctx.owner}' may not overwrite it"))
    return out


def check_unmapped_review(ctx: PlanContext) -> list[Finding]:
    """Uncertainty is first-class — held for review, never force-fit.

    Only fires for NEW unmapped observations. If the (kind,key) already exists,
    the collision check governs the re-ingest (identical -> no-op, changed ->
    conflict/supersede), so an already-approved unmapped node isn't re-created.
    """
    out = []
    for it in ctx.unmapped:
        kind = it.get("kind", _default_kind(ctx.schema))
        if (kind, ctx.key_of(kind, it)) in ctx.existing:
            continue
        out.append(Finding("unmapped_review", ctx.iid_of(kind, it), Verdict.REQUIRE_APPROVAL,
                           f"'{ctx.key_of(kind, it)}' has no controlled-vocabulary home — "
                           f"held for human review", severity="warn",
                           data={"action": "unmapped"}))
    for kind, items in ctx.nodes.items():
        nk = ctx.schema.kind(kind)
        if not nk or not nk.vocab_fields:
            continue
        for item in items:
            if (kind, ctx.key_of(kind, item)) in ctx.existing:
                continue
            if any(item.get(f) is None for f in nk.vocab_fields):
                out.append(Finding("unmapped_review", ctx.iid_of(kind, item),
                                   Verdict.REQUIRE_APPROVAL,
                                   f"'{ctx.key_of(kind, item)}' has a null controlled field — "
                                   f"will load as unmapped; approve to confirm",
                                   severity="warn", data={"action": "unmapped"}))
    return out


CHECK_REGISTRY = [
    ("date_format", check_date_format),
    ("unknown_kind", check_unknown_kind),
    ("key_present", check_key_present),
    ("required_fields", check_required_fields),
    ("vocab_membership", check_vocab_membership),
    ("key_unique_in_file", check_key_unique_in_file),
    ("refs_exist", check_refs_exist),
    ("implicit_create_guard", check_implicit_create_guard),
    ("collision", check_collision),
    ("protected_guard", check_protected_guard),
    ("unmapped_review", check_unmapped_review),
]


def run_checks(ctx: PlanContext) -> list[Finding]:
    findings = []
    for _name, fn in CHECK_REGISTRY:
        findings.extend(fn(ctx))
    return findings


# ---------------------------------------------------------------------------
# Action resolution
# ---------------------------------------------------------------------------

CREATE = "create"
UNCHANGED = "unchanged"
SUPERSEDE = "supersede"
CREATE_UNMAPPED = "create_unmapped"
HELD = "held"
BLOCKED = "blocked"


@dataclass
class ItemPlan:
    section: str          # node | declare | unmapped
    kind: str
    key: str
    id: str               # iid(kind, key) — the composite identity
    verdict: Verdict
    action: str
    approved: bool
    item: dict
    detail: str = ""
    findings: list = field(default_factory=list)


def _hint(findings: list[Finding], _id: str) -> str | None:
    for f in findings:
        if f.target == _id and f.data.get("action"):
            return f.data["action"]
    return None


def resolve_actions(ctx: PlanContext, findings: list[Finding],
                    approvals: set | None = None, approve_all: bool = False) -> list[ItemPlan]:
    """Turn findings into one ItemPlan per writable item. `approvals` is a set of iids."""
    approvals = approvals or set()
    plans = []
    for section, kind, item in ctx.sections():
        key = ctx.key_of(kind, item) or "?"
        _id = iid(kind, key)
        verdict = item_verdict(findings, _id)
        hint = _hint(findings, _id)
        approved = approve_all or _id in approvals
        item_findings = [f for f in findings if f.target == _id]

        if verdict >= Verdict.BLOCK:
            action, detail = BLOCKED, ("run HALT" if verdict == Verdict.HALT else "failed a blocking check")
        elif verdict == Verdict.REQUIRE_APPROVAL:
            if approved:
                if hint == "unmapped":
                    action, detail = CREATE_UNMAPPED, "approved (unmapped)"
                elif hint == "conflict":
                    action, detail = SUPERSEDE, "approved (supersede prior belief)"
                else:
                    action, detail = CREATE, "approved"
            else:
                action, detail = HELD, "awaiting approval"
        else:  # ALLOW
            if hint == "identical":
                action, detail = UNCHANGED, "already present, unchanged"
            elif hint == "supersede":
                action, detail = SUPERSEDE, "supersede:true"
            elif section == "unmapped":
                action, detail = CREATE_UNMAPPED, "unmapped"
            elif (kind, key) in ctx.existing:
                action, detail = UNCHANGED, "already present"
            else:
                action, detail = CREATE, "new"

        plans.append(ItemPlan(section=section, kind=kind, key=key, id=_id, verdict=verdict,
                              action=action, approved=approved, item=item,
                              detail=detail, findings=item_findings))
    return plans
