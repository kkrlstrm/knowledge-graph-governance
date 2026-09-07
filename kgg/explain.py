"""`kgg explain` — why does the graph believe this, and can that be trusted?

A write gate is only half an answer. It can prove a fact was *admitted* under
policy; it cannot prove the fact is still what you should act on. Everything that
goes wrong after a clean write is a read-time problem:

- the node was superseded and the reader is holding a prior belief
- the run that wrote it is not in the audit log, or the chain broke before it
- another active node contradicts it and nobody looked
- the quote is verbatim and the *interpretation* of it was wrong

The last one is the important one, and it is why this command shows evidence
rather than summarising it. A verbatim gate proves provenance, not
interpretation: text can be copied perfectly from a real source and still not
support the claim stapled to it. No check catches that. A human reading the claim
next to its own evidence does.

This is a **read over existing rows** — nodes, revisions, edges, the audit log,
the contradiction ledger. It builds no new store, and it is deliberately bounded
and explicit about truncation, because an explanation that quietly stopped early
reads exactly like a complete one.

Integrity is reported in three states, never collapsed into a boolean:

    verified      the node exists, its run resolves to an audit entry, and the
                  chain is intact through that entry
    unverified    the node exists but its provenance cannot be checked — no run
                  id, or a run id with no audit entry. NOT the same as "fine".
    invalidated   the chain is broken at or before this node's entry, or the node
                  is not active while being read as current
"""
from __future__ import annotations

from . import audit
from .backends.base import ReadSurfaceMissing
from .model import Schema, iid, split_iid

VERIFIED = "verified"
UNVERIFIED = "unverified"
INVALIDATED = "invalidated"

DEFAULT_VERSION_LIMIT = 20
MAX_VERSION_LIMIT = 200


def _audit_index(audit_log: str) -> tuple[dict, dict]:
    """`(run_id -> entry, chain_state)` for the whole log, read once."""
    chain = audit.verify(audit_log)
    entries = {}
    for i, entry in enumerate(audit.tail(audit_log, count=10**9), start=1):
        rid = entry.get("run_id")
        if rid:
            entries.setdefault(rid, {**entry, "line": i})
    return entries, chain


def _integrity(node: dict, entry: dict | None, chain: dict) -> dict:
    """Three-state integrity, with the reason spelled out."""
    if not node:
        return {"state": INVALIDATED, "reason": "node does not exist"}

    status = node.get("status", "active")
    run_id = node.get("run_id") or ""

    if status not in ("active", "unmapped"):
        return {"state": INVALIDATED,
                "reason": f"node status is '{status}' — it is not current truth"}
    if not run_id:
        return {"state": UNVERIFIED,
                "reason": "no ingest_run_id — written outside the gate, or before "
                          "provenance was stamped"}
    if entry is None:
        return {"state": UNVERIFIED,
                "reason": f"run '{run_id}' has no entry in the audit log; the write "
                          f"cannot be tied to a recorded, verifiable run"}
    if not chain.get("ok"):
        broken = chain.get("broken_at")
        if broken is not None and broken <= entry.get("line", 0):
            return {"state": INVALIDATED,
                    "reason": f"audit chain breaks at line {broken}, at or before this "
                              f"node's entry (line {entry.get('line')}): {chain.get('reason')}"}
        return {"state": UNVERIFIED,
                "reason": f"audit chain is broken later in the log (line {broken}); this "
                          f"node's own entry still verifies, but the log as a whole does not"}
    return {"state": VERIFIED,
            "reason": f"run '{run_id}' is entry {entry.get('line')} in an intact chain"}


