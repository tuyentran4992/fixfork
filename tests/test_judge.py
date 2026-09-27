"""Tests for the evidence-based judge."""

import unittest

from fixfork.judge import pick_winner
from fixfork.models import BranchResult, BranchStatus, TestOutcome


def make_branch(hid, status, failed=0, lines=0, passed=0):
    branch = BranchResult(hypothesis_id=hid)
    branch.status = status
    branch.outcome = TestOutcome(ok=status is BranchStatus.GREEN, passed=passed, failed=failed)
    branch.lines_changed = lines
    return branch


class PickWinnerTest(unittest.TestCase):
    def test_green_beats_red_even_with_a_bigger_diff(self):
        green = make_branch(1, BranchStatus.GREEN, lines=50, passed=10)
        red = make_branch(2, BranchStatus.RED, failed=1, lines=1)
        winner, reason = pick_winner([red, green])
        self.assertEqual(winner, 1)
        self.assertIn("passed", reason)

    def test_smallest_diff_wins_among_greens(self):
        big = make_branch(1, BranchStatus.GREEN, lines=40, passed=10)
        small = make_branch(2, BranchStatus.GREEN, lines=3, passed=10)
        winner, _ = pick_winner([big, small])
        self.assertEqual(winner, 2)

    def test_no_green_picks_fewest_failures_and_warns(self):
        worse = make_branch(1, BranchStatus.RED, failed=3)
        better = make_branch(2, BranchStatus.RED, failed=1)
        winner, reason = pick_winner([worse, better])
        self.assertEqual(winner, 2)
        self.assertIn("no branch went green", reason)

    def test_identical_scores_fall_back_to_lowest_id(self):
        a = make_branch(1, BranchStatus.RED, failed=1, lines=2)
        b = make_branch(2, BranchStatus.RED, failed=1, lines=2)
        winner, _ = pick_winner([b, a])
        self.assertEqual(winner, 1)

    def test_empty(self):
        winner, reason = pick_winner([])
        self.assertIsNone(winner)
        self.assertIn("no branches", reason)

    def test_error_branches_lose_to_red(self):
        error = make_branch(1, BranchStatus.ERROR, failed=99)
        red = make_branch(2, BranchStatus.RED, failed=2)
        winner, _ = pick_winner([error, red])
        self.assertEqual(winner, 2)


if __name__ == "__main__":
    unittest.main()
