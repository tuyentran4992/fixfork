"""Evidence-based scoring: pick the branch that actually passes, with a small diff."""

from __future__ import annotations

from .models import BranchResult, BranchStatus


def rank_key(branch: BranchResult) -> tuple:
    """Sort key (ascending = better): green first, then fewest failures,
    then smallest diff, then lowest hypothesis id (deterministic ties)."""
    return (
        0 if branch.status is BranchStatus.GREEN else 1,
        branch.outcome.failed,
        branch.lines_changed,
        branch.hypothesis_id,
    )


def pick_winner(branches: list[BranchResult]) -> tuple[int | None, str]:
    if not branches:
        return None, "no branches were run"

    greens = [b for b in branches if b.status is BranchStatus.GREEN]
    pool = greens or branches
    best = sorted(pool, key=rank_key)[0]

    if best.status is BranchStatus.GREEN:
        reason = (
            f"branch {best.hypothesis_id} passed the tests ({best.outcome.summary}) "
            f"with {best.lines_changed} line(s) changed"
        )
    else:
        reason = (
            f"no branch went green; branch {best.hypothesis_id} failed least "
            f"({best.outcome.summary}) - treat its edits as a lead, not a fix"
        )
    return best.hypothesis_id, reason
