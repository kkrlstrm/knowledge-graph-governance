"""Generic graph model — declared in YAML, not hardcoded.

A Schema describes the node kinds you allow, their identity field, which fields
must draw from a controlled Vocabulary, which fields are references to other
nodes (referential integrity + no-implicit-create), and which fields count as
"content" for supersession. The engine and checks are written entirely against
this model, so the same governance kernel drives any domain — the bundled GTM
pack is just one Schema.

schema.yaml
-----------
    name: gtm
    scope_label: account          # display label for the ingest's scope
    vocabularies:
      gtm_taxonomy: vocabulary.yaml
    node_kinds:
      insight:
        key: tag                   # identity property name (single-field key)
        required: [statement]
        vocab_fields: { category: gtm_taxonomy }   # null allowed -> unmapped/held
        content_fields: [statement, evidence_strength]
        supersedable: true
        refs: { entities: entity, personas: persona }   # each value must resolve
      entity:  { key: name, implicit_create: false }
      persona: { key: name, implicit_create: false }
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Composite item identity. A node is identified by (kind, key) — never key
# alone, or findings/approvals/actions for two kinds that share a key would
# bleed into each other. kgg validate-schema forbids ':' in kind names, so this
# encoding is an unambiguous bijection.
GLOBAL = "global"
SCOPED = "scoped"


def iid(kind: str, key: str) -> str:
    return f"{kind}:{key}"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

@dataclass
class Vocabulary:
    name: str
    terms: dict          # term -> definition
    category_of: dict    # term -> category (or "" if flat)

    def __contains__(self, term) -> bool:
        return term in self.terms

    @classmethod
    def load(cls, name: str, path: str | Path) -> "Vocabulary":
        data = yaml.safe_load(Path(path).read_text()) or {}
        terms, cat_of = {}, {}
        if "categories" in data:               # nested: category -> {term: def}
            for cat, entries in (data["categories"] or {}).items():
                for term, defn in (entries or {}).items():
                    terms[term] = defn
                    cat_of[term] = cat
        else:                                   # flat: {term: def} under `terms:` or top-level
            flat = data.get("terms", data)
            for term, defn in (flat or {}).items():
                terms[term] = defn
                cat_of[term] = ""
        return cls(name=name, terms=terms, category_of=cat_of)


# ---------------------------------------------------------------------------
# Node kinds + schema
# ---------------------------------------------------------------------------

@dataclass
class NodeKind:
    name: str
    key: str = "key"
    required: list = field(default_factory=list)
    vocab_fields: dict = field(default_factory=dict)     # field -> vocab name
    content_fields: list = field(default_factory=list)
    refs: dict = field(default_factory=dict)             # field -> target kind
    supersedable: bool = True
    implicit_create: bool = True    # default open; closed kinds (entity/persona)
                                    # opt in to `implicit_create: false` and must
                                    # be created via `declare:`, never a stray ref


@dataclass
class Schema:
    name: str
    kinds: dict                        # kind name -> NodeKind
    vocabularies: dict                 # vocab name -> Vocabulary
    scope_label: str = "scope"
    identity: str = GLOBAL             # GLOBAL: (kind,key) unique, scope is metadata.
                                       # SCOPED: (scope,kind,key) — scope partitions the graph.
    _paths: tuple = ()                 # schema + vocab file paths, for fingerprinting

    def kind(self, name: str) -> NodeKind | None:
        return self.kinds.get(name)

    @property
    def scoped(self) -> bool:
        return self.identity == SCOPED

    @classmethod
    def load(cls, path: str | Path) -> "Schema":
        p = Path(path)
        data = yaml.safe_load(p.read_text()) or {}
        base = p.parent

        vocabs, vpaths = {}, []
        for vname, vpath in (data.get("vocabularies") or {}).items():
            vocabs[vname] = Vocabulary.load(vname, base / vpath)
            vpaths.append(str(base / vpath))

        kinds = {}
        for kname, spec in (data.get("node_kinds") or {}).items():
            spec = spec or {}
            kinds[kname] = NodeKind(
                name=kname,
                key=spec.get("key", "key"),
                required=list(spec.get("required", [])),
                vocab_fields=dict(spec.get("vocab_fields", {})),
                content_fields=list(spec.get("content_fields", [])),
                refs=dict(spec.get("refs", {})),
                supersedable=bool(spec.get("supersedable", True)),
                implicit_create=bool(spec.get("implicit_create", True)),
            )
        return cls(name=data.get("name", p.stem), kinds=kinds, vocabularies=vocabs,
                   scope_label=data.get("scope_label", "scope"),
                   identity=str(data.get("identity", GLOBAL)),
                   _paths=tuple([str(p)] + sorted(vpaths)))

    def fingerprint(self) -> str:
        """Stable hash of the schema + its vocab files — binds approvals to policy."""
        h = hashlib.sha256()
        for fp in self._paths:
            try:
                h.update(Path(fp).read_bytes())
            except OSError:
                h.update(fp.encode())
        return h.hexdigest()


# ---------------------------------------------------------------------------
# Schema self-validation — validate the governance contract before ingests
# ---------------------------------------------------------------------------

def validate_schema(schema: Schema) -> list[str]:
    """Return a list of problems with the schema itself (empty = valid)."""
    errs = []
    if schema.identity not in (GLOBAL, SCOPED):
        errs.append(f"identity must be '{GLOBAL}' or '{SCOPED}', got {schema.identity!r}")
    if not schema.kinds:
        errs.append("schema defines no node_kinds")
    for kname, nk in schema.kinds.items():
        if ":" in kname:
            errs.append(f"kind name '{kname}' must not contain ':' (breaks composite identity)")
        if not nk.key:
            errs.append(f"kind '{kname}' has no key field")
        for f, vname in nk.vocab_fields.items():
            if vname not in schema.vocabularies:
                errs.append(f"kind '{kname}' vocab_field '{f}' -> unknown vocabulary '{vname}'")
        for f, target in nk.refs.items():
            if target not in schema.kinds:
                errs.append(f"kind '{kname}' ref '{f}' -> unknown target kind '{target}'")
        reserved = {"supersede", "protected"} & (set(nk.required) | set(nk.content_fields))
        if reserved:
            errs.append(f"kind '{kname}' uses reserved control field name(s): {sorted(reserved)}")
    for vname, vocab in schema.vocabularies.items():
        if not vocab.terms:
            errs.append(f"vocabulary '{vname}' has no terms")
    return errs


# ---------------------------------------------------------------------------
# Existing-node snapshot (read from a backend)
# ---------------------------------------------------------------------------

@dataclass
class ExistingNode:
    kind: str
    key: str
    content: dict = field(default_factory=dict)   # content_field -> stored value
    is_protected: bool = False
    owner: str = ""
    status: str = "active"
    revision: int = 1
    scope: str = ""
