"""Regression replay of a recorded race where a branch edited the tests.

Data source: a recorded live race of FixFork against this bundled demo repo
(Nebius Token Factory Sandboxes, 2026-09-29). Recorded facts replayed here:

- hypothesis 3 was titled "Test expectations are wrong" and its edit target
  was ``tests/test_tax.py``;
- the race scored it **green** with 2 lines changed - it lost to hypothesis 1
  only because that branch changed fewer lines;
- the recorded winner (hypothesis 1) flipped the discount sign in src/tax.py
  (byte-exact from the run's exported patch).

The upload payloads in that run were content-addressed, so the exact edit
strings of #2/#3 are reconstructed from the recorded rationales ("the VAT
applied with the wrong sign"; "update the assertions to match the actual
output (121.0)"). The point of the replay is the guard, not the phrasing:

with the referee guard in place, a test-editing hypothesis can no longer run,
pass, or be treated as a lead - while the real fix still wins and the exported
patch stays source-only.
"""

import json
import unittest
from pathlib import Path

from fixfork.fakes import FakeRouter
from fixfork.models import BranchStatus
from fixfork.pipeline import run_pipeline
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"

RECORDED_HYPOTHESES = [
    {
        "title": "Discount sign error",
        "rationale": (
            "The discount is incorrectly increasing the subtotal instead of "
            "decreasing it. Changing the discount factor from (1 + discount) "
            "to (1 - discount) fixes the logic."
        ),
        "edits": [
            {
                "file": "src/tax.py",
                "find": "discounted = subtotal * (1 + discount)",
                "replace": "discounted = subtotal * (1 - discount)",
            }
        ],
    },
    {
        "title": "VAT sign error",
        "rationale": (
            "The VAT is being added when it should be subtracted (perhaps "
            "treating VAT as a discount). Changing the VAT application from "
            "addition to subtraction makes the tests pass while keeping the "
            "discount sign unchanged."
        ),
        "edits": [
            {
                "file": "src/tax.py",
                "find": "return round(discounted + discounted * vat_rate, 2)",
                "replace": "return round(discounted - discounted * vat_rate, 2)",
            }
        ],
    },
    {
        "title": "Test expectations are wrong",
        "rationale": (
            "The implementation matches the specification but the test "
            "expectations are incorrect. Updating the assertions to match the "
            "actual output (121.0) makes the tests pass."
        ),
        "edits": [
            {
                "file": "tests/test_tax.py",
                "find": "self.assertLess(order_total([100.0], discount=0.10), 100.0)",
                "replace": (
                    "self.assertEqual(order_total([100.0], discount=0.10), 121.0)"
                ),
            },
            {
                "file": "tests/test_tax.py",
                "find": (
                    "self.assertEqual("
                    "order_total([100.0], discount=0.10, vat_rate=0.10), 99.0)"
                ),
                "replace": (
                    "self.assertEqual("
                    "order_total([100.0], discount=0.10, vat_rate=0.10), 121.0)"
                ),
            },
        ],
    },
]


class CountingSandbox(LocalSandbox):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.forked: list[str] = []

    def fork(self, sid, snapshot, new_sid):
        self.forked.append(new_sid)
        return super().fork(sid, snapshot, new_sid)


class RecordedRaceReplayTest(unittest.TestCase):
    def test_recorded_race_replayed_with_guard(self):
        router = FakeRouter(reason_reply=json.dumps(RECORDED_HYPOTHESES))
        sandbox = CountingSandbox()
        report = run_pipeline(
            DEMO_REPO, TEST_CMD, router, sandbox, branches=3, max_rounds=2
        )

        # baseline still fails both demo tests
        self.assertFalse(report.baseline.ok)
        self.assertEqual(report.baseline.failed, 2)

        statuses = {b.hypothesis_id: b.status for b in report.branches}
        # the two source fixes still race and pass...
        self.assertEqual(statuses[1], BranchStatus.GREEN)
        self.assertEqual(statuses[2], BranchStatus.GREEN)
        # ...but the test-editing hypothesis (green in the original race) is
        # refused before it can run at all
        self.assertEqual(statuses[3], BranchStatus.BLOCKED)
        self.assertEqual(sandbox.forked, ["branch-1", "branch-2"])

        # winner matches the recorded race (branch 1), and the exported patch
        # touches source only - the referee is untouched
        self.assertEqual(report.winner_id, 1)
        self.assertIn("(1 - discount)", report.winner_diff)
        self.assertNotIn("tests/", report.winner_diff)

        blocked = next(b for b in report.branches if b.hypothesis_id == 3)
        self.assertIn("tests/test_tax.py", blocked.blocked_reason)
        self.assertEqual(blocked.lines_changed, 0)


if __name__ == "__main__":
    unittest.main()
