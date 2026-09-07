"""Backend adapter interface.

A backend is the only thing that touches a real store. The engine, checks, and
kernel never import a database — they call these methods, so adding a store
means implementing this interface, nothing else.

Identity: `identity_scope` is the partition a node lives in. In a GLOBAL-identity
schema it is the constant "*" (so (kind, key) is globally unique and the ingest's
scope is only run metadata). In a SCOPED schema it is the ingest scope, so the
true identity is (scope, kind, key) and the same key can exist per tenant/seller.

Atomicity: an apply runs inside `transaction()`. Backends commit on clean exit
and roll back on any exception, so a crash mid-apply leaves no partial writes —
and the caller writes the audit entry only after the transaction commits.

Fail-closed: `create` must fail (not overwrite) if the node already exists.

Two surfaces, and only the first is required:

  WRITE (5 methods, abstract)  read_existing / create / supersede / link / close
  READ  (optional)             everything `kgg explain`, `kgg contradictions`
                               and `kgg gaps` need

The read surface is optional so a store can be wired up for governed writes
without implementing history or ledger queries. It raises `ReadSurfaceMissing`
rather than returning empty: a reader that answered "no revisions" when it simply
could not look would present an unexamined node as a clean one, which is the
failure this whole repo is about.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager

from ..model import Schema, NodeKind

GLOBAL_PARTITION = "*"


class ReadSurfaceMissing(NotImplementedError):
    """This backend supports governed writes but not the inspection queries."""


class Backend(ABC):
    # --- write surface (required) ---
    @abstractmethod
    def read_existing(self, schema: Schema, scope: str) -> dict:
        """{(kind, key): ExistingNode} — scope-filtered in a SCOPED schema."""

    @abstractmethod
    def create(self, kind: NodeKind, key: str, props: dict, prov: dict,
               status: str, identity_scope: str) -> None:
        """Create a new node. MUST raise if (identity_scope, kind, key) exists."""

    @abstractmethod
    def supersede(self, kind: NodeKind, key: str, props: dict, prov: dict,
                  run_id: str, identity_scope: str) -> int:
        """Snapshot the prior belief, update in place, return the new revision."""

    @abstractmethod
    def link(self, src_kind: str, src_key: str, field: str, dst_kind: str,
             dst_key: str, identity_scope: str) -> None:
        """Idempotent edge."""

    # --- read surface (optional; override to support explain/contradictions/gaps) ---
    def read_node(self, kind: str, key: str, identity_scope: str) -> dict | None:
        """Full stored row for one node, or None. Props, provenance, status."""
        raise ReadSurfaceMissing(f"{type(self).__name__} does not implement read_node")

    def read_revisions(self, kind: str, key: str, identity_scope: str,
                       limit: int = 20) -> list[dict]:
        """Prior beliefs, newest revision first."""
        raise ReadSurfaceMissing(f"{type(self).__name__} does not implement read_revisions")

    def read_edges(self, kind: str, key: str, identity_scope: str) -> list[dict]:
        """Outgoing reference edges for one node."""
        raise ReadSurfaceMissing(f"{type(self).__name__} does not implement read_edges")

    def read_nodes(self, schema: Schema, scope: str) -> list[dict]:
        """Every node with full props — what contradiction detection scans."""
        raise ReadSurfaceMissing(f"{type(self).__name__} does not implement read_nodes")

    def read_contradictions(self, state: str = "active") -> list[dict]:
        raise ReadSurfaceMissing(f"{type(self).__name__} has no contradiction ledger")

    def write_contradictions(self, upsert, resolve, reactivate) -> dict:
        raise ReadSurfaceMissing(f"{type(self).__name__} has no contradiction ledger")

    def record_gaps(self, rows: list[dict]) -> int:
        raise ReadSurfaceMissing(f"{type(self).__name__} has no gap registry")

    def read_gap_rows(self) -> list[dict]:
        raise ReadSurfaceMissing(f"{type(self).__name__} has no gap registry")

    def read_gap_resolutions(self) -> dict:
        raise ReadSurfaceMissing(f"{type(self).__name__} has no gap registry")

    def set_gap_resolution(self, slug: str, status: str, promoted_to: str = "",
                           note: str = "", actor: str = "") -> None:
        raise ReadSurfaceMissing(f"{type(self).__name__} has no gap registry")

    @property
    def has_read_surface(self) -> bool:
        return type(self).read_node is not Backend.read_node

    # --- transactions (override; defaults make a no-op single-statement store) ---
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

    @contextmanager
    def transaction(self):
        self.begin()
        try:
            yield
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

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