def explain(backend, schema: Schema, target: str, *, scope: str = "",
            audit_log: str = "./audit.jsonl",
            version_limit: int = DEFAULT_VERSION_LIMIT) -> dict:
    """Everything known about one node and how far it can be trusted.

    `target` is a composite id, `kind:key`. Bare keys are refused rather than
    guessed: two kinds can share a key, and resolving that silently is how an
    explanation of the wrong node reads as authoritative.
    """
    from .backends.base import GLOBAL_PARTITION

    kind, key = split_iid(target)
    if not kind or not key:
        raise ValueError(f"{target!r} is not a composite id; use 'kind:key'")
    if schema.kind(kind) is None:
        raise ValueError(f"kind '{kind}' is not defined in schema '{schema.name}'")

    version_limit = max(1, min(int(version_limit), MAX_VERSION_LIMIT))
    identity_scope = scope if schema.scoped else GLOBAL_PARTITION

    out = {
        "id": iid(kind, key), "kind": kind, "key": key,
        "scope": identity_scope, "schema": schema.name,
        "schema_fingerprint": schema.fingerprint()[:12],
        "current": None, "versions": [], "edges": [],
        "contradictions": [], "gap": None,
        "traversal": {"version_limit": version_limit, "versions_truncated": False},
        "read_surface": True,
    }

    try:
        node = backend.read_node(kind, key, identity_scope)
    except ReadSurfaceMissing as exc:
        out["read_surface"] = False
        out["integrity"] = {"state": UNVERIFIED, "reason": str(exc)}
        return out

    entries, chain = _audit_index(audit_log)
    entry = entries.get((node or {}).get("run_id", "")) if node else None

    out["audit"] = {"chain_ok": chain.get("ok"), "chain_reason": chain.get("reason"),
                    "entries": chain.get("entries"), "broken_at": chain.get("broken_at"),
                    "entry": None}
    out["integrity"] = _integrity(node, entry, chain)

    if not node:
        return out

    nk = schema.kind(kind)
    out["current"] = {
        "props": node["props"], "status": node.get("status"),
        "revision": node.get("revision"), "owner": node.get("owner"),
        "is_protected": node.get("is_protected"),
        "valid_from": node.get("valid_from"), "slot": node.get("slot") or "",
        "claim": node["props"].get(nk.claim_field) if nk and nk.claim_field else None,
        "provenance": {
            "run_id": node.get("run_id"), "provenance_ts": node.get("provenance_ts"),
            "source": node["props"].get("source", ""),
            "ingested_by": node["props"].get("ingested_by", ""),
        },
    }
    if entry:
        out["audit"]["entry"] = {
            "line": entry.get("line"), "ts": entry.get("ts"), "event": entry.get("event"),
            "file": entry.get("file"), "owner": entry.get("owner"),
            "hash": entry.get("hash", "")[:12], "schema_hash": entry.get("schema_hash", "")[:12],
        }

    # Version chain. `valid_from`/`valid_until` are recorded but not yet
    # queryable — kgg has no as-of read, and saying so is better than implying
    # a time-travel guarantee the code does not make.
    revs = backend.read_revisions(kind, key, identity_scope, limit=version_limit + 1)
    out["traversal"]["versions_truncated"] = len(revs) > version_limit
    for r in revs[:version_limit]:
        out["versions"].append({
            "revision": r["revision"], "owner": r.get("owner", ""),
            "valid_until": r.get("valid_until", ""),
            "superseded_by_run": r.get("superseded_by_run", ""),
            "claim": r["props"].get(nk.claim_field) if nk and nk.claim_field else None,
            "props": r["props"],
        })

    try:
        out["edges"] = backend.read_edges(kind, key, identity_scope)
    except ReadSurfaceMissing:
        out["edges"] = []

    # Competing claims. Reported, never resolved — the ledger holds both sides
    # and this read does not pick between them either.
    try:
        me = iid(kind, key)
        for c in backend.read_contradictions("all"):
            if c.get("left_id") == me or c.get("right_id") == me:
                other = c["right_id"] if c.get("left_id") == me else c["left_id"]
                out["contradictions"].append({
                    "with": other, "state": c.get("state"), "detector": c.get("detector"),
                    "reason": c.get("reason"),
                    "this_side": c.get("left_snapshot") if c.get("left_id") == me
                                 else c.get("right_snapshot"),
                    "other_side": c.get("right_snapshot") if c.get("left_id") == me
                                  else c.get("left_snapshot"),
                    "other_source": c.get("right_source") if c.get("left_id") == me
                                    else c.get("left_source"),
                    "resolved_ts": c.get("resolved_ts", ""),
                })
    except ReadSurfaceMissing:
        pass

    if node.get("status") == "unmapped":
        slug = node["props"].get("unmapped_gap", "")
        out["gap"] = {"slug": slug, "reason": node["props"].get("unmapped_reason", "")}

    if out["contradictions"] and any(c["state"] == "active" for c in out["contradictions"]):
        out["current"]["status_note"] = (
            "another active claim in this slot disagrees; both are held, neither wins")

    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_GLYPH = {VERIFIED: "✓", UNVERIFIED: "?", INVALIDATED: "✗"}


