"""knowledge-graph-governance (kgg) — a governed write-path for knowledge graphs.

Your writer (an LLM, a script, a human) proposes changes as a declarative file.
kgg validates every write against a controlled vocabulary, refuses implicit node
creation, versions changed beliefs, stamps provenance, holds anything ambiguous
for human approval, and records it all in a tamper-evident audit log — before
anything reaches the graph.

The kernel (verdicts, audit, provenance, proposals) imports no database; a
Backend adapter is the only thing that touches a store. The node model and
controlled vocabulary are declared in YAML (see model.Schema), so the same
engine governs any domain — the bundled GTM taxonomy pack is just one Schema.
"""
from .verdicts import Verdict, Finding
from .model import Schema, Vocabulary, NodeKind, ExistingNode
from .backends.base import Backend, open_backend

__version__ = "0.1.0"

__all__ = [
    "Verdict", "Finding", "Schema", "Vocabulary", "NodeKind", "ExistingNode",
    "Backend", "open_backend", "__version__",
]
