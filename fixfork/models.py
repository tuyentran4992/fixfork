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
