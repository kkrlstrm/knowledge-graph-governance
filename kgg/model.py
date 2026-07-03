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

from dataclasses import dataclass, field
from pathlib import Path

import yaml


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

    def kind(self, name: str) -> NodeKind | None:
        return self.kinds.get(name)

    @classmethod
    def load(cls, path: str | Path) -> "Schema":
        p = Path(path)
        data = yaml.safe_load(p.read_text()) or {}
        base = p.parent

        vocabs = {}
        for vname, vpath in (data.get("vocabularies") or {}).items():
            vocabs[vname] = Vocabulary.load(vname, base / vpath)

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
                   scope_label=data.get("scope_label", "scope"))


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
