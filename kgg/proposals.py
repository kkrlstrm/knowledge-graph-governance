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
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .verdicts import Finding, RUN


def content_hash(file_path: str | Path) -> str:
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def new_run_id(file_path: str | Path) -> str:
    """A run id that is unique per run, not per (file, second).

    The proposal file is named after the run id, so a collision silently
    overwrites the earlier proposal — and with it every approval recorded against
    that run. Second resolution collides in ordinary use: two dry-runs of one
    file under two schemas, or a script looping over a directory, land in the
    same second with the same content hash and produce the same id.
    """
    stem = Path(file_path).stem
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    short = content_hash(file_path)[:8]
    return f"{stem}-{stamp}-{short}-{secrets.token_hex(2)}"


def build(run_id: str, file_path: str, owner: str, scope: str,
          plans: list, findings: list[Finding], schema_hash: str = "",
          identity: str = "global", checks: list | None = None) -> dict:
    return {
        "run_id": run_id,
        "file": str(file_path),
        "content_hash": content_hash(file_path),
        "schema_hash": schema_hash,
        "identity": identity,
        # The domain rules that were in force. Inside the schema fingerprint
        # already; recorded here so a proposal reads as a complete record of what
        # was enforced without re-resolving the schema.
        "domain_checks": list(checks or []),
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


def find_all_for_hash(proposal_dir: str | Path, chash: str,
                      schema_hash: str | None = None) -> list[dict]:
    """Every proposal matching the content hash (and schema hash, if given),
    newest first. `find_for_hash` returns the newest; the approve path needs the
    full list so it can refuse an ambiguous match instead of guessing."""
    d = Path(proposal_dir)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            prop = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if prop.get("content_hash") != chash:
            continue
        if schema_hash is not None and prop.get("schema_hash", "") != schema_hash:
            continue
        out.append(prop)
    out.sort(key=lambda p: p.get("created_ts", ""), reverse=True)
    return out


def find_for_hash(proposal_dir: str | Path, chash: str, schema_hash: str | None = None) -> dict | None:
    """Latest proposal matching the file content hash — and, if given, the schema
    hash. Binding on both means neither an edited ingest file nor a changed
    governance policy can silently reuse a stale approval."""
    found = find_all_for_hash(proposal_dir, chash, schema_hash)
    return found[0] if found else None


def approved_ids(proposal: dict | None) -> set:
    """The set of composite ids (kind:key) a human has cleared.

    An approval record carries the content + schema hash it was granted under.
    The engine already selects the proposal by both hashes, so this is defence in
    depth: a hand-edited or hand-merged proposal file whose approval records name
    a different policy contributes nothing. An approval that predates the hash
    fields (empty string) is honoured — it can only have come from a proposal
    that already matched.
    """
    if not proposal:
        return set()
    want_schema = proposal.get("schema_hash", "")
    want_content = proposal.get("content_hash", "")
    ids = set()
    for a in proposal.get("approvals", []):
        got_schema = a.get("schema_hash", "")
        got_content = a.get("content_hash", "")
        if got_schema and got_schema != want_schema:
            continue
        if got_content and got_content != want_content:
            continue
        ids.update(a.get("items", []))
    return ids


def _held_index(proposal: dict):
    held = {it["id"] for it in proposal["items"] if it["verdict"] == "REQUIRE_APPROVAL"}
    by_key = {}
    for it in proposal["items"]:
        if it["verdict"] == "REQUIRE_APPROVAL":
            by_key.setdefault(it["key"], []).append(it["id"])
    return held, by_key


def resolve_ids(proposal: dict, tokens: list[str]) -> list[str]:
    """Map CLI --items tokens ('kind:key' or a bare key) to held composite ids."""
    resolved, _ = resolve_ids_reporting(proposal, tokens)
    return resolved


def resolve_ids_reporting(proposal: dict, tokens: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """`(resolved, unmatched)` where unmatched is [(token, reason), ...].

    Silently dropping a token a human typed is how a partial approval reads as a
    completed one. The CLI exits non-zero on any unmatched token rather than
    reporting a count the reader has to reconcile themselves.
    """
    held, by_key = _held_index(proposal)
    resolved, unmatched = [], []
    for t in tokens:
        if t in held:
            resolved.append(t)
        elif t in by_key and len(by_key[t]) == 1:   # unambiguous bare key
            resolved.append(by_key[t][0])
        elif t in by_key:
            unmatched.append((t, f"ambiguous bare key — matches {', '.join(sorted(by_key[t]))}"))
        elif any(it["id"] == t or it["key"] == t for it in proposal["items"]):
            unmatched.append((t, "present in this proposal but not awaiting approval"))
        else:
            unmatched.append((t, "no such item in this proposal"))
    return resolved, unmatched


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
