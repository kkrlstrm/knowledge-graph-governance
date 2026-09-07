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

from .model import (
    Schema, NodeKind, ExistingNode, iid,
    EXACT, SYNONYM, DEPRECATED_HIT, UNKNOWN,
)
from .similarity import find_near_duplicates
from .verdicts import Finding, Verdict, RUN, item_verdict, worst


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

    def unmapped_items(self):
        """(kind, key, item) for everything with no controlled-vocabulary home.

        Two ways to be unmapped and they are the same thing to every downstream
        consumer: listed under `unmapped_for_review`, or carried as a node whose
        controlled field is explicitly null. Treating them as one list is what
        lets the gap layer see both.
        """
        for it in self.unmapped:
            kind = it.get("kind", _default_kind(self.schema))
            yield kind, self.key_of(kind, it), it
        for kind, items in self.nodes.items():
            nk = self.schema.kind(kind)
            if not nk or not nk.vocab_fields:
                continue
            for it in items:
                if any(it.get(f) is None for f in nk.vocab_fields):
                    yield kind, self.key_of(kind, it), it

    def slot_of(self, kind: str, item: dict) -> str:
        """Serialised `claim_slot` — what this node is a claim ABOUT.

        Two active nodes sharing a slot are competing claims about one thing;
        that is the anchor a contradiction detector needs. Empty when the kind
        declares no slot, which disables detection for that kind.
        """
        nk = self.schema.kind(kind)
        if not nk or not nk.claim_slot:
            return ""
        return slot_value(nk, item)


def _default_kind(schema: Schema) -> str:
    for name, nk in schema.kinds.items():
        if nk.vocab_fields:
            return name
    return next(iter(schema.kinds), "node")


def slot_value(nk: NodeKind, item: dict) -> str:
    """Stable serialisation of a kind's claim_slot fields from one item."""
    parts = []
    for f in nk.claim_slot:
        v = item.get(f)
        if isinstance(v, list):
            v = "|".join(sorted(str(x) for x in v))
        parts.append(f"{f}={'' if v is None else v}")
    return "&".join(parts)


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
    """Controlled vocabulary, with a declared alias and deprecation path.

    Four outcomes, because "in the list / not in the list" loses the two states
    that matter most to a vocabulary that is maintained rather than frozen:

      exact       silent
      synonym     folded onto the canonical term, reported (the fold happened in
                  `normalize_vocab` before this ran, so this only ever sees it
                  as exact — the finding is emitted there)
      deprecated  REQUIRE_APPROVAL, naming the replacement. Not a block: the
                  writer used a term that WAS canonical, and refusing outright
                  turns a rename into data loss.
      unknown     BLOCK — this is the taxonomy-drift guarantee
    """
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
            if vocab is None:
                continue
            canonical, how = vocab.resolve(val)
            if how == UNKNOWN:
                out.append(Finding("vocab_membership", ctx.iid_of(kind, item), Verdict.BLOCK,
                                   f"{fieldname}='{val}' is not in vocabulary '{vname}'; "
                                   f"fix it, or set null and name the gap in `unmapped_gap`"))
            elif how == DEPRECATED_HIT:
                out.append(Finding("vocab_membership", ctx.iid_of(kind, item),
                                   Verdict.REQUIRE_APPROVAL,
                                   f"{fieldname}='{val}' is deprecated in '{vname}'"
                                   + (f"; replaced by '{canonical}'" if canonical != val else "")
                                   + " — approve to write it under the replacement",
                                   severity="warn",
                                   data={"vocab_replace": [fieldname, val, canonical]}))
    return out


def normalize_vocab(ctx: PlanContext) -> list[Finding]:
    """Fold DECLARED synonyms onto their canonical term, before any check runs.

    Normalise-on-read, so a file written against an alias lands under the
    canonical term and stays retrievable by it. The fold is reported, never
    silent — an unreported rewrite of a writer's term is how you lose the ability
    to tell a curated alias from a model that invented one.

    Only aliases declared in the vocabulary file fold. Nothing is inferred from
    string similarity: two terms that sound alike are routinely different
    concepts, and a gate that guessed would merge them with no diff to review.
    """
    out = []
    for section, kind, item in ctx.sections():
        nk = ctx.schema.kind(kind)
        if not nk:
            continue
        for fieldname, vname in nk.vocab_fields.items():
            val = item.get(fieldname)
            vocab = ctx.schema.vocabularies.get(vname)
            if val is None or vocab is None:
                continue
            canonical, how = vocab.resolve(val)
            if how == SYNONYM and canonical:
                item[fieldname] = canonical
                out.append(Finding("vocab_synonym_folded", ctx.iid_of(kind, item), Verdict.ALLOW,
                                   f"{fieldname}='{val}' folded onto declared canonical term "
                                   f"'{canonical}' in '{vname}'",
                                   severity="info",
                                   data={"folded": [fieldname, val, canonical]}))
    return out


