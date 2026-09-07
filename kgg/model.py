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
    checks:                       # domain check registries (see kgg/plugins.py)
      - my_package.checks:REGISTRY
    vocabularies:
      gtm_taxonomy: vocabulary.yaml
    node_kinds:
      insight:
        key: tag                   # identity property name (single-field key)
        required: [statement]
        vocab_fields: { category: gtm_taxonomy }   # null allowed -> unmapped/held
        content_fields: [statement, evidence_strength]
        claim_field: statement     # the text a contradiction detector reads
        claim_slot: [entities, personas]  # what this node is a claim ABOUT
        supersedable: true
        refs: { entities: entity, personas: persona }   # each value must resolve
      entity:  { key: name, implicit_create: false }
      persona: { key: name, implicit_create: false }

Everything in this file is inside the schema *fingerprint*, because the
fingerprint hashes the schema and vocabulary file bytes. So declaring a domain
check, adding a synonym, or promoting a gap into the vocabulary all invalidate
stale approvals — a changed contract requires re-review, by construction.
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

ACTIVE = "active"
DEPRECATED = "deprecated"


def iid(kind: str, key: str) -> str:
    return f"{kind}:{key}"


def split_iid(_id: str) -> tuple[str, str]:
    kind, _, key = _id.partition(":")
    return kind, key


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# How a submitted term resolved against the controlled vocabulary.
EXACT = "exact"
SYNONYM = "synonym"
DEPRECATED_HIT = "deprecated"
UNKNOWN = "unknown"


@dataclass
class Term:
    """One controlled term. `synonyms` are DECLARED aliases, never inferred.

    Folding an alias onto a canonical term is a curation decision that belongs in
    the vocabulary file where it is a reviewable diff. Two terms that merely sound
    alike are not synonyms — a tool-calling `tool-design` and a developer-tooling
    `dev-tooling` are different concepts, and a gate that guessed would silently
    merge them.
    """
    name: str
    definition: str = ""
    synonyms: tuple = ()
    status: str = ACTIVE
    replaced_by: str = ""
    category: str = ""
    since: str = ""              # vocabulary version this term entered at
    promoted_from_gap: str = ""  # the unmapped_gap slug that produced it, if any


@dataclass
class Vocabulary:
    """A controlled vocabulary, versioned as its own artifact.

    Two accepted file shapes, both backward compatible:

        # flat
        terms:
          signal.new_hire: "A new leader in the buyer role."

        # curated
        version: 3
        updated: 2026-09-07
        categories:
          Buying Signals:
            signal.new_hire:
              definition: "A new leader in the buyer role."
              synonyms: [new_leader]
              status: active
    """
    name: str
    terms: dict          # term -> definition          (canonical terms only)
    category_of: dict    # term -> category (or "" if flat)
    records: dict = field(default_factory=dict)      # term -> Term
    synonyms: dict = field(default_factory=dict)     # alias -> canonical
    version: str = ""
    updated: str = ""

    def __contains__(self, term) -> bool:
        return term in self.terms

    def record(self, term: str) -> Term | None:
        return self.records.get(term)

    def resolve(self, value: str) -> tuple[str | None, str]:
        """`(canonical_term, how)` — how in exact | synonym | deprecated | unknown.

        A deprecated term still resolves (so the gate can name its replacement
        rather than just refusing), but the caller decides the verdict.
        """
        if value in self.terms:
            rec = self.records.get(value)
            if rec and rec.status == DEPRECATED:
                return (rec.replaced_by or value), DEPRECATED_HIT
            return value, EXACT
        if value in self.synonyms:
            return self.synonyms[value], SYNONYM
        return None, UNKNOWN

    @classmethod
    def load(cls, name: str, path: str | Path) -> "Vocabulary":
        data = yaml.safe_load(Path(path).read_text()) or {}
        terms, cat_of, records, syns = {}, {}, {}, {}

        def add(term, spec, category):
            if isinstance(spec, dict):
                rec = Term(
                    name=term,
                    definition=str(spec.get("definition", "") or ""),
                    synonyms=tuple(spec.get("synonyms", []) or []),
                    status=str(spec.get("status", ACTIVE) or ACTIVE),
                    replaced_by=str(spec.get("replaced_by", "") or ""),
                    category=category,
                    since=str(spec.get("since", "") or ""),
                    promoted_from_gap=str(spec.get("promoted_from_gap", "") or ""),
                )
            else:
                rec = Term(name=term, definition=str(spec or ""), category=category)
            terms[term] = rec.definition
            cat_of[term] = category
            records[term] = rec
            for alias in rec.synonyms:
                syns[alias] = term

        if "categories" in data:               # nested: category -> {term: spec}
            for cat, entries in (data["categories"] or {}).items():
                for term, spec in (entries or {}).items():
                    add(term, spec, cat)
        else:                                   # flat: {term: spec} under `terms:` or top-level
            flat = data.get("terms", data)
            if not isinstance(flat, dict):
                flat = {}
            for term, spec in (flat or {}).items():
                if term in ("version", "updated"):
                    continue
                add(term, spec, "")

        return cls(name=name, terms=terms, category_of=cat_of, records=records,
                   synonyms=syns, version=str(data.get("version", "") or ""),
                   updated=str(data.get("updated", "") or ""))


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
    claim_field: str = ""           # the text a contradiction detector reads
    claim_slot: list = field(default_factory=list)   # fields identifying WHAT
                                    # this node is a claim about; two active
                                    # nodes sharing a slot are competing claims
    near_duplicate_check: bool = False   # hold a new key that is nearly an
                                    # existing one. OFF by default: whether two
                                    # similar keys name one thing is a domain
                                    # question, and the answer differs by kind
                                    # (company names merge, place names do not).
    near_duplicate_threshold: float = 0.75


