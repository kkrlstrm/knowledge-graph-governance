"""Graded verdicts + findings.

Four graded verdicts under strict precedence, so the gate distinguishes
"reject one item" from "stop the whole run" from "a human must look first":

    ALLOW             safe to write automatically
    REQUIRE_APPROVAL  loadable, but a human must clear it (conflict, unmapped,
                      anything ambiguous)
    BLOCK             reject this single item; the rest of the run proceeds
    HALT              abort the entire run (malformed file, protected-node attack)

Precedence is numeric: the worst verdict wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Verdict(IntEnum):
    ALLOW = 0
    REQUIRE_APPROVAL = 1
    BLOCK = 2
    HALT = 3

    @property
    def label(self) -> str:
        return self.name

    @property
    def glyph(self) -> str:
        return {0: "✓", 1: "⏸", 2: "✗", 3: "⛔"}[int(self)]


SEVERITY = ("info", "warn", "error")

# Sentinel target for run-level (not item-level) findings.
RUN = "__run__"


@dataclass
class Finding:
    """One observation from one named check about one target."""
    check: str
    target: str          # an item key, or RUN for run-level findings
    verdict: Verdict
    message: str
    severity: str = "error"
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "target": self.target,
            "verdict": self.verdict.label,
            "severity": self.severity,
            "message": self.message,
            "data": self.data,
        }


def worst(findings: list[Finding], default: Verdict = Verdict.ALLOW) -> Verdict:
    v = default
    for f in findings:
        if f.verdict > v:
            v = f.verdict
    return v


def item_verdict(findings: list[Finding], target: str) -> Verdict:
    return worst([f for f in findings if f.target == target])


def run_halts(findings: list[Finding]) -> bool:
    return any(f.verdict == Verdict.HALT for f in findings)
