"""Evidence-based scoring: pick the branch that actually passes, with a small diff."""

from __future__ import annotations

from .models import BranchResult, BranchStatus

# Lower rank = better; first sort key. Branches that never ran (blocked:
# edits hit the referee; not_run: edits do not locate) rank below every
# branch that produced a measured outcome - they are not fixes and must
# never be surfaced as a "lead" either.
_STATUS_RANK = {
    BranchStatus.GREEN: 0,
    BranchStatus.RED: 1,
    BranchStatus.ERROR: 2,
    BranchStatus.NOT_RUN: 3,
    BranchStatus.BLOCKED: 4,
    BranchStatus.PENDING: 5,
}
# Unknown future statuses fall back to the worst rank on purpose: an
# unrecognized state must never be treated as a winner or a lead.


def rank_key(branch: BranchResult) -> tuple:
    """Sort key (ascending = better): passing first, then fewest failures,
    then smallest diff, then lowest hypothesis id (deterministic ties).
    Blocked branches always lose, even to plain failures."""
    return (
        _STATUS_RANK.get(branch.status, 9),
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
    elif best.status is BranchStatus.BLOCKED:
        n_blocked = sum(1 for b in branches if b.status is BranchStatus.BLOCKED)
        return None, (
            f"no branch produced a valid fix: {n_blocked} branch(es) were blocked "
            "for editing test/CI files (the test suite is the referee and is "
            "off-limits)"
        )
    elif best.status is BranchStatus.NOT_RUN:
        n_not_run = sum(1 for b in branches if b.status is BranchStatus.NOT_RUN)
        n_blocked = sum(1 for b in branches if b.status is BranchStatus.BLOCKED)
        detail = (
            f"{n_not_run} branch(es) had edits that do not locate in the "
            "baseline files (pre-flight refusal)"
        )
        if n_blocked:
            detail += (
                f"; {n_blocked} branch(es) were blocked by the referee guard"
            )
        return None, f"no branch could be run: {detail}"
    else:
        reason = (
            f"no branch went green; branch {best.hypothesis_id} failed least "
            f"({best.outcome.summary}) - treat its edits as a lead, not a fix"
        )
    return best.hypothesis_id, reason
