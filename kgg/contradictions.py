"""Contradiction observations — simultaneous disagreement, kept as evidence.

Supersession answers "the belief changed over time." It cannot answer "two
sources disagree *right now*", and collapsing the second into the first is a
real loss: superseding one of two competing claims asserts that the newer one
won, when the only thing actually established is that they conflict.

So this is a **derived observation ledger**, and four properties make it safe:

- **Advisory, not a third truth value.** Both claims keep their normal status,
  revision, and provenance. A read of current truth is unchanged by a
  contradiction existing. What changes is that the reader can now find out.
- **It never picks a winner.** No source-authority ranking, no confidence
  comparison, no recency tie-break. Both sides are stored with their own
  provenance so a caller can judge; the ledger's job is to make the disagreement
  findable, not to resolve it.
- **Deterministic detectors only.** No model call. A detector that needed
  inference would make "is there a contradiction?" a question with a different
  answer every time you asked it.
- **Resolution retains the record.** When one side is superseded, archived, or
  removed, the observation resolves — it is not deleted. The resolved row keeps
  both content snapshots, so "we used to believe two conflicting things and here
  is how it ended" survives the thing that ended it.

The anchor is the *claim slot*: the schema declares `claim_slot` (what a node is
a claim ABOUT) and `claim_field` (the text that carries the claim). Two active
nodes sharing a slot are candidates. Sharing a slot is necessary and nowhere near
sufficient — most nodes about one subject simply say different things — so a
detector must also fire.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ACTIVE = "active"
RESOLVED = "resolved"

# --- detectors -------------------------------------------------------------

DECLARED = "declared"
NEGATION_MISMATCH = "negation_mismatch"
ANTONYM_PAIR = "antonym_pair"

_NEGATORS = {
    "no", "not", "never", "none", "cannot", "cant", "wont", "isnt", "arent",
    "doesnt", "dont", "didnt", "hasnt", "havent", "wasnt", "werent", "without",
    "lacks", "lacking", "absent", "denies", "denied", "refuses", "refused",
    "unable", "neither", "nor",
}

# Deliberately small and domain-neutral. Every pair here is one where asserting
# both about the same subject is incoherent rather than merely different. A large
# generated antonym list would fire on ordinary variation and bury the real ones.
_ANTONYMS = [
    ("increase", "decrease"), ("increases", "decreases"), ("increasing", "decreasing"),
    ("rising", "falling"), ("rose", "fell"), ("grew", "shrank"), ("growth", "decline"),
    ("above", "below"), ("higher", "lower"), ("more", "less"),
    ("required", "optional"), ("mandatory", "voluntary"), ("allowed", "forbidden"),
    ("permitted", "prohibited"), ("approved", "rejected"), ("accepted", "declined"),
    ("open", "closed"), ("opened", "closed"), ("active", "inactive"),
    ("available", "unavailable"), ("reachable", "unreachable"),
    ("centralized", "decentralized"), ("centralised", "decentralised"),
    ("expanding", "contracting"), ("renewed", "cancelled"), ("renewed", "canceled"),
    ("gained", "lost"), ("won", "lost"), ("started", "stopped"), ("began", "ended"),
]

_ANTONYM_INDEX = {}
for _a, _b in _ANTONYMS:
    _ANTONYM_INDEX.setdefault(_a, set()).add(_b)
    _ANTONYM_INDEX.setdefault(_b, set()).add(_a)

# Below this, two claims about one subject are simply about different aspects of
# it, and the negation signal means nothing.
OVERLAP_FLOOR = 0.35

_STOP = {
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "by", "is", "are",
    "was", "were", "be", "been", "and", "or", "but", "with", "that", "this",
    "it", "its", "as", "from", "they", "their", "we", "our", "has", "have",
}


def _tokens(text) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t]


def _content_tokens(text) -> set:
    return {t for t in _tokens(text) if t not in _STOP and len(t) > 1}


def overlap(a, b) -> float:
    """Jaccard over content words — how much two claims are ABOUT the same thing."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _negation_count(text) -> int:
    return sum(1 for t in _tokens(text) if t in _NEGATORS)


def detect(a_text: str, b_text: str) -> tuple[str, str] | None:
    """`(detector, reason)` if these two claims deterministically conflict.

    Requires lexical overlap first. Two claims that do not overlap are not in
    disagreement even when one of them is negated — "the buyer has no budget" and
    "procurement runs through a co-op" are both true and unrelated, and a detector
    that fired on the negator alone would report every such pair.
    """
    ov = overlap(a_text, b_text)
    if ov < OVERLAP_FLOOR:
        return None

    na, nb = _negation_count(a_text), _negation_count(b_text)
    if (na > 0) != (nb > 0):
        return (NEGATION_MISMATCH,
                f"same subject (overlap {ov:.2f}); one claim is negated and the other is not")

    ta, tb = _content_tokens(a_text), _content_tokens(b_text)
    for tok in sorted(ta - tb):
        hits = _ANTONYM_INDEX.get(tok, set()) & tb
        if hits:
            return (ANTONYM_PAIR,
                    f"same subject (overlap {ov:.2f}); '{tok}' vs '{sorted(hits)[0]}'")
    return None


# --- the observation -------------------------------------------------------

