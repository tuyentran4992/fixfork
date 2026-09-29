"""Turn test-runner output into a small, uniform outcome."""

from __future__ import annotations

import re

from .models import TestOutcome

_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.M)
_FAILED_RE = re.compile(r"^FAILED \(([^)]*)\)", re.M)
_OK_RE = re.compile(r"^OK\b", re.M)
_PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")


def _parse_pytest_counts(text: str) -> tuple[int, int] | None:
    """(passed, failed+errors) from a pytest summary line, or None.

    Only the final summary line carries both a duration (" in 0.20s") and the
    count words; matching per line keeps the "short test summary info" block
    (which lists lines like ``FAILED tests/...``) from being read as counts.
    When several lines qualify the LAST one wins (it is the final summary) -
    counts are not merged across lines, which would double-count.
    ``skipped`` is parsed but intentionally counted as neither passed nor
    failed. Added after a real repo run (humanize, 2026-09-29) where pytest
    was the runner and everything collapsed to "unrecognized output".
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if " in " not in line:
            continue
        found = _PYTEST_COUNT_RE.findall(line)
        if not found:
            continue
        counts = {word: int(n) for n, word in found}  # last qualifying line wins
    if not counts:
        return None
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    return counts.get("passed", 0), failed


def parse_unittest_output(text: str, returncode: int = 0) -> TestOutcome:
    """Parse unittest-style output; pytest summaries; else the exit code."""
    ran = _RAN_RE.search(text)
    total = int(ran.group(1)) if ran else 0

    failed_match = _FAILED_RE.search(text)
    if failed_match:
        parts = dict(
            piece.strip().split("=")
            for piece in failed_match.group(1).split(",")
            if "=" in piece
        )
        n_failed = int(parts.get("failures", 0)) + int(parts.get("errors", 0))
        return TestOutcome(
            ok=False,
            passed=max(total - n_failed, 0),
            failed=n_failed,
            summary=f"FAILED ({n_failed} of {total})",
        )

    if _OK_RE.search(text):
        return TestOutcome(ok=True, passed=total, failed=0, summary=f"OK ({total} tests)")

    pytest_counts = _parse_pytest_counts(text)
    if pytest_counts is not None:
        passed, n_failed = pytest_counts
        total = passed + n_failed
        if n_failed:
            return TestOutcome(
                ok=False,
                passed=passed,
                failed=n_failed,
                summary=f"FAILED ({n_failed} of {total})",
            )
        return TestOutcome(
            ok=True, passed=passed, failed=0, summary=f"OK ({total} tests)"
        )

    return TestOutcome(
        ok=returncode == 0,
        passed=0,
        failed=0 if returncode == 0 else 1,
        summary=f"unrecognized output, exit code {returncode}",
    )
