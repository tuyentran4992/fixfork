"""Turn test-runner output into a small, uniform outcome."""

from __future__ import annotations

import re

from .models import TestOutcome

_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.M)
_FAILED_RE = re.compile(r"^FAILED \(([^)]*)\)", re.M)
_OK_RE = re.compile(r"^OK\b", re.M)


def parse_unittest_output(text: str, returncode: int = 0) -> TestOutcome:
    """Parse unittest-style output; fall back to the exit code for other runners."""
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

    return TestOutcome(
        ok=returncode == 0,
        passed=0,
        failed=0 if returncode == 0 else 1,
        summary=f"unrecognized output, exit code {returncode}",
    )