def check_unmapped_gap_named(ctx: PlanContext) -> list[Finding]:
    """An unmapped observation must name the GAP it found, with a joinable slug.

    Unmapped is a first-class answer, but it is a *proposal*, not a resting
    place — and a proposal nobody can join to another proposal dies alone. The
    slug is the join key: two writers reading different sources who hit the same
    missing mechanic must be able to land on the same short string, or the gap
    can never accumulate the independent evidence that promotes it into the
    vocabulary.

    Prose cannot do this job. A longer `unmapped_reason` inflates the overlap
    denominator between two descriptions of one gap and makes convergence
    strictly harder — richer reasons measurably drove cross-writer gap merges
    down, not up. So the slug is short and canonical, and the nuance goes in the
    reason where a human reads it.
    """
    if not ctx.schema.require_gap_slug:
        return []
    from .gaps import validate_slug
    out = []
    for kind, key, item in ctx.unmapped_items():
        _id = iid(kind, key)
        slug = (item.get("unmapped_gap") or "").strip()
        if not slug:
            out.append(Finding("unmapped_gap_named", _id, Verdict.BLOCK,
                               "has no controlled-vocabulary home and no `unmapped_gap` slug; "
                               "name the missing mechanic in 2-6 kebab-case words so a second "
                               "writer who finds the same hole lands on the same key"))
            continue
        for problem in validate_slug(slug, scope=ctx.scope):
            out.append(Finding("unmapped_gap_named", _id, Verdict.BLOCK,
                               f"unmapped_gap '{slug}': {problem}"))
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


def check_near_duplicate_key(ctx: PlanContext) -> list[Finding]:
    """A new key that is nearly an existing one — held, never merged.

    `refs_exist` stops a stray reference from conjuring a node. This catches the
    other door: a writer legitimately declares `Acme Robotics, Inc.` when `Acme
    Robotics` exists, both declarations are valid, and the graph now holds two
    nodes for one company with nothing marking it.

    Only fires for genuinely NEW keys on kinds that opt in with
    `near_duplicate_check: true`. It is off by default because whether two
    similar keys are the same thing is a domain question — `Portland, OR` and
    `Portland, ME` score the same as `Acme Robotics` and `Acme Robotics, Inc.` —
    and a gate should not assume the answer for a kind whose owner has not said.
    """
    out = []
    for kind, nk in ctx.schema.kinds.items():
        if not nk.near_duplicate_check:
            continue
        existing = [k for (kd, k) in ctx.existing if kd == kind]
        incoming = []
        for section, k, item in ctx.sections():
            if k != kind:
                continue
            key = ctx.key_of(kind, item)
            if key and (kind, key) not in ctx.existing:
                incoming.append(key)
        if not incoming or not existing:
            continue
        for new_key, other, score in find_near_duplicates(
                incoming, existing, threshold=nk.near_duplicate_threshold):
            out.append(Finding(
                "near_duplicate_key", iid(kind, new_key), Verdict.REQUIRE_APPROVAL,
                f"'{new_key}' is {score:.0%} similar to existing {kind} '{other}'; "
                f"approve to create it as a separate node, or fix the key to reuse "
                f"the existing one. Nothing is merged either way.",
                severity="warn", data={"near_duplicate_of": other, "score": round(score, 4)}))
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
    for kind, key, item in ctx.unmapped_items():
        if (kind, key) in ctx.existing:
            continue
        gap = (item.get("unmapped_gap") or "").strip()
        out.append(Finding("unmapped_review", iid(kind, key), Verdict.REQUIRE_APPROVAL,
                           f"'{key}' has no controlled-vocabulary home"
                           + (f" — gap '{gap}'" if gap else "")
                           + "; held for human review", severity="warn",
                           data={"action": "unmapped", "gap": gap}))
    return out


def check_contradicts_ref(ctx: PlanContext) -> list[Finding]:
    """A declared `contradicts:` target must be a real node, and not itself.

    The contradiction ledger is advisory — it never changes what the graph
    believes — but a pointer into nothing is still a broken record, and a node
    that contradicts itself is a writer bug worth catching at the gate.
    """
    out = []
    for section, kind, item in ctx.sections():
        targets = item.get("contradicts")
        if targets is None:
            continue
        if not isinstance(targets, list):
            targets = [targets]
        _id = ctx.iid_of(kind, item)
        for t in targets:
            tk, _, tkey = str(t).partition(":")
            if not tkey:
                out.append(Finding("contradicts_ref", _id, Verdict.BLOCK,
                                   f"contradicts '{t}' is not a composite id; use 'kind:key'"))
                continue
            if (tk, tkey) == (kind, ctx.key_of(kind, item)):
                out.append(Finding("contradicts_ref", _id, Verdict.BLOCK,
                                   "contradicts itself"))
                continue
            if (tk, tkey) not in ctx.existing and tkey not in ctx.pending_keys(tk):
                out.append(Finding("contradicts_ref", _id, Verdict.BLOCK,
                                   f"contradicts '{t}', which does not exist"))
    return out


