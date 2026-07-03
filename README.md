# knowledge-graph-governance

**A governed write-path for knowledge graphs.** Your writer — an LLM, a script, or a human — proposes changes as a declarative file. `kgg` validates every write against a controlled vocabulary, refuses implicit node creation, versions changed beliefs, stamps provenance, holds anything ambiguous for human approval, and records it all in a tamper-evident audit log — *before* anything reaches the graph.

Everyone tells you to give your agent a knowledge graph. Nobody ships the write path. The two common options are (a) let the model emit raw Cypher/SQL — and watch it drift your schema, invent node types, and silently overwrite facts — or (b) hand-code a rigid ORM no agent can drive. `kgg` is the governed middle: the writer proposes intent as a file; a deterministic gate enforces the contract.

```
writer ──▶  proposal.yaml  ──▶  [ kgg gate ]  ──▶  your graph
                                   │
                    controlled vocabulary · no implicit creation
                    supersession · provenance · staged approval
                    hash-chained audit
```

## What the gate enforces

Seven mechanisms, one pipeline:

1. **Graded verdicts** — every finding is `ALLOW` / `REQUIRE_APPROVAL` / `BLOCK` / `HALT` under strict precedence. One bad item is rejected without killing the file; a corrupt file halts before any read.
2. **Named checks** — validation is a registry of named checks, not a flat error blob. A rejection tells you *which* rule fired; adding a domain rule is one function.
3. **Controlled vocabulary** — a write to a vocabulary-constrained field must use a defined term. No taxonomy drift (`signal.new_hire` → `new_hire_v2` → `signal_newhire`). New terms are a reviewable diff.
4. **No implicit creation** — a reference to a node that doesn't exist is blocked; closed node kinds must be declared explicitly, never conjured by a stray edge.
5. **Supersession, not silent overwrite** — a re-appearing key with identical content is a no-op; with *changed* content it versions the prior belief into a revision (addressable history) instead of dropping it or clobbering it.
6. **Provenance + protection** — every node carries `owner` / `is_protected` / `ingest_run_id`, and a protected node owned by someone else can't be overwritten.
7. **Staged proposal + tamper-evident audit** — a dry-run writes an approvable artifact; applied runs append a hash-chained audit entry, so any post-hoc edit to the log is detectable.

## 60-second quickstart (no database to install)

```bash
pip install knowledge-graph-governance

# dry-run the bundled GTM example against a throwaway SQLite graph
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml \
    --backend sqlite:./demo.db

# apply it — writes ALLOW items, holds the one unmapped observation for review
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml \
    --backend sqlite:./demo.db --apply

# the audit log is tamper-evident
kgg verify-audit --audit ./audit.jsonl

# clear the held item, then re-apply
kgg approve --file examples/gtm/sample.yaml \
    --items buyers_asking_for_ai_governance_clause --approver you
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml \
    --backend sqlite:./demo.db --apply
```

The dry-run prints a graded plan:

```
  ＋ CREATE (7):
      ✓ insight:acme_new_cro_first_60_days
      ✓ entity:Acme Robotics
      ...
  ⏸ HELD (1):
      ⏸ insight:buyers_asking_for_ai_governance_clause  — no controlled-vocabulary home
  create=7  supersede=0  create_unmapped=0  unchanged=0  held=1  blocked=0
```

## How it works

The engine, checks, and kernel import **no database** — a `Backend` adapter is the only thing that touches a store. The node model and the controlled vocabulary are declared in **YAML**, so the same governance engine drives any domain.

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

An **ingest file** is what the writer produces:

```yaml
source: Q3 discovery calls
date: 2026-07-03
declare:
  entity:  [{ name: Acme Robotics }]
  persona: [{ name: Chief Revenue Officer }]
nodes:
  insight:
    - tag: acme_new_cro
      category: signal.new_hire        # must be in gtm_taxonomy
      statement: "New CRO at Acme — first 60 days."
      entities: [Acme Robotics]        # must resolve to a declared/existing entity
      personas: [Chief Revenue Officer]
      evidence_strength: 5
      # supersede: true   # optional — version a changed belief instead of holding it
      # protected: true   # optional — owner-only; block foreign overwrites
```

## The bundled GTM taxonomy pack

`examples/gtm/` ships a **universal GTM taxonomy** — a starter ontology for a go-to-market knowledge graph that applies to nearly any B2B revenue org: buying signals, stakeholder dynamics, buying mechanics, objections, outreach tactics, timing/seasonality, and segmentation. It's a working schema *and* a genuinely useful starting vocabulary. Fork it, or write your own — every term you add is a reviewable diff, and an insight tagged with an undefined term is blocked.

## Backends

| DSN | Notes |
|---|---|
| `sqlite:<path>` | Zero-infra, standard library. The quickstart. Nodes + revision history + edges in one file. |
| `neo4j:<uri>` | Requires `pip install knowledge-graph-governance[neo4j]`. Auth from `NEO4J_USER` / `NEO4J_PASSWORD`. |

Add a store by implementing five methods (`read_existing` / `create` / `supersede` / `link` / `close`) in `kgg/backends/`.

## Status

v0.1 — the governance kernel, generic schema model, SQLite + Neo4j backends, the GTM pack, and a full test suite (`python -m pytest`). Roadmap: a YAML config-DSL to declare node models with less friction, a read/query companion, Postgres backend, and batch proposed-change *branching*.

## License

Apache-2.0. Contributions welcome.