@dataclass
class Observation:
    """One recorded disagreement. Both sides, symmetric, no winner."""
    left_id: str
    right_id: str
    slot: str
    detector: str
    reason: str
    left_snapshot: str = ""
    right_snapshot: str = ""
    left_owner: str = ""
    right_owner: str = ""
    left_source: str = ""
    right_source: str = ""
    left_run_id: str = ""
    right_run_id: str = ""
    state: str = ACTIVE
    detected_run_id: str = ""
    detected_ts: str = ""
    resolved_ts: str = ""
    resolution: str = ""

    @property
    def pair_key(self) -> str:
        """Order-independent identity, so one disagreement is one row.

        Recording it twice — once per direction — would double every count and
        make "how many open disagreements are there" unanswerable.
        """
        lo, hi = sorted((self.left_id, self.right_id))
        return f"{lo}|{hi}|{self.slot}"

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "left_id", "right_id", "slot", "detector", "reason",
            "left_snapshot", "right_snapshot", "left_owner", "right_owner",
            "left_source", "right_source", "left_run_id", "right_run_id",
            "state", "detected_run_id", "detected_ts", "resolved_ts", "resolution")}
        d["pair_key"] = self.pair_key
        return d


def _claim_text(nk, props: dict) -> str:
    return str(props.get(nk.claim_field, "") or "") if nk and nk.claim_field else ""


def scan(schema, nodes: list[dict], *, run_id: str = "") -> list[Observation]:
    """Every deterministic disagreement among a set of ACTIVE nodes.

    `nodes` are dicts with kind / key / props / owner / source / run_id / status.
    Quadratic within a slot and linear across slots, which is fine because a slot
    holding enough claims for that to matter is itself the finding.
    """
    from .model import iid

    buckets: dict = {}
    for n in nodes:
        if n.get("status") not in (None, "", "active", "unmapped"):
            continue
        nk = schema.kind(n["kind"])
        if not nk or not nk.claim_field or not nk.claim_slot:
            continue
        slot = n.get("slot") or ""
        if not slot:
            continue
        buckets.setdefault((n["kind"], slot), []).append(n)

    out = []
    for (kind, slot), group in sorted(buckets.items()):
        nk = schema.kind(kind)
        group = sorted(group, key=lambda n: n["key"])
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                hit = detect(_claim_text(nk, a["props"]), _claim_text(nk, b["props"]))
                if not hit:
                    continue
                detector, reason = hit
                out.append(Observation(
                    left_id=iid(a["kind"], a["key"]), right_id=iid(b["kind"], b["key"]),
                    slot=slot, detector=detector, reason=reason,
                    left_snapshot=_claim_text(nk, a["props"]),
                    right_snapshot=_claim_text(nk, b["props"]),
                    left_owner=a.get("owner", ""), right_owner=b.get("owner", ""),
                    left_source=a.get("source", ""), right_source=b.get("source", ""),
                    left_run_id=a.get("run_id", ""), right_run_id=b.get("run_id", ""),
                    detected_run_id=run_id))
    return out


def declared(schema, nodes: list[dict], *, run_id: str = "") -> list[Observation]:
    """Disagreements the writer stated outright via `contradicts:`.

    Always recorded, with no detector gate. A writer who has read both sources
    and says they conflict is better evidence than any lexical rule, and the rule
    would miss most real cases — two sources can contradict each other without
    sharing a single content word.
    """
    from .model import iid

    by_id = {iid(n["kind"], n["key"]): n for n in nodes}
    out = []
    for n in nodes:
        targets = n["props"].get("contradicts")
        if targets is None:
            continue
        if not isinstance(targets, list):
            targets = [targets]
        a_id = iid(n["kind"], n["key"])
        nk = schema.kind(n["kind"])
        for t in targets:
            b = by_id.get(str(t))
            if b is None or str(t) == a_id:
                continue
            bnk = schema.kind(b["kind"])
            out.append(Observation(
                left_id=a_id, right_id=str(t), slot=n.get("slot") or b.get("slot") or "",
                detector=DECLARED, reason="declared by the writer via `contradicts:`",
                left_snapshot=_claim_text(nk, n["props"]),
                right_snapshot=_claim_text(bnk, b["props"]),
                left_owner=n.get("owner", ""), right_owner=b.get("owner", ""),
                left_source=n.get("source", ""), right_source=b.get("source", ""),
                left_run_id=n.get("run_id", ""), right_run_id=b.get("run_id", ""),
                detected_run_id=run_id))
    return out


def reconcile(schema, nodes: list[dict], stored: list[dict], *, run_id: str = "") -> dict:
    """Fold freshly detected disagreements into the stored ledger.

    Returns `{"upsert": [...], "resolve": [...], "reactivate": [...]}`.

    Idempotent by pair key: re-running over an unchanged graph produces no
    changes, so the ledger records disagreements rather than the history of the
    job having run.
    """
    found = {o.pair_key: o for o in scan(schema, nodes, run_id=run_id)}
    for o in declared(schema, nodes, run_id=run_id):
        found.setdefault(o.pair_key, o)

    stored_by_key = {s["pair_key"]: s for s in stored}
    upsert, resolve, reactivate = [], [], []

    for key, obs in found.items():
        prior = stored_by_key.get(key)
        if prior is None:
            upsert.append(obs)
        elif prior.get("state") == RESOLVED:
            reactivate.append(obs)
        elif (prior.get("left_snapshot"), prior.get("right_snapshot")) != \
             (obs.left_snapshot, obs.right_snapshot):
            upsert.append(obs)          # same pair, refreshed content snapshot

    for key, prior in stored_by_key.items():
        if prior.get("state") == ACTIVE and key not in found:
            resolve.append({
                "pair_key": key,
                "resolution": "one side changed, was superseded, or no longer conflicts",
            })
    return {"upsert": upsert, "resolve": resolve, "reactivate": reactivate}
