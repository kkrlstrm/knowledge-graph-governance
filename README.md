# kgg — the write gate for agent-managed knowledge graphs

<!-- portfolio-status -->
**Status:** Reference implementation — extracted from a private production GTM system; tenant data, provider adapters, and company-specific policy stay private. · **Layer:** Knowledge governance · **[Portfolio map ›](https://github.com/kkrlstrm)**

## Agents should not get direct write access to your knowledge graph.

Knowledge graphs are becoming the memory layer for AI systems. Most agentic setups skip the dangerous part: **the write path.** Everyone tells you to give your agent a knowledge graph. Nobody ships the gate that keeps it from corrupting one.

Let an LLM emit raw Cypher, SQL, or graph mutations and it will eventually:

- invent node types and drift your taxonomy
- create phantom entities from a stray edge
- overwrite prior beliefs with no history
- lose provenance — nobody knows where a fact came from
- make post-hoc audit impossible

**LLM extraction is probabilistic. Graph memory is durable. Those two things should not be wired together directly.**

`kgg` is the deterministic write gate between them. Writers — LLMs, scripts, or humans — propose changes as YAML. `kgg` validates the proposal against your schema and controlled vocabulary, refuses implicit node creation, versions changed beliefs, holds ambiguous items for approval, stamps provenance, and appends a hash-chained audit record — before anything reaches the graph.

```
writer ──▶  proposal.yaml  ──▶  [ kgg gate ]  ──▶  your graph
  (LLM /                           │
   script /        controlled vocabulary · no implicit creation
   human)          supersession · provenance · staged approval
                   graded verdicts · hash-chained audit
```

It's what Git and CI do for code — proposed changes, validation, review, versioning, provenance, audit — applied to graph memory.

## What corrupts agent-written graphs

| Failure mode | Without a gate | What `kgg` does | Proof |
|---|---|---|---|
| Taxonomy drift | `new_hire`, `new_hire_v2`, `signal.newhire` all appear | **Controlled vocabulary** — undefined terms are blocked | `taxonomy-drift` |
| Phantom nodes | a stray edge creates a fake account/persona | **No implicit creation** — closed kinds must be declared | `phantom-nodes` |
| Silent overwrite | a newer belief clobbers prior context | **Supersession** — the prior belief is versioned, not dropped | `silent-overwrite` |
| Sources disagree | one claim quietly wins, or both are averaged | **Contradiction ledger** — both held, neither chosen | `contradiction-ledger` |
| Unclear evidence | nobody knows where a fact came from | **Provenance** — owner + run id + timestamp on every node | `provenance` |
| Ambiguous category | the model force-fits a novel insight | **Held for approval** — uncertainty is first-class | `held-for-approval` |
| The vocabulary is wrong | writers coin terms, or force-fit forever | **Gap promotion** — unmapped is a proposal with a join key | `gap-promotion` |
| Bad batch | one corrupt item poisons the whole run | **Graded verdicts** — reject one item, apply the rest | `graded-verdicts` |
| Audit gaps | you can't prove what changed | **Hash-chained audit** — tamper-evident, verifiable | `audit-chain` |
| Stale read | a superseded belief is read as current | **Three-state integrity** on every read | `read-integrity` |

Every row in that last column is a **runnable case**, not a description. `kgg
conformance` executes all of them against a real backend and fails if any
documented guarantee stops holding:

```bash
kgg conformance            # 19 cases across 15 guarantees
kgg conformance --verbose  # with each case's claim spelled out
```

A gate is a claim about what it *refuses*, and prose cannot discharge one. The
cases live in [`examples/conformance/`](examples/conformance/) — each declares the
ingest files, the operations, and the exact per-item verdict expected at every
step. Three real bugs were found by writing them.

## 60-second quickstart (no database to install)

```bash
git clone https://github.com/kkrlstrm/knowledge-graph-governance
cd knowledge-graph-governance
pip install -e .

# dry-run the example against a throwaway SQLite graph — prints a graded plan
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml --backend sqlite:./demo.db

# apply it — writes ALLOW items, holds the one unmapped observation for review
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml --backend sqlite:./demo.db --apply

# validate the governance contract itself, anytime
kgg validate-schema --schema examples/gtm/schema.yaml

# the audit log is tamper-evident
kgg verify-audit --audit ./audit.jsonl

# clear the held item, then re-apply
kgg approve --file examples/gtm/sample.yaml --schema examples/gtm/schema.yaml \
    --items insight:buyers_asking_for_ai_governance_clause --approver you
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml --backend sqlite:./demo.db --apply

# ask the graph why it believes something, and whether that can be verified
kgg explain insight:acme_new_cro \
    --schema examples/gtm/schema.yaml --backend sqlite:./demo.db

# what do two sources disagree about? (both shown, neither chosen)
kgg contradictions --backend sqlite:./demo.db

# what does the vocabulary have no word for yet?
kgg gaps --backend sqlite:./demo.db
```

The dry-run prints exactly what would change, and why:

```
  ＋ CREATE (7):
      ✓ insight:acme_new_cro_first_60_days
      ✓ entity:Acme Robotics
      ...
  ⏸ HELD (1):
      ⏸ insight:buyers_asking_for_ai_governance_clause  — no controlled-vocabulary home
  create=7  supersede=0  create_unmapped=0  unchanged=0  held=1  blocked=0
```

## When you need this

Reach for `kgg` when:

- agents extract facts from calls, docs, emails, tickets, or research
- those facts become **durable memory**, not a scratchpad
- multiple writers can update the same graph
- taxonomy quality, provenance, or belief history matter
- changed beliefs should be **versioned, not overwritten**
- humans need to approve ambiguous or novel observations before they land

## What this is not

`kgg` is **not** a graph database, a GraphRAG framework, or an ontology editor. It's the **write-path control layer that sits *before* your graph database.** Point it at whatever store you already use.

## How it works

The engine, checks, and kernel import **no database** — a `Backend` adapter is the only thing that touches a store. Your node model and controlled vocabulary are declared in **YAML**, so the same gate governs any domain.

A **schema** declares your node kinds, their identity field, which fields draw from a controlled vocabulary, which fields reference other nodes, and which count as "content" for supersession:

```yaml
# schema.yaml
name: gtm
vocabularies:
  gtm_taxonomy: vocabulary.yaml
node_kinds:
  insight:
    key: tag
    required: [statement]
    vocab_fields: { category: gtm_taxonomy }   # null -> held as unmapped
    content_fields: [statement, evidence_strength]
    supersedable: true
    refs: { entities: entity, personas: persona }
  entity:  { key: name, implicit_create: false }   # must be declared, never implicit
  persona: { key: name, implicit_create: false }
```

**Identity** is always composite — a node is `(kind, key)`, never a bare key, so two kinds that share a key can't collide. A schema sets `identity: global` (default — `(kind, key)` is unique; the ingest's `scope` is only run metadata) or `identity: scoped` (`(scope, kind, key)` — the same key can exist per tenant/seller/team, partitioned by scope). Run `kgg validate-schema` to check the contract itself before ingesting.

