# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Planned
- Optimistic-concurrency revisions (compare-and-swap on supersede) so two
  planners against one stale snapshot can't silently conflict.
- External audit anchoring (Git commit / object-store version / signed
  timestamp) for tamper-*proof*, not just tamper-evident, guarantees.
- Neo4j exercised in CI via Testcontainers (SQLite is the CI-tested path today).
- A config-DSL for lower-friction schemas, a read/query companion, and a
  Postgres backend.

## [0.1.0] — 2026-07-03

First public release: the deterministic write gate for agent-managed knowledge
graphs. Writers propose changes as YAML; only validated, versioned,
provenance-stamped writes reach the graph.

### Added
- **Write-gate kernel** (no database import): graded verdicts
  (`ALLOW`/`REQUIRE_APPROVAL`/`BLOCK`/`HALT`), a named-check registry,
  per-node provenance, staged proposals, and a hash-chained audit log.
- **Generic schema model** — node kinds, controlled vocabularies, references,
  and content fields declared in YAML, so the same engine governs any domain.
- **Composite identity** `(kind, key)` across findings, plans, proposals,
  approvals, and audit actions — two kinds sharing a key can't collide.
- **Identity modes** — `global` (`(kind, key)` unique; scope is run metadata)
  and `scoped` (`(scope, kind, key)`, partitioned per tenant/seller/team).
- **Transactional apply** — writes run in one backend transaction; a mid-apply
  failure rolls back with no partial graph, and the audit entry is written only
  after commit.
- **Fail-closed creation** — plain `INSERT` / `CREATE` against a uniqueness
  constraint; the storage layer refuses to overwrite rather than replace/merge.
- **Approvals bound to policy** — matched to both the file content hash and the
  schema fingerprint, so a changed contract or edited file drops stale approvals.
- **`kgg validate-schema`** — validates the governance contract itself.
- **Backends** — zero-infra SQLite (nodes + revision history + edges in one
  file) and Neo4j (shared `:KggNode` label, composite uniqueness constraint,
  `:SUPERSEDES` revision snapshots), behind a five-method adapter interface.
- **CLI** — `ingest`, `approve`, `validate-schema`, `verify-audit` (prints the
  chain head for external anchoring).
- **GTM example pack** — a universal, org-agnostic go-to-market taxonomy
  (7 categories) as a working schema and a genuinely reusable starter ontology.
- Test suite (SQLite end-to-end + kernel + hardening) and CI on Python
  3.10–3.12.

### Fixed
- Re-ingesting an already-existing *unmapped* node no longer attempts to
  re-create it — `unmapped_review` fires only for new observations, so
  re-ingest is idempotent (surfaced once creation became fail-closed).

### Security / integrity notes
- The audit log is **tamper-evident, not tamper-proof**: it detects any edit,
  reorder, insertion, or deletion, but a full rewrite can regenerate a valid
  chain. Anchor the head hash externally for a stronger guarantee.