@dataclass
class Schema:
    name: str
    kinds: dict                        # kind name -> NodeKind
    vocabularies: dict                 # vocab name -> Vocabulary
    scope_label: str = "scope"
    identity: str = GLOBAL             # GLOBAL: (kind,key) unique, scope is metadata.
                                       # SCOPED: (scope,kind,key) — scope partitions the graph.
    check_specs: tuple = ()            # 'module:ATTR' domain check registries
    require_gap_slug: bool = True      # an unmapped item must name the gap it found
    base_dir: str = ""                 # the schema's own directory; path-based
                                       # check specs resolve against it, exactly
                                       # as `vocabularies:` paths already do
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
            slot = spec.get("claim_slot", []) or []
            kinds[kname] = NodeKind(
                name=kname,
                key=spec.get("key", "key"),
                required=list(spec.get("required", [])),
                vocab_fields=dict(spec.get("vocab_fields", {})),
                content_fields=list(spec.get("content_fields", [])),
                refs=dict(spec.get("refs", {})),
                supersedable=bool(spec.get("supersedable", True)),
                implicit_create=bool(spec.get("implicit_create", True)),
                claim_field=str(spec.get("claim_field", "") or ""),
                claim_slot=list(slot if isinstance(slot, list) else [slot]),
                near_duplicate_check=bool(spec.get("near_duplicate_check", False)),
                near_duplicate_threshold=float(
                    spec.get("near_duplicate_threshold", 0.75)),
            )
        return cls(name=data.get("name", p.stem), kinds=kinds, vocabularies=vocabs,
                   scope_label=data.get("scope_label", "scope"),
                   identity=str(data.get("identity", GLOBAL)),
                   check_specs=tuple(data.get("checks", []) or []),
                   require_gap_slug=bool(data.get("require_gap_slug", True)),
                   base_dir=str(base),
                   _paths=tuple([str(p)] + sorted(vpaths)))

    def fingerprint(self) -> str:
        """Stable hash of the schema + its vocab files — binds approvals to policy.

        Hashing file *bytes* means the declared check registries, every synonym,
        and every promoted term are inside the fingerprint automatically. There is
        no way to change what the gate enforces without invalidating approvals
        granted under the old rules.
        """
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

def validate_schema(schema: Schema, *, load_checks: bool = True) -> list[str]:
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
        reserved = ({"supersede", "protected", "contradicts", "unmapped_gap"}
                    & (set(nk.required) | set(nk.content_fields)))
        if reserved:
            errs.append(f"kind '{kname}' uses reserved control field name(s): {sorted(reserved)}")
        if nk.claim_field and nk.claim_field not in nk.content_fields:
            errs.append(f"kind '{kname}' claim_field '{nk.claim_field}' is not in content_fields; "
                        f"a contradiction over a field supersession ignores is unreachable")
        if nk.claim_slot and not nk.claim_field:
            errs.append(f"kind '{kname}' declares claim_slot but no claim_field — "
                        f"there is nothing for a detector to compare")
        if not (0.0 < nk.near_duplicate_threshold <= 1.0):
            errs.append(f"kind '{kname}' near_duplicate_threshold must be in (0, 1], "
                        f"got {nk.near_duplicate_threshold}")
    for vname, vocab in schema.vocabularies.items():
        if not vocab.terms:
            errs.append(f"vocabulary '{vname}' has no terms")
        for alias, canonical in vocab.synonyms.items():
            if alias in vocab.terms:
                errs.append(f"vocabulary '{vname}': '{alias}' is both a canonical term and a "
                            f"synonym of '{canonical}' — a term cannot fold onto another term")
        for term, rec in vocab.records.items():
            if rec.status == DEPRECATED and rec.replaced_by and rec.replaced_by not in vocab.terms:
                errs.append(f"vocabulary '{vname}': '{term}' is replaced_by '{rec.replaced_by}', "
                            f"which is not a term in this vocabulary")
            if rec.status not in (ACTIVE, DEPRECATED):
                errs.append(f"vocabulary '{vname}': term '{term}' has unknown status "
                            f"'{rec.status}' (expected {ACTIVE} or {DEPRECATED})")
    if load_checks and schema.check_specs:
        from .plugins import load_check_registries
        try:
            load_check_registries(schema.check_specs, base_dir=schema.base_dir)
        except Exception as exc:                              # noqa: BLE001
            errs.append(f"checks: {exc}")
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
    slot: str = ""                                # serialized claim_slot value