**Domain rules are yours.** The kernel enforces what is true of any governed graph. What is true of *your* graph — "a fact needs an expiry", "a `verified` tier needs an authoritative source" — is a check pack you declare in the schema:

```yaml
checks:
  - domain_checks.py:REGISTRY        # a sibling file, or an installed module
```

A pack is a list of `(name, fn)` where `fn(ctx) -> list[Finding]`; see [`examples/gtm/domain_checks.py`](examples/gtm/domain_checks.py). It is declared in the schema and **not** on the command line, because the fingerprint hashes the schema bytes — so which rules are in force is inside the fingerprint, and weakening the gate invalidates approvals granted under the strong one. A pack cannot take over a kernel check's name.

An **ingest file** is what a writer produces:

```yaml
source: Q3 discovery calls
date: 2026-07-03
declare:
  entity:  [{ name: Acme Robotics }]
  persona: [{ name: Chief Revenue Officer }]
nodes:
  insight:
    - tag: acme_new_cro
      category: signal.new_hire        # must be in gtm_taxonomy, or it's blocked
      statement: "New CRO at Acme — first 60 days."
      entities: [Acme Robotics]        # must resolve to a declared/existing entity
      personas: [Chief Revenue Officer]
      evidence_strength: 5
      # supersede: true   # optional — version a changed belief instead of holding it
      # protected: true   # optional — owner-only; block foreign overwrites
```

## The read path — `kgg explain`

A write gate is half an answer. It proves a fact was *admitted* under policy; it cannot prove the fact is still what you should act on. Everything that goes wrong after a clean write is a read-time problem, so the gate ships a reader:

```bash
kgg explain insight:acme_budget --schema examples/gtm/schema.yaml --backend sqlite:./demo.db
```

```
  integrity: ✓ VERIFIED — run 'a-2026…-7f0e' is entry 1 in an intact chain

  CURRENT (revision 1, status active)
      claim: Acme has budget approved for the platform this fiscal year.
      run=a-2026…-7f0e  source=call with the CFO  at=2026-09-07T09:34:00Z
      ⚠ another active claim in this slot disagrees; both are held, neither wins

  COMPETING CLAIMS (1 active)  — both held, no winner picked
      ● vs insight:acme_budget_denied  [negation_mismatch] same subject (overlap 0.86)
          this : Acme has budget approved for the platform this fiscal year.
          other: Acme has not approved budget for the platform this fiscal year.
```

