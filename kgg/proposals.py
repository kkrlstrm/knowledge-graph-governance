"""Staged proposal + approval records.

A dry-run doesn't just print — it writes a durable proposal artifact pairing
the file with its per-item verdicts, the named-check findings, an approval
record, and a content hash. The common path (everything ALLOW) still applies
in one command; the gate only bites when items are HELD for approval.

The proposal is matched to a file by content hash, so editing the source
invalidates stale approvals automatically (a changed file needs re-review).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .verdicts import Finding, RUN


def content_hash(file_path: str | Path) -> str:
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def new_run_id(file_path: str | Path) -> str:
    stem = Path(file_path).stem
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = content_hash(file_path)[:8]
    return f"{stem}-{stamp}-{short}"


def build(run_id: str, file_path: str, owner: str, scope: str,
          plans: list, findings: list[Finding], schema_hash: str = "",
          identity: str = "global") -> dict:
    return {
        "run_id": run_id,
        "file": str(file_path),
        "content_hash": content_hash(file_path),
        "schema_hash": schema_hash,
        "identity": identity,
        "created_ts": datetime.now(timezone.utc).isoformat(),
        "owner": owner,
        "scope": scope,
        "status": "pending",
        "run_findings": [f.to_dict() for f in findings if f.target == RUN],
        "items": [
            {
                "id": p.id,               # composite identity (kind:key)
                "kind": p.kind,
                "key": p.key,
                "verdict": p.verdict.label,
                "action": p.action,
                "detail": p.detail,
                "approved": p.approved,
                "findings": [f.to_dict() for f in p.findings],
            }
            for p in plans
        ],
        "approvals": [],
        "applied": None,
    }


def path_for(proposal_dir: str | Path, run_id: str) -> Path:
    return Path(proposal_dir) / f"{run_id}.json"


def save(proposal_dir: str | Path, proposal: dict) -> Path:
    d = Path(proposal_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = path_for(d, proposal["run_id"])
    p.write_text(json.dumps(proposal, indent=2, default=str))
    return p


def load(proposal_dir: str | Path, run_id: str) -> dict | None:
    p = path_for(proposal_dir, run_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def find_for_hash(proposal_dir: str | Path, chash: str, schema_hash: str | None = None) -> dict | None:
    """Latest proposal matching the file content hash — and, if given, the schema
    hash. Binding on both means neither an edited ingest file nor a changed
    governance policy can silently reuse a stale approval."""
    d = Path(proposal_dir)
    if not d.exists():
        return None
    best = None
    for f in sorted(d.glob("*.json")):
        try:
            prop = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if prop.get("content_hash") != chash:
            continue
        if schema_hash is not None and prop.get("schema_hash", "") != schema_hash:
            continue
        if best is None or prop.get("created_ts", "") >= best.get("created_ts", ""):
            best = prop
    return best


def approved_ids(proposal: dict | None) -> set:
    """The set of composite ids (kind:key) a human has cleared."""
    if not proposal:
        return set()
    ids = set()
    for a in proposal.get("approvals", []):
        ids.update(a.get("items", []))
    return ids


def resolve_ids(proposal: dict, tokens: list[str]) -> list[str]:
    """Map CLI --items tokens ('kind:key' or a bare key) to held composite ids."""
    held = {it["id"] for it in proposal["items"] if it["verdict"] == "REQUIRE_APPROVAL"}
    by_key = {}
    for it in proposal["items"]:
        if it["verdict"] == "REQUIRE_APPROVAL":
            by_key.setdefault(it["key"], []).append(it["id"])
    out = []
    for t in tokens:
        if t in held:
            out.append(t)
        elif t in by_key and len(by_key[t]) == 1:   # unambiguous bare key
            out.append(by_key[t][0])
    return out


def record_approval(proposal: dict, items: list[str], approver: str,
                    note: str = "", approve_all: bool = False):
    held = [it["id"] for it in proposal["items"] if it["verdict"] == "REQUIRE_APPROVAL"]
    granted = held if approve_all else resolve_ids(proposal, items)
    proposal["approvals"].append({
        "items": granted,
        "approver": approver,
        "note": note,
        "schema_hash": proposal.get("schema_hash", ""),
        "content_hash": proposal.get("content_hash", ""),
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    granted_set = set(granted)
    for it in proposal["items"]:
        if it["id"] in granted_set:
            it["approved"] = True
    return proposal, granted


def mark_applied(proposal: dict, summary: dict) -> dict:
    proposal["applied"] = {"ts": datetime.now(timezone.utc).isoformat(), **summary}
    held_remaining = any(
        it["verdict"] == "REQUIRE_APPROVAL" and not it["approved"]
        for it in proposal["items"]
    )
    proposal["status"] = "partial" if held_remaining else "applied"
    return proposal
