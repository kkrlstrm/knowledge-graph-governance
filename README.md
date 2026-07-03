# kgg — the write gate for agent-managed knowledge graphs

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

| Failure mode | Without a gate | What `kgg` does |
|---|---|---|
| Taxonomy drift | `new_hire`, `new_hire_v2`, `signal.newhire` all appear | **Controlled vocabulary** — undefined terms are blocked |
| Phantom nodes | a stray edge creates a fake account/persona | **No implicit creation** — closed kinds must be declared |
| Silent overwrite | a newer belief clobbers prior context | **Supersession** — the prior belief is versioned, not dropped |
| Unclear evidence | nobody knows where a fact came from | **Provenance** — owner + run id + timestamp on every node |
| Ambiguous category | the model force-fits a novel insight | **Held for approval** — uncertainty is first-class |
| Bad batch | one corrupt item poisons the whole run | **Graded verdicts** — reject one item, apply the rest |
| Audit gaps | you can't prove what changed | **Hash-chained audit** — tamper-evident, verifiable |

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

# the audit log is tamper-evident
kgg verify-audit --audit ./audit.jsonl

# clear the held item, then re-apply
kgg approve --file examples/gtm/sample.yaml \
    --items buyers_asking_for_ai_governance_clause --approver you
kgg ingest examples/gtm/sample.yaml \
    --schema examples/gtm/schema.yaml --backend sqlite:./demo.db --apply
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

## Backends

| DSN | Notes |
|---|---|
| `sqlite:<path>` | Zero-infra, standard library. The quickstart. Nodes + revision history + edges in one file. |
| `neo4j:<uri>` | `pip install -e ".[neo4j]"`. Auth from `NEO4J_USER` / `NEO4J_PASSWORD`. |

Add a store by implementing five methods (`read_existing` / `create` / `supersede` / `link` / `close`) in `kgg/backends/`.

## The GTM example pack

`examples/gtm/` is the strongest demo domain: turning messy go-to-market observations — buying signals, personas, objections, outreach tactics — into durable graph memory *without* letting an agent invent taxonomy, overwrite prior beliefs, or create phantom accounts. It ships a real, reusable GTM taxonomy (7 categories, org-agnostic terms) you can fork, but the engine itself is domain-neutral — write your own `schema.yaml` + `vocabulary.yaml` for any domain where agents write to durable memory.

## Status

v0.1 — the write-gate kernel, a generic schema model, SQLite + Neo4j backends, the GTM example pack, and a full test suite (`python -m pytest`). Roadmap: a config-DSL for lower-friction schemas, a read/query companion, a Postgres backend, and batch proposed-change *branching*.

## License

Apache-2.0. Contributions welcome.
