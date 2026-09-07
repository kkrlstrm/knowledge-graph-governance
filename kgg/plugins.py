"""Domain check registries — how a domain adds rules without forking the kernel.

The core registry in `checks.py` enforces what is true of *any* governed graph:
identity, controlled vocabulary, referential integrity, collision, protection.
What is true of *your* graph — "a fact asserting a market it has no evidence in
needs a human", "a `verified` tier needs an authoritative source" — is a domain
rule, and it belongs to you.

A domain registry is a module-level list of `(name, fn)` pairs, where `fn` takes
a `PlanContext` and returns `list[Finding]`:

    # my_package/checks.py
    from kgg.verdicts import Finding, Verdict

    def check_review_date_set(ctx):
        out = []
        for section, kind, item in ctx.sections():
            if kind == "fact" and not item.get("review_after"):
                out.append(Finding("review_date_set", ctx.iid_of(kind, item),
                                   Verdict.BLOCK, "a fact with no expiry outlives "
                                   "its shelf life while still reading as current"))
        return out

    REGISTRY = [("review_date_set", check_review_date_set)]

Declared in the schema, never on the command line — either as an installed
module, or as a file path resolved relative to the schema (the same rule
`vocabularies:` already follows, so a governance contract stays a directory you
can copy):

    # schema.yaml
    checks:
      - my_package.checks:REGISTRY     # installed module
      - domain_checks.py:REGISTRY      # sibling file

That placement is deliberate. The schema fingerprint hashes the schema file's
bytes, so the set of enforced rules is inside the fingerprint — changing which
checks run invalidates every approval granted under the old rules, exactly as
changing a vocabulary term does. A `--checks` flag would let a caller weaken the
gate for one run while reusing approvals granted under the strong one, which is
the whole failure this repo exists to prevent.

Note what the fingerprint does and does not cover: it pins the *declaration*, not
the file the declaration points at. Editing a check's body does not invalidate
approvals. Version your check pack the way you version any other code that can
refuse a write.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Callable

CheckFn = Callable[..., list]
Registry = list  # list[tuple[str, CheckFn]]


class CheckSpecError(ValueError):
    """A domain check registry could not be loaded — fail closed, never skip."""


def _parse(spec: str) -> tuple[str, str]:
    if not isinstance(spec, str) or ":" not in spec:
        raise CheckSpecError(
            f"{spec!r} is not a check spec; expected 'module.path:ATTRIBUTE'")
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise CheckSpecError(
            f"{spec!r} is not a check spec; expected 'module.path:ATTRIBUTE'")
    return module, attr


def _load_from_path(path: Path, spec: str):
    if not path.exists():
        raise CheckSpecError(f"check spec {spec!r}: no such file {path}")
    mod_name = f"_kgg_checks_{abs(hash(str(path.resolve())))}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    loader = importlib.util.spec_from_file_location(mod_name, path)
    if loader is None or loader.loader is None:
        raise CheckSpecError(f"check spec {spec!r}: {path} is not importable as Python")
    module = importlib.util.module_from_spec(loader)
    sys.modules[mod_name] = module
    try:
        loader.loader.exec_module(module)
    except Exception as exc:                                   # noqa: BLE001
        del sys.modules[mod_name]
        raise CheckSpecError(f"check spec {spec!r}: {path} raised on import: {exc}") from exc
    return module


def load_registry(spec: str, base_dir=None) -> Registry:
    """Import one `module:ATTR` or `file.py:ATTR` registry, validating its shape."""
    module_name, attr = _parse(spec)

    if module_name.endswith(".py") or "/" in module_name or "\\" in module_name:
        p = Path(module_name)
        if not p.is_absolute() and base_dir:
            p = Path(base_dir) / p
        module = _load_from_path(p, spec)
    else:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise CheckSpecError(
                f"cannot import '{module_name}' for check spec {spec!r}: {exc}") from exc

    if not hasattr(module, attr):
        raise CheckSpecError(f"module '{module_name}' has no attribute '{attr}'")
    registry = getattr(module, attr)
    if not isinstance(registry, (list, tuple)):
        raise CheckSpecError(
            f"{spec!r} is a {type(registry).__name__}, expected a list of (name, fn) pairs")
    out = []
    for i, entry in enumerate(registry):
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise CheckSpecError(f"{spec!r} entry {i} is not a (name, fn) pair")
        name, fn = entry
        if not isinstance(name, str) or not name:
            raise CheckSpecError(f"{spec!r} entry {i} has a non-string check name")
        if not callable(fn):
            raise CheckSpecError(f"{spec!r} check '{name}' is not callable")
        out.append((name, fn))
    return out


def load_check_registries(specs, base_dir=None) -> Registry:
    """Load every declared registry, in order, refusing name collisions.

    A duplicate name is refused rather than last-wins because a rejection reports
    the check that fired: two rules answering to one name make the report
    ambiguous about which rule refused the write. Shadowing a core check name is
    refused for the same reason, and a stronger one — a domain pack that
    redefined `vocab_membership` would silently replace the taxonomy-drift
    guarantee with whatever it felt like.
    """
    out: Registry = []
    seen: dict = {}
    core = core_check_names()
    for spec in specs or ():
        for name, fn in load_registry(spec, base_dir=base_dir):
            if name in core:
                raise CheckSpecError(
                    f"check name '{name}' from {spec!r} shadows a kernel check; the core "
                    f"guarantees cannot be redefined by a domain pack. Rename it.")
            if name in seen:
                raise CheckSpecError(
                    f"duplicate check name '{name}' from {spec!r}; already provided by "
                    f"{seen[name]!r}. Check names appear in findings and proposals, so "
                    f"they must be unique.")
            seen[name] = spec
            out.append((name, fn))
    return out


def core_check_names() -> set:
    """Names the kernel already uses — a domain registry may not shadow these."""
    from .checks import CORE_CHECKS
    return {name for name, _ in CORE_CHECKS}
