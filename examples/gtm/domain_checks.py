"""Example domain check pack — rules that are true of THIS graph, not every graph.

The kernel enforces what holds for any governed graph: identity, controlled
vocabulary, referential integrity, collision, protection. It deliberately does
not know that a GTM insight needs an expiry date, because most graphs don't.

Declared in the schema, so the enforced rule set is inside the schema
fingerprint:

    # schema.yaml
    checks:
      - examples.gtm.domain_checks:REGISTRY

Point the same mechanism at your own rules. The two below are the shapes worth
copying, and both come from real failures rather than from imagination.
"""
from __future__ import annotations

from datetime import date, datetime

from kgg.verdicts import Finding, Verdict


def check_review_date_set(ctx) -> list[Finding]:
    """Every insight must declare when it needs re-checking.

    A graph with no expiry has no way to distinguish a fact that is still true
    from one nobody has looked at in eighteen months, and both read as current to
    whoever queries it. An SME confidently out of date is worse than no SME —
    especially when the output is client-facing.

    Blocking rather than holding is deliberate: `review_after` is one field the
    writer always knows, so there is nothing for a human to adjudicate.
    """
    out = []
    for section, kind, item in ctx.sections():
        if kind != "insight" or section == "declare":
            continue
        val = item.get("review_after")
        _id = ctx.iid_of(kind, item)
        if not val:
            out.append(Finding("review_date_set", _id, Verdict.BLOCK,
                               "no `review_after` — a fact with no expiry outlives its "
                               "shelf life while still reading as current"))
            continue
        try:
            when = datetime.strptime(str(val), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            out.append(Finding("review_date_set", _id, Verdict.BLOCK,
                               f"review_after must be YYYY-MM-DD, got {val!r}"))
            continue
        if when <= date.today():
            out.append(Finding("review_date_set", _id, Verdict.REQUIRE_APPROVAL,
                               f"review_after {when} is already past — this fact is stale on "
                               f"arrival; approve only if you have re-verified it",
                               severity="warn"))
    return out


def check_evidence_supports_tier(ctx) -> list[Finding]:
    """A `verified` claim needs an authoritative source, not more anecdotes.

    Independence beats count, and one authoritative document outranks any number
    of conversations. Three calls to three towns in one county is one source with
    a loud voice; a statute is evidence. Counting sources gets that backwards, so
    the tier is gated on the KIND of source rather than how many there are.
    """
    authoritative = {"statute", "budget-doc", "official-policy", "publication", "filing"}
    out = []
    for section, kind, item in ctx.sections():
        if kind != "insight" or item.get("evidence_tier") != "verified":
            continue
        if str(item.get("source_kind", "")) not in authoritative:
            out.append(Finding("evidence_supports_tier", ctx.iid_of(kind, item), Verdict.BLOCK,
                               f"evidence_tier 'verified' needs an authoritative source_kind "
                               f"(one of {sorted(authoritative)}), got "
                               f"{item.get('source_kind')!r}"))
    return out


REGISTRY = [
    ("review_date_set", check_review_date_set),
    ("evidence_supports_tier", check_evidence_supports_tier),
]
