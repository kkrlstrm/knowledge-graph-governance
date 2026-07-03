"""Backend adapter interface.

A backend is the only thing that touches a real store. The engine, checks, and
kernel never import a database — they call these five methods, so adding a
store means implementing this interface, nothing else.

Contract:
  read_existing(schema)  -> {(kind, key): ExistingNode}   reads content_fields +
                            provenance so checks can compare/guard.
  create(kind, key, props, prov, status)                  new node, provenance stamped.
  supersede(kind, key, props, prov, run_id) -> int         snapshot prior belief,
                            update in place, return the new revision number.
  link(src_kind, src_key, field, dst_kind, dst_key)        idempotent edge.
  close()
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..model import Schema, NodeKind, ExistingNode


class Backend(ABC):
    @abstractmethod
    def read_existing(self, schema: Schema) -> dict:
        ...

    @abstractmethod
    def create(self, kind: NodeKind, key: str, props: dict, prov: dict, status: str) -> None:
        ...

    @abstractmethod
    def supersede(self, kind: NodeKind, key: str, props: dict, prov: dict, run_id: str) -> int:
        ...

    @abstractmethod
    def link(self, src_kind: str, src_key: str, field: str, dst_kind: str, dst_key: str) -> None:
        ...

    def close(self) -> None:
        pass


def open_backend(dsn: str) -> Backend:
    """dsn: 'sqlite:<path>' | 'neo4j:<uri>' (auth from NEO4J_* env)."""
    if dsn.startswith("sqlite:"):
        from .sqlite import SqliteBackend
        return SqliteBackend(dsn[len("sqlite:"):])
    if dsn.startswith("neo4j:") or dsn.startswith("bolt:") or dsn.startswith("neo4j+s:"):
        from .neo4j import Neo4jBackend
        return Neo4jBackend(dsn)
    raise ValueError(f"unrecognized backend dsn: {dsn!r} (use sqlite:<path> or neo4j:<uri>)")