def render(x: dict) -> str:
    L = []
    integ = x.get("integrity", {})
    L.append(f"\n=== kgg explain  {x['id']} ===")
    L.append(f"  schema={x['schema']}  scope={x['scope']}  fingerprint={x['schema_fingerprint']}…")
    L.append(f"  integrity: {_GLYPH.get(integ.get('state'), '?')} {str(integ.get('state', '?')).upper()}"
             f" — {integ.get('reason', '')}")

    if not x.get("read_surface"):
        L.append("\n  (this backend implements governed writes but not the read surface)")
        return "\n".join(L)
    cur = x.get("current")
    if not cur:
        L.append("\n  no such node.")
        return "\n".join(L)

    p = cur["provenance"]
    nk_claim = None
    L.append(f"\n  CURRENT (revision {cur['revision']}, status {cur['status']})")
    if cur.get("claim"):
        L.append(f"      claim: {str(cur['claim']).strip()}")
        # Don't print it twice: the claim field is shown above, in the role that
        # matters, and repeating it verbatim under its raw field name buries the
        # rest of the props.
        nk_claim = next((k for k, v in cur["props"].items() if v == cur["claim"]), None)
    for k, v in sorted(cur["props"].items()):
        if k in ("owner", "ingested_by", "ingest_run_id", "provenance_ts",
                 "is_protected", "source", nk_claim):
            continue
        L.append(f"      {k}: {str(v).strip() if isinstance(v, str) else v}")
    L.append(f"      owner={cur['owner']}  protected={cur['is_protected']}  "
             f"valid_from={cur.get('valid_from')}")
    L.append(f"      run={p['run_id']}  source={p['source']}  at={p['provenance_ts']}")
    if cur.get("status_note"):
        L.append(f"      ⚠ {cur['status_note']}")

    a = x.get("audit", {})
    if a.get("entry"):
        e = a["entry"]
        L.append(f"\n  AUDIT  line {e['line']}  {e['ts']}  {e['event']}  hash {e['hash']}…")
        L.append(f"      chain: {a['chain_reason']} ({a['entries']} entries)")
    else:
        L.append(f"\n  AUDIT  no entry for this node's run "
                 f"(chain: {a.get('chain_reason')})")

    if x["versions"]:
        L.append(f"\n  PRIOR BELIEFS ({len(x['versions'])}"
                 + (", truncated" if x["traversal"]["versions_truncated"] else "") + ")")
        for v in x["versions"]:
            L.append(f"      r{v['revision']}  until {v['valid_until']}  "
                     f"by run {v['superseded_by_run']}")
            if v.get("claim"):
                L.append(f"          {str(v['claim']).strip()}")
    else:
        L.append("\n  PRIOR BELIEFS  none — this node has never been superseded")

    if x["contradictions"]:
        active = [c for c in x["contradictions"] if c["state"] == "active"]
        L.append(f"\n  COMPETING CLAIMS ({len(active)} active / {len(x['contradictions'])} total)"
                 "  — both held, no winner picked")
        for c in x["contradictions"]:
            mark = "●" if c["state"] == "active" else "○"
            L.append(f"      {mark} vs {c['with']}  [{c['detector']}] {c['reason']}")
            L.append(f"          this : {c['this_side']}")
            L.append(f"          other: {c['other_side']}"
                     + (f"   (source: {c['other_source']})" if c.get("other_source") else ""))

    if x["edges"]:
        L.append(f"\n  REFERENCES ({len(x['edges'])})")
        for e in x["edges"]:
            L.append(f"      -{e['field']}-> {e['dst_kind']}:{e['dst_key']}")

    if x.get("gap"):
        L.append(f"\n  UNMAPPED  gap '{x['gap']['slug']}'")
        if x["gap"].get("reason"):
            L.append(f"      {x['gap']['reason']}")

    L.append("\n  Evidence is shown, not summarised: a verbatim quote proves where text")
    L.append("  came from, never that it supports the claim. Read them together.")
    return "\n".join(L)