Integrity is three states, never a boolean:

| | |
|---|---|
| `verified` | the node exists, its run resolves to an audit entry, and the chain is intact through it |
| `unverified` | it exists but its provenance **cannot be checked** — no run id, or a run id with no entry |
| `invalidated` | the chain is broken at or before its entry, or the node isn't active while being read as current |

Collapsing the middle state into either neighbour is the failure: reported as fine, an unexamined node gets acted on; reported as broken, a legitimately un-audited import gets thrown away. `kgg explain` exits `0` / `4` / `5` respectively, so a script can't grep success and get it wrong.

This is a read over existing rows — nodes, revisions, edges, the audit log, the contradiction ledger. It builds no second store. And it shows evidence rather than summarising it, because **a verbatim gate proves provenance, not interpretation**: text can be copied perfectly from a real source and still not support the claim stapled to it. No check catches that; a human reading the claim next to its own evidence does.

## Contradictions — simultaneous disagreement, kept as evidence

Supersession answers "the belief changed over time." It cannot answer "two sources disagree *right now*", and collapsing the second into the first asserts that the newer claim won when all that is established is that they conflict.

```bash
kgg contradictions --backend sqlite:./demo.db --state active
```

A schema declares what a node is a claim *about* (`claim_slot`) and which field carries the claim (`claim_field`). Two active nodes sharing a slot are candidates; a **deterministic** detector — negation mismatch or an antonym pair, over a lexical-overlap floor, no model call — decides. Sharing a slot is necessary and nowhere near sufficient, or every second observation about one account would register as a disagreement. A writer can also state one outright with `contradicts: [kind:key]`.

Four properties make the ledger safe to keep:

- **Advisory, not a third truth value.** Both claims keep their status, revision, and provenance. Current-truth reads are unchanged; what changes is that the reader can find out.
- **It never picks a winner.** No source ranking, no confidence comparison, no recency tie-break.
- **Deterministic detectors only.** An inference-based detector would give a different answer every time you asked.
- **Resolution retains the record.** When one side is superseded or removed the observation resolves — it isn't deleted. "We believed two conflicting things and here is how it ended" survives the thing that ended it.

This is where `kgg` parts company with agent-memory stores: Graphiti invalidates the older fact, mem0 overwrites. Holding both is a governance position, not a missing feature.

## Gaps — how a closed vocabulary grows without drifting

