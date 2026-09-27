"""Core data model.

A *run* takes a repository with a failing test and produces a *report*:
three hypotheses, three raced branches, one evidence-backed winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BranchStatus(str, Enum):
    PENDING = "pending"
    GREEN = "green"
    RED = "red"
    ERROR = "error"


@dataclass
class Edit:
    """A single find/replace edit proposed by a model."""

    file: str
    find: str
    replace: str


@dataclass
class Hypothesis:
    """One candidate root cause, plus the change that would fix it."""

    id: int
    title: str
    rationale: str
    edits: list[Edit] = field(default_factory=list)


@dataclass
class TestOutcome:
    ok: bool = False
    passed: int = 0
    failed: int = 0
    summary: str = ""


@dataclass
class BranchResult:
    hypothesis_id: int
    status: BranchStatus = BranchStatus.PENDING
    outcome: TestOutcome = field(default_factory=TestOutcome)
    lines_changed: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    rounds: int = 0
    log_tail: str = ""


@dataclass
class RunReport:
    repo: str
    test_command: str
    baseline: TestOutcome = field(default_factory=TestOutcome)
    branches: list[BranchResult] = field(default_factory=list)
    winner_id: int | None = None
    winner_reason: str = ""
    winner_diff: str = ""
    notes: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    diagnosis_raw: str = ""
    diagnosis_tokens: int = 0
    diagnosis_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.diagnosis_tokens + sum(b.tokens_used for b in self.branches)

    @property
    def total_cost_usd(self) -> float:
        return self.diagnosis_cost_usd + sum(b.cost_usd for b in self.branches)
