"""The gap layer — how a controlled vocabulary grows without drifting.

A closed vocabulary has one honest failure mode: the writer finds something real
that the vocabulary has no word for. Every gate has to answer that, and there are
only three answers.

  1. Let the writer coin a term.  This is taxonomy drift, which is the thing the
     gate exists to prevent.
  2. Force-fit it into the nearest existing term.  Worse than drift: the fact is
     now filed under a claim it does not support, and nothing marks it.
  3. Hold it as unmapped.  Correct, and where most systems stop — at which point
     the observation is stored somewhere nobody reads and the vocabulary never
     improves.

This module is the fourth answer: **unmapped is a proposal, and proposals need a
join key.** An unmapped item names the missing mechanic in a short kebab-case
slug. Two writers reading different sources who hit the same hole land on the
same slug, the gap accumulates independent evidence, and at a threshold it is
promoted into the vocabulary as a reviewable diff.

Three findings shaped this, all of them measured rather than reasoned:

**Prose is not a join key.** An earlier version asked writers for a richer
free-text reason and clustered on that. The reasons got ~65% longer and
cross-writer gap merges went from one to *zero* — a longer description inflates
the overlap denominator between two accounts of the same gap, so convergence gets
strictly harder as the artifact a human reads gets better. The slug and the prose
are different artifacts with different consumers. Keep the slug short.

**Convergence on an open set is telepathy.** Asking writers to independently
choose the same "canonical" name does not work: 280 untagged observations
produced 223 distinct slugs and *no* gap was ever named by two different sources.
The canonical vocabulary does not fail this way for a structural reason, not an
effort one — the writer is handed the list. `known_gaps()` exists to hand them
the gap list too, which turns convergence from telepathy into a lookup.

**A wrong match is worse than a new slug.** Two unrelated observations filed
under one gap manufacture the corroboration that promotes it. So the matching
rule is the same one the vocabulary already uses: match on the *mechanic*, not on
the words, and when it is close but not the same, coin a new slug and say in the
reason which gap it resembles and how it differs. A near-miss is a real finding.

**Independence, not count.** Promotion counts *distinct scopes*, never
observations. Three observations from one scope are one source with a loud voice;
two scopes that never spoke to each other agreeing is evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 2-6 kebab-case words. Long enough to name a mechanic, short enough that two
# writers can land on it independently.
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){1,5}$")
MIN_WORDS, MAX_WORDS = 2, 6

# Slugs that are syntactically fine and name nothing. Each of these describes the
# writer's state ("I could not tag this") rather than the mechanic they found,
# which makes them a bucket every unrelated gap falls into.
NON_NAMING = {
    "no-tag", "no-tag-for-this", "no-match", "not-applicable", "not-sure",
    "unknown-gap", "other-gap", "misc-gap", "to-be-decided", "to-do",
    "needs-review", "needs-a-tag", "new-tag-needed", "missing-tag",
    "no-category", "uncategorised", "uncategorized", "general-gap", "misc-other",
}

# Tokens that carry no discriminating meaning; a slug made only of these names
# nothing even if it avoids the blocklist above.
STOPWORDS = {"the", "a", "an", "of", "for", "this", "that", "thing", "stuff",
             "item", "gap", "new", "other", "misc", "general", "various"}


def _words(slug: str) -> list[str]:
    return [w for w in slug.split("-") if w]


def _tokens(text: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) > 2}


def validate_slug(slug: str, *, scope: str = "") -> list[str]:
    """Problems with a gap slug (empty list = usable as a join key).

    The instance check is the load-bearing one. A slug that names the account,
    client, or tenant it came from cannot be reached by a second writer looking at
    a different account — it is unique by construction, which is exactly the
    property that killed every gap in the version of this that keyed on a
    per-observation hash. `northwind-tourist-town-thing` is a real example of the
    failure: syntactically perfect, joinable by nobody.

    It checks the SCOPE only, deliberately. The item's own key is a writer-chosen
    label for the same mechanic the slug names, so the two overlap constantly in
    perfectly good slugs — `ai-governance-clause` on an item tagged
    `buyers_want_ai_clause` is the mechanic named twice, not an instance name.
    Rejecting on that would refuse the well-formed case far more often than the
    malformed one.
    """
    problems = []
    if not SLUG_RE.match(slug):
        problems.append(
            f"must be {MIN_WORDS}-{MAX_WORDS} lowercase kebab-case words "
            f"(e.g. 'effective-size-proxy'), got {slug!r}")
        return problems      # the checks below assume a parseable slug

    words = _words(slug)
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        problems.append(f"must be {MIN_WORDS}-{MAX_WORDS} words, got {len(words)}")
    if slug in NON_NAMING:
        problems.append("names the writer's state, not the missing mechanic — "
                        "say what the vocabulary is missing a word FOR")
    elif all(w in STOPWORDS for w in words):
        problems.append("is made entirely of filler words and names no mechanic")

    shared = _tokens(scope) & set(words)
    if shared:
        problems.append(
            f"names the scope it came from ({', '.join(sorted(shared))}) rather than the "
            f"mechanic; a slug only this source can produce can never be joined by a second "
            f"writer, which is the whole point of the key")
    return problems


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

@dataclass
class Gap:
    """One named hole in the vocabulary, and the evidence standing behind it."""
    slug: str
    means: str = ""                  # the most recent non-empty `unmapped_reason`
    observations: int = 0
    scopes: tuple = ()               # DISTINCT scopes — the promotion metric
    sources: tuple = ()
    items: tuple = ()                # composite ids carrying this gap
    first_seen: str = ""
    last_seen: str = ""
    status: str = "open"             # open | promoted | rejected
    promoted_to: str = ""
    resolution_note: str = ""

    @property
    def independent(self) -> int:
        return len(self.scopes)

    def promotable(self, floor: int) -> bool:
        return self.status == "open" and self.independent >= floor

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "means": self.means, "observations": self.observations,
            "independent_scopes": self.independent, "scopes": list(self.scopes),
            "sources": list(self.sources), "items": list(self.items),
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "status": self.status, "promoted_to": self.promoted_to,
            "resolution_note": self.resolution_note,
        }


# Two independent scopes. The bar is deliberately the same one the evidence tiers
# use: corroboration means two sources that could not have influenced each other,
# and raising the bar to three would mean no gap is ever promoted from a corpus
# with only a handful of scopes.
DEFAULT_PROMOTION_FLOOR = 2


def observations_from_plans(ctx, plans) -> list[dict]:
    """Gap observations produced by one run's plans.

    Only items that actually LAND contribute. A held or blocked observation has
    not entered the graph, so counting it would let a rejected write vote on the
    vocabulary — the same mistake as counting an unapplied proposal as evidence.
    """
    from .checks import CREATE_UNMAPPED
    out = []
    for plan in plans:
        if plan.action != CREATE_UNMAPPED:
            continue
        slug = (plan.item.get("unmapped_gap") or "").strip()
        if not slug:
            continue
        out.append({
            "slug": slug,
            "means": (plan.item.get("unmapped_reason") or "").strip(),
            "kind": plan.kind,
            "key": plan.key,
            "item_id": plan.id,
            "scope": ctx.scope or "",
            "source": ctx.source or "",
            "run_id": ctx.run_id,
            "observed_on": ctx.date or "",
        })
    return out


def aggregate(rows: list[dict], resolutions: dict | None = None) -> list[Gap]:
    """Roll observation rows up into ranked Gaps, most promotable first."""
    resolutions = resolutions or {}
    by_slug: dict[str, dict] = {}
    for r in rows:
        g = by_slug.setdefault(r["slug"], {
            "observations": 0, "scopes": set(), "sources": set(), "items": set(),
            "means": "", "first": "", "last": "",
        })
        g["observations"] += 1
        if r.get("scope"):
            g["scopes"].add(r["scope"])
        if r.get("source"):
            g["sources"].add(r["source"])
        if r.get("item_id"):
            g["items"].add(r["item_id"])
        if r.get("means"):
            g["means"] = r["means"]          # most recent non-empty wins
        ts = r.get("recorded_ts") or r.get("observed_on") or ""
        if ts:
            g["first"] = min(g["first"], ts) if g["first"] else ts
            g["last"] = max(g["last"], ts) if g["last"] else ts

    out = []
    for slug, g in by_slug.items():
        res = resolutions.get(slug, {})
        out.append(Gap(
            slug=slug, means=g["means"], observations=g["observations"],
            scopes=tuple(sorted(g["scopes"])), sources=tuple(sorted(g["sources"])),
            items=tuple(sorted(g["items"])), first_seen=g["first"], last_seen=g["last"],
            status=res.get("status", "open"), promoted_to=res.get("promoted_to", ""),
            resolution_note=res.get("note", ""),
        ))
    out.sort(key=lambda x: (-x.independent, -x.observations, x.slug))
    return out


def known_gaps(gaps: list[Gap], *, limit: int = 200, include_promoted: bool = False) -> list[dict]:
    """The list to hand a writer BEFORE they coin a new slug.

    This is the mechanism, not a convenience. Convergence on a closed set is a
    lookup; convergence on an open set is telepathy. Injecting this into an
    extraction prompt is what turns the gap layer from a pile of unique strings
    into something that can accumulate evidence.

    `means` is included because a slug alone cannot be matched on the mechanic —
    and matching on the words instead of the mechanic is how two unrelated
    observations end up manufacturing each other's corroboration.
    """
    out = []
    for g in gaps:
        if g.status == "rejected":
            continue
        if g.status == "promoted" and not include_promoted:
            continue
        out.append({"slug": g.slug, "means": g.means,
                    "independent_scopes": g.independent,
                    **({"promoted_to": g.promoted_to} if g.promoted_to else {})})
        if len(out) >= limit:
            break
    return out


def promotion_block(gap: Gap, term: str, category: str, definition: str,
                    vocab_version: str = "") -> dict:
    """The vocabulary entry a promoted gap becomes.

    Emitted for review, never written silently. Promoting changes the vocabulary
    file, which changes the schema fingerprint, which invalidates every approval
    granted under the old contract — so a promotion is a policy change and gets
    the same propose-then-apply beat as any other.
    """
    return {
        "term": term,
        "category": category,
        "entry": {
            "definition": definition,
            "status": "active",
            "since": vocab_version or "",
            "promoted_from_gap": gap.slug,
        },
        "evidence": {
            "independent_scopes": gap.independent,
            "scopes": list(gap.scopes),
            "observations": gap.observations,
            "items": list(gap.items),
        },
    }
