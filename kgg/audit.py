"""Hash-chained, tamper-evident audit log.

Each entry is chained to the previous one's hash:

    entry.hash = sha256( prev_hash + canonical_json(payload) )

Genesis prev_hash is 64 zeros. Any post-hoc edit to a payload changes that
line's hash, which breaks every subsequent link; `verify()` returns the first
broken link. Pure local file + hashing — no database, no dependencies.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode()).hexdigest()


def last_hash(log_path: str | Path) -> str:
    p = Path(log_path)
    if not p.exists():
        return GENESIS
    last = GENESIS
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "hash" in rec:
                last = rec["hash"]
    return last


def append(log_path: str | Path, payload: dict) -> dict:
    """Append one chained entry. Returns the written record (incl. its hash)."""
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = last_hash(p)
    body = dict(payload)
    body.setdefault("ts", datetime.now(timezone.utc).isoformat())
    entry = {"prev_hash": prev, **body}
    entry["hash"] = _hash(prev, body)
    with p.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def verify(log_path: str | Path) -> dict:
    """Walk the chain. Returns {ok, entries, broken_at, reason}."""
    p = Path(log_path)
    if not p.exists():
        return {"ok": True, "entries": 0, "broken_at": None, "reason": "no log yet"}

    prev = GENESIS
    n = 0
    with p.open() as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "entries": n, "broken_at": lineno,
                        "reason": "unparseable JSON"}
            if "hash" not in rec or "prev_hash" not in rec:
                return {"ok": False, "entries": n, "broken_at": lineno,
                        "reason": "entry missing hash/prev_hash (not chained)"}
            if rec["prev_hash"] != prev:
                return {"ok": False, "entries": n, "broken_at": lineno,
                        "reason": "prev_hash mismatch (line inserted/removed/reordered)"}
            body = {k: v for k, v in rec.items() if k not in ("prev_hash", "hash")}
            if _hash(prev, body) != rec["hash"]:
                return {"ok": False, "entries": n, "broken_at": lineno,
                        "reason": "payload edited after write (hash mismatch)"}
            prev = rec["hash"]
            n += 1
    return {"ok": True, "entries": n, "broken_at": None, "reason": "chain intact"}


def tail(log_path: str | Path, count: int = 10) -> list[dict]:
    p = Path(log_path)
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out[-count:]
