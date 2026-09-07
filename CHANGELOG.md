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
- As-of reads over the `valid_from` / `valid_until` already recorded.
- Neo4j exercised in CI via Testcontainers (SQLite is the CI-tested path today).
- A config-DSL for lower-friction schemas, and a Postgres backend.

## [Unreleased] — near-duplicate keys

### Added
- **Near-duplicate key detection** — `near_duplicate_check: true` per kind holds a
  new key that is nearly an existing one (`Acme Robotics, Inc.` against
  `Acme Robotics`) as REQUIRE_APPROVAL. It **never merges**: approving creates a
  separate node and leaves the original at revision 1. Off by default, because
  whether two similar keys name one thing is a domain question — company names
  merge, place names do not.

  Trigram Jaccard over a normalised key, gated on Shannon entropy first (short or
  repetitive keys are excluded from comparison rather than compared more
  strictly), blocked on a 4-character prefix to stay near-linear. The threshold
  was calibrated against four named pairs recorded in `kgg/similarity.py`;
  `test_similarity_calibration_pairs` pins them. Known miss, measured and stated:
  a long suffix on a short base scores low (`Globex` / `Globex Corporation` is
  0.27) and is not flagged.

  Two bugs of my own, found by writing the conformance case: a `len // 8` block
  band put `acme robotics` and `acme robotics inc` in different blocks and so
  missed the exact pair the check exists for, and the initial 0.86 floor was
  carried over from tools that threshold a different metric — trigram Jaccard
  scores systematically lower than normalised edit distance, so the two numbers
  were never comparable.

## [0.2.0] — 2026-09-07

The gate grows a read path, a way to disagree, and a way for the vocabulary to
change — plus a conformance suite that turns every documented guarantee into a
runnable refusal.

### Added
- **`kgg conformance`** — the documented guarantees as executable cases. 19 cases
  across 15 guarantees in `examples/conformance/`, each declaring its ingest
  files, operations, and the exact per-item verdict expected at every step. Runs
  in CI as its own job and publishes a guarantee table. A gate is a claim about
  what it refuses; prose cannot discharge one.
- **`kgg explain <kind:key>`** — why the graph believes something, and how far
  that can be trusted. Current value, prior beliefs, references, competing
  claims, the audit entry the node's run resolves to, and a three-state
  `verified` / `unverified` / `invalidated` integrity verdict (exit 0/4/5). A read
  over existing rows; no second store. Shows evidence rather than summarising it,
  because a verbatim gate proves provenance, not interpretation.
- **Contradiction ledger** — `kgg contradictions`. Two active nodes sharing a
  declared `claim_slot` whose `claim_field` deterministically conflicts (negation
  mismatch or antonym pair over a lexical-overlap floor; no model call) are
  recorded as one order-independent observation. Advisory, never a third truth
  value: both claims keep their status and provenance, the ledger never picks a
  winner, and resolution retains the row with both snapshots. `contradicts:` on an
  item records one outright.
- **Gap promotion** — `kgg gaps [list|known|promote|reject]`. An unmapped item must
  name the missing mechanic in a validated kebab-case `unmapped_gap` slug; slugs
  that name the ingest's scope, or name the writer's state rather than a mechanic,
  are refused. Gaps accumulate across runs and rank by **independent scopes**, not
  observation count. `kgg gaps known` emits the list to hand the next writer —
  convergence on a closed set is a lookup, on an open set it is telepathy.
- **Curated vocabularies** — a vocabulary now carries `version`, declared
  `synonyms` (folded on read and reported, never inferred), and a
  `deprecated` / `replaced_by` path that holds for approval and rewrites the term
  instead of blocking the writer. The flat `term: "definition"` form still loads.
- **Schema-declared domain check packs** — `checks: [module:REGISTRY]` or
  `checks: [file.py:REGISTRY]`, resolved relative to the schema like
  `vocabularies:` already is. Declared in the contract rather than on the command
  line, so which rules are in force is inside the schema fingerprint. A pack
  cannot shadow a kernel check name, and an unloadable pack fails the contract
  closed. Example in `examples/gtm/domain_checks.py`.
- **Optional backend read surface** — `read_node` / `read_revisions` /
  `read_edges` / `read_nodes` plus the contradiction and gap ledgers, on both
  SQLite and Neo4j. Raises `ReadSurfaceMissing` rather than returning empty: a
  reader that answered "no revisions" when it could not look would present an
  unexamined node as a clean one.
- `kgg validate-schema` now reports vocabulary version, alias and deprecation
  counts, and the domain checks in force.

### Fixed
- **`kgg approve --file` did not bind the schema fingerprint.** It selected a
  proposal by content hash alone, so an approval could attach to a proposal built
  under a *different* governance contract; `ingest` then filtered on both hashes,
  found no approvals, and left the item held. The human approved, nothing
  happened, exit code 0. `--file` now binds the schema and refuses an ambiguous
  match; `--schema` is accepted (and required when one file has proposals under
  more than one contract). `approved_ids` re-checks both hashes as defence in
  depth against a hand-edited proposal file.
- **A run-level `BLOCK` blocked nothing.** Item verdicts are matched by target
  and no item's target is `RUN`, so a check condemning the whole file — a
  malformed date, a bad top-level field — was recorded in the proposal and then
  every item applied anyway. The worst run-level verdict is now a floor under
  every item, and the plan says which run-level check refused it. Found by writing
  the `graded-verdicts` conformance case.
- **Run ids collided at second resolution.** The proposal file is named after the
  run id, so two dry-runs of one file within the same second — two schemas, or a
  script looping a directory — produced the same id and the second silently
  overwrote the first proposal, taking any approvals recorded against it. Ids now
  carry microseconds and a random suffix. Found by writing the
  `approvals-bound-to-policy` conformance case.
- **The SQLite backend dropped `source` and `ingested_by`.** Both were stamped
  into provenance and written by the Neo4j backend, but SQLite kept only the
  columns and discarded the rest, so a node's recorded origin depended on which
  store it landed in. Provenance is now merged into the stored document on both.
- `kgg approve` exits non-zero and names every `--items` token that matched
  nothing, with the reason (ambiguous bare key / not awaiting approval / no such
  item). A partial approval that reads as a completed one is how three ids get
  typed and two get cleared.

### Changed
- An unmapped item now **requires** an `unmapped_gap` slug. Set
  `require_gap_slug: false` in the schema to opt out.
- `CHECK_REGISTRY` is now `CORE_CHECKS`; the old name remains as an alias.
- `run_checks(ctx)` takes an optional `extra=` registry and runs a vocabulary
  normalisation pass before the checks.
- SQLite gains `contradictions`, `gap_observations` and `gap_resolutions` tables
  and a `nodes.slot` column; an existing 0.1.0 database is migrated in place on
  open rather than failing on a missing column.

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