A closed vocabulary has one honest failure mode: the writer finds something real it has no word for. There are only three usual answers, and all three are bad — let the writer coin a term (drift), force-fit the nearest term (worse than drift: the observation is now filed under a claim it doesn't support, with nothing marking it), or hold it as unmapped and never look again.

`kgg` takes the fourth: **unmapped is a proposal, and proposals need a join key.**

```yaml
nodes:
  insight:
    - tag: acme_size
      # no `category` — the vocabulary has no word for this
      statement: "Headcount is the wrong size proxy here; route density predicts fit."
      unmapped_gap: effective-size-proxy        # REQUIRED. the join key.
      unmapped_reason: "Nearest is procurement.acv_band_by_segment, which bands by deal size."
```

```bash
kgg gaps                        # ranked by INDEPENDENT scopes
kgg gaps known                  # the list you inject into the next extraction prompt
kgg gaps promote effective-size-proxy --term procurement.effective_size_proxy --category "Buying Mechanics"
```

Four design decisions, each from a measured failure rather than an intuition:

- **The slug is short and canonical; the prose is separate.** An earlier version clustered on the free-text reason and asked writers to make it richer. The reasons got ~65% longer and cross-writer gap merges went from one to *zero* — a longer description inflates the overlap denominator between two accounts of one gap, so convergence gets strictly harder as the artifact a human reads gets better. Prose is not a join key.
- **`kgg gaps known` exists because convergence on an open set is telepathy.** Asking writers to independently choose the same name does not work: 280 untagged observations produced 223 distinct slugs, and no gap was ever named by two independent sources. The canonical vocabulary doesn't fail this way for a structural reason — the writer is *handed the list*. This hands them the gap list too.
- **A slug that names its instance is refused.** `upciti-tourist-town-thing` is syntactically perfect and reachable by nobody: unique by construction, which is exactly the property that killed every gap when they were keyed per-observation. The check compares the slug against the ingest's scope.
- **Promotion counts independent scopes, not observations.** Nine observations from one scope is one source with a loud voice. Two scopes that never spoke to each other landing on the same slug is evidence.

Promoting edits the vocabulary file, which changes the schema fingerprint, which invalidates approvals granted under the old vocabulary. That is the intended cost: changing what the graph is allowed to say is a policy change and gets re-reviewed.

**The vocabulary is a versioned artifact,** not a config file — it carries a `version`, declared `synonyms` folded on read, and a `deprecated` / `replaced_by` path that holds for approval and rewrites the term rather than blocking the writer. Synonyms are only ever *declared*: two terms that sound alike are routinely different concepts, so folding is a curation decision that belongs in the file as a reviewable diff.

## Backends

| DSN | Notes |
|---|---|
| `sqlite:<path>` | Zero-infra, standard library. The quickstart. Nodes, revision history, edges, the contradiction ledger and the gap registry in one file. |
| `neo4j:<uri>` | `pip install -e ".[neo4j]"`. Auth from `NEO4J_USER` / `NEO4J_PASSWORD`. |

Add a store by implementing five write methods (`read_existing` / `create` / `supersede` / `link` / `close`) in `kgg/backends/`. The read surface `kgg explain`, `kgg contradictions` and `kgg gaps` use is **optional** — a store can be wired up for governed writes alone. It raises rather than returning empty, because a reader that answered "no revisions" when it simply could not look would present an unexamined node as a clean one.

## The GTM example pack

`examples/gtm/` is the strongest demo domain: turning messy go-to-market observations — buying signals, personas, objections, outreach tactics — into durable graph memory *without* letting an agent invent taxonomy, overwrite prior beliefs, or create phantom accounts. It ships a real, reusable GTM taxonomy (7 categories, org-agnostic terms) you can fork, but the engine itself is domain-neutral — write your own `schema.yaml` + `vocabulary.yaml` for any domain where agents write to durable memory.

## Guarantees & limits

- **Atomic apply.** A run's writes happen inside one backend transaction. A crash or error mid-apply rolls back — no partial graph — and the audit entry is written *only after* the transaction commits, so the log never claims a write that didn't land.
- **Fail-closed creation.** `create` is a plain `INSERT` / `CREATE` against a uniqueness constraint on the node identity. If the planner is wrong or another writer raced in, the storage layer refuses to overwrite and the transaction aborts — it never silently clobbers.
- **Approvals are bound to policy.** An approval is matched to the exact file **content hash** *and* the **schema fingerprint**. Edit the ingest file or change the governance contract and stale approvals stop applying.
- **Tamper-evident, not tamper-proof.** The audit chain detects any edit, reorder, insertion, or deletion within the log. It does not stop someone who can rewrite the whole file from regenerating a fresh valid chain — for that, anchor the head hash externally (a Git commit, an object-store version, a signed timestamp, a transparency log). `kgg verify-audit` prints the head so you can pin it.
- **The fingerprint pins declarations, not their contents.** Editing a domain check's *body* does not invalidate approvals; changing which packs are declared does. Version your check packs like any other code that can refuse a write.
- **Valid time is recorded, not queryable.** Nodes and revisions carry `valid_from` / `valid_until`, and `kgg explain` shows the version chain, but there is no as-of read yet. Transaction time is what the audit chain establishes.
- **Contradiction detection is lexical.** It finds negation mismatches and antonym pairs over a lexical-overlap floor. Two sources can contradict each other without sharing a content word, and nothing deterministic will catch that — use `contradicts:` when a writer has read both.
- **Not yet:** cross-process concurrency control (two planners against one stale snapshot can conflict — coming via optimistic revisions), and Neo4j is exercised by a manual live smoke, not CI (SQLite is the CI-tested path, and the conformance suite runs against it).

## Status

v0.2 — the write-gate kernel, a generic schema model with global/scoped identity, schema-declared domain check packs, a curated vocabulary with aliases and deprecation, the gap-promotion layer, the contradiction ledger, the `kgg explain` read path, transactional + fail-closed SQLite & Neo4j backends, and a conformance suite where each documented guarantee is a runnable refusal (`kgg conformance`). Roadmap: optimistic-concurrency revisions, external audit anchoring, as-of reads over the recorded valid time, near-duplicate key detection at the reference boundary, a config-DSL for lower-friction schemas, and a Postgres backend.

## License

Apache-2.0. Contributions welcome.

---

<!-- portfolio-footer -->
## Where this fits

Part of a portfolio of **governed, AI-native GTM systems** — reference implementations and reusable patterns extracted from a private production stack. In that system this is the deterministic write gate that keeps agent-proposed graph memory honest.

**Full portfolio map → [github.com/kkrlstrm](https://github.com/kkrlstrm)**

Works with:
- [agent-tenancy](https://github.com/kkrlstrm/agent-tenancy) — isolates which tenant a write belongs to
