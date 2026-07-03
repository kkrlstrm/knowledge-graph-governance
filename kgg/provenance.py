"""Per-node provenance, as queryable node properties.

Provenance is first-class: every written node carries these properties, so
"which run wrote (or last changed) this node, and was that run tampered with?"
is a query (via ingest_run_id -> the hash-chained audit entry), not a
log-scrape.

    source        free-text origin
    owner         who controls this node (default: the ingest owner)
    is_protected  if true, only the owner may overwrite it (enforced by the
                  protected_node_guard check)
    ingested_by   the tool that wrote it
    ingest_run_id ties the node to an audit entry
    provenance_ts when provenance was last stamped
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


def default_owner() -> str:
    return os.getenv("KGG_OWNER") or os.getenv("USER") or "kgg"


@dataclass
class Provenance:
    owner: str
    ingest_run_id: str
    source: str
    ingested_by: str = "knowledge-graph-governance"
    provenance_ts: str = ""

    def __post_init__(self):
        if not self.provenance_ts:
            self.provenance_ts = datetime.now(timezone.utc).isoformat()

    def as_props(self, *, is_protected: bool = False) -> dict:
        d = asdict(self)
        d["is_protected"] = bool(is_protected)
        return d


def stamp(owner: str, run_id: str, source: str, *, is_protected: bool = False) -> dict:
    return Provenance(owner=owner, ingest_run_id=run_id, source=source).as_props(
        is_protected=is_protected
    )