# The kernel's own rules: what is true of ANY governed graph. Domain rules are
# declared in the schema (`checks:`) and appended — see kgg/plugins.py.
CORE_CHECKS = [
    ("date_format", check_date_format),
    ("unknown_kind", check_unknown_kind),
    ("key_present", check_key_present),
    ("required_fields", check_required_fields),
    ("vocab_membership", check_vocab_membership),
    ("key_unique_in_file", check_key_unique_in_file),
    ("refs_exist", check_refs_exist),
    ("near_duplicate_key", check_near_duplicate_key),
    ("contradicts_ref", check_contradicts_ref),
    ("implicit_create_guard", check_implicit_create_guard),
    ("collision", check_collision),
    ("protected_guard", check_protected_guard),
    ("unmapped_review", check_unmapped_review),
    ("unmapped_gap_named", check_unmapped_gap_named),
]

# Back-compat alias — CORE_CHECKS is the name to use.
CHECK_REGISTRY = CORE_CHECKS


def run_checks(ctx: PlanContext, extra=None) -> list[Finding]:
    """Normalise, then run the core registry, then any domain registries.

    Domain checks run last so a domain rule sees items whose synonyms have
    already been folded, and so a core BLOCK is never masked by a domain check
    raising an error on a malformed item the kernel would have rejected anyway.
    """
    findings = list(normalize_vocab(ctx))
    for _name, fn in list(CORE_CHECKS) + list(extra or []):
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


def _apply_vocab_replacements(findings: list[Finding], _id: str, item: dict) -> list[str]:
    """Rewrite a deprecated term onto its replacement, once approved.

    Deferred to approval on purpose: a rename is a policy change, and a gate that
    quietly rewrote the writer's term would hide the fact that the vocabulary
    moved under them.
    """
    notes = []
    for f in findings:
        if f.target != _id or "vocab_replace" not in f.data:
            continue
        fieldname, old, new = f.data["vocab_replace"]
        if new and new != old:
            item[fieldname] = new
            notes.append(f"{fieldname} '{old}' -> '{new}'")
    return notes


def resolve_actions(ctx: PlanContext, findings: list[Finding],
                    approvals: set | None = None, approve_all: bool = False) -> list[ItemPlan]:
    """Turn findings into one ItemPlan per writable item. `approvals` is a set of iids."""
    approvals = approvals or set()

    # A run-level finding is a floor under EVERY item. Without this a check that
    # condemns the whole file — a malformed date, a bad top-level field — is
    # recorded in the proposal and then blocks nothing, because item verdicts are
    # matched by target and no item's target is RUN. The file is suspect, so
    # nothing from it is trusted.
    run_findings = [f for f in findings if f.target == RUN]
    run_verdict = worst(run_findings)
    run_why = "; ".join(f.message for f in run_findings
                        if f.verdict >= Verdict.REQUIRE_APPROVAL)

    plans = []
    for section, kind, item in ctx.sections():
        key = ctx.key_of(kind, item) or "?"
        _id = iid(kind, key)
        verdict = max(item_verdict(findings, _id), run_verdict)
        hint = _hint(findings, _id)
        approved = approve_all or _id in approvals
        item_findings = [f for f in findings if f.target == _id]

        if verdict >= Verdict.BLOCK:
            if verdict == Verdict.HALT:
                detail = "run HALT"
            elif item_verdict(findings, _id) < Verdict.BLOCK:
                detail = f"blocked by a run-level check: {run_why}"
            else:
                detail = "failed a blocking check"
            action = BLOCKED
        elif verdict == Verdict.REQUIRE_APPROVAL:
            if approved:
                renames = _apply_vocab_replacements(item_findings, _id, item)
                if hint == "unmapped":
                    action, detail = CREATE_UNMAPPED, "approved (unmapped)"
                elif hint == "conflict":
                    action, detail = SUPERSEDE, "approved (supersede prior belief)"
                elif (kind, key) in ctx.existing:
                    action, detail = UNCHANGED, "approved; already present"
                else:
                    action, detail = CREATE, "approved"
                if renames:
                    detail += " [deprecated term rewritten: " + "; ".join(renames) + "]"
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
