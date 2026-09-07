"""Near-duplicate key detection — the phantom the refs check cannot catch.

`refs_exist` enforces that a reference resolves to a declared node, which stops
a stray edge from conjuring an account. It does nothing about the other way a
phantom arrives: a writer declares `Acme Robotics, Inc.` when `Acme Robotics`
already exists, both are legitimate declarations, and the graph now holds two
nodes for one company. Nothing errored. Every query that joins on either one is
quietly half-right from then on.

Three decisions shape this, and the last is the one that matters.

**Exact match first, similarity for the residue.** Normalising and comparing
exactly costs nothing and introduces no false merges; it catches the
overwhelmingly common case, which is the same extraction running twice with
different whitespace or casing. Similarity is for what is left.

**Entropy before similarity.** The real problem with fuzzy matching is not
picking a threshold, it is that a threshold means different things for different
strings: `ABC` and `ABD` score high and mean nothing, `Northwind Analytics` and
`Northwind Analytic` score high and mean everything. Short or repetitive keys are
unreliable at any threshold, so they are excluded from comparison rather than
compared more strictly. This gate is taken from Graphiti's `dedup_helpers.py`,
which defers low-entropy names to an LLM; here there is no LLM to defer to, so a
low-entropy key is simply not a candidate.

**It never merges.** A near-duplicate is reported as REQUIRE_APPROVAL and the
node is created if approved — two nodes, both intact, and a human told about it.
Auto-merging above a threshold is what the field does: the official Neo4j
resolver calls APOC with `properties: 'discard'`, which keeps the first node's
properties and throws away the rest with no record and no reversal. That is
unsafe here for a reason no threshold fixes: `Acme Robotics` vs `Acme Robotics,
Inc.` should merge, `Portland, OR` vs `Portland, ME` scores about the same and
must not, and `Q3 Report` vs `Q4 Report` differ by the one character that carries
all the meaning. A gate whose whole thesis is that ambiguity gets held cannot
resolve this one by guessing.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

# --- tuning ---------------------------------------------------------------
#
# CALIBRATED ON NAMED PAIRS, and the number is meaningless without the metric.
# Trigram Jaccard scores systematically lower than the normalised edit-distance
# ratios other tools threshold at, so this 0.75 is NOT more permissive than
# somebody else's 0.8 — the scales are different and the two are not comparable.
# The pairs it was set against:
#
#     Acme Robotics       / Acme Robotics, Inc.   0.77   flag   (one company)
#     Northwind Analytics / Northwind Analytic    0.94   flag   (a typo)
#     Portland, OR        / Portland, ME          0.60   pass   (two governments)
#     Q3 Report           / Q4 Report             0.50   pass   (two documents)
#
# Change this and re-check those four; the separation between the first two and
# the last two is the whole design, and it is narrower than it looks.
#
# The floor can sit this low precisely BECAUSE a hit is held rather than merged.
# A false positive costs one human glance; a tool that auto-merges has to set a
# high floor because a false positive there destroys data. Holding buys recall.
DEFAULT_THRESHOLD = 0.75

# Below these, a key carries too little signal for similarity to mean anything.
MIN_LENGTH = 6
MIN_TOKENS = 2
ENTROPY_FLOOR = 1.5

# Comparing every pair is O(n²). Blocking on the first few characters of the
# normalised form keeps candidate generation near-linear while catching the edits
# people actually make — an added suffix, punctuation, casing.
#
# Do NOT add a length band to this. The first version did (`len // 8`), which put
# `acme robotics` (13) and `acme robotics inc` (17) in different blocks and so
# missed the exact pair this check exists for. A suffix addition changes length
# by definition; banding on length excludes the target case.
#
# The documented cost of prefix blocking is a first-character typo — `Acme` vs
# `Acmee` blocks together, `Acme` vs `Ecme` does not.
_BLOCK_PREFIX = 4


def normalize(key: str) -> str:
    """Casefold, strip punctuation, collapse whitespace.

    Two keys that normalise identically are the same identity by any reasonable
    reading — `ACME Robotics` and `acme  robotics.` — so this is the exact-match
    pass and it runs before any scoring.
    """
    s = re.sub(r"[^\w\s]", " ", str(key or "").casefold())
    return re.sub(r"\s+", " ", s).strip()


def _entropy(text: str) -> float:
    """Shannon entropy over characters — how much signal the string carries."""
    chars = text.replace(" ", "")
    if not chars:
        return 0.0
    counts: dict[str, int] = {}
    for c in chars:
        counts[c] = counts.get(c, 0) + 1
    total = len(chars)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def is_comparable(normalized: str) -> bool:
    """Is this key specific enough that a similarity score means anything?

    A short single token (`api`, `q3`, `us`) is excluded: such keys collide by
    coincidence constantly, and reporting those pairs would bury the real ones.
    """
    if not normalized:
        return False
    if len(normalized) < MIN_LENGTH and len(normalized.split()) < MIN_TOKENS:
        return False
    return _entropy(normalized) >= ENTROPY_FLOOR


@lru_cache(maxsize=4096)
def _trigrams(normalized: str) -> frozenset:
    s = normalized.replace(" ", "")
    if len(s) < 3:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + 3] for i in range(len(s) - 2))


def similarity(a_norm: str, b_norm: str) -> float:
    """Jaccard over character trigrams, in [0, 1].

    Trigrams rather than edit distance because the common real case is an added
    or removed *span* — a legal suffix, a parenthetical, a middle name — which
    edit distance penalises in proportion to its length while trigram overlap
    barely notices. That is the behaviour we want: `Acme Robotics` and `Acme
    Robotics, Inc.` should score high.

    **Known miss, measured:** a long suffix on a *short* base scores low, because
    the added span dominates the union. `Globex` vs `Globex Corporation` is 0.27
    and will not be flagged. The metric is only reliable when the shared part is
    most of both strings, which is true of the suffix and typo cases it is aimed
    at and false when a two-word name grows to five. This check narrows the
    phantom-node surface; it does not close it.
    """
    ta, tb = _trigrams(a_norm), _trigrams(b_norm)
    if not ta or not tb:
        return 1.0 if a_norm == b_norm else 0.0
    inter = len(ta & tb)
    return inter / (len(ta) + len(tb) - inter)


def block_key(normalized: str) -> str:
    """Candidates only compare within a block. Cheap, and lossy in a stated way."""
    return normalized[:_BLOCK_PREFIX]


def find_near_duplicates(candidates, existing, *, threshold: float = DEFAULT_THRESHOLD):
    """Yield `(new_key, existing_key, score)` for pairs worth a human's attention.

    `candidates` and `existing` are iterables of raw key strings. An exact
    normalised match is NOT reported: that is the collision check's job, and
    reporting it here would double up on an item the gate already handles
    precisely.
    """
    by_block: dict = {}
    norm_existing = {}
    for key in existing:
        n = normalize(key)
        norm_existing[key] = n
        if is_comparable(n):
            by_block.setdefault(block_key(n), []).append((key, n))

    seen = set()
    for key in candidates:
        n = normalize(key)
        if not is_comparable(n):
            continue
        for other_key, other_n in by_block.get(block_key(n), ()):
            if other_key == key or n == other_n:
                continue          # identical -> the collision check owns it
            pair = tuple(sorted((key, other_key)))
            if pair in seen:
                continue
            score = similarity(n, other_n)
            if score >= threshold:
                seen.add(pair)
                yield (key, other_key, score)
