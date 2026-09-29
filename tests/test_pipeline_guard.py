"""Guard behavior wired into the pipeline, offline (fake router + local sandbox)."""

import copy
import json
import unittest
from pathlib import Path

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.models import BranchStatus
from fixfork.pipeline import run_pipeline
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"

TEST_EDIT = {
    "file": "tests/test_tax.py",
    "find": "self.assertLess(order_total([100.0], discount=0.10), 100.0)",
    "replace": "self.assertEqual(order_total([100.0], discount=0.10), 121.0)",
}
TEST_EDIT_2 = {
    "file": "tests/test_tax.py",
    "find": "self.assertEqual(order_total([100.0], discount=0.10, vat_rate=0.10), 99.0)",
    "replace": "self.assertEqual(order_total([100.0], discount=0.10, vat_rate=0.10), 121.0)",
}


def hyps(replacements: dict[int, dict]) -> str:
    """DEMO_HYPOTHESES with certain slots replaced; returns the router reply."""
    items = copy.deepcopy(DEMO_HYPOTHESES)
    for index, hypothesis in replacements.items():
        items[index - 1] = hypothesis
    return json.dumps(items)


class RecordingSink:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind, **data):
        self.events.append((kind, data))


class CountingSandbox(LocalSandbox):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.forked: list[str] = []

    def fork(self, sid, snapshot, new_sid):
        self.forked.append(new_sid)
        return super().fork(sid, snapshot, new_sid)


class PipelineGuardTest(unittest.TestCase):
    def test_branch_editing_tests_is_blocked_and_never_green(self):
        router = FakeRouter(
            reason_reply=hyps(
                {
                    3: {
                        "title": "Test expectations are wrong",
                        "rationale": "Update the assertions to match the actual output.",
                        "edits": [TEST_EDIT],
                    }
                }
            )
        )
        sandbox = CountingSandbox()
        sink = RecordingSink()
        report = run_pipeline(
            DEMO_REPO, TEST_CMD, router, sandbox, branches=3, events=sink
        )

        statuses = {b.hypothesis_id: b.status for b in report.branches}
        self.assertEqual(statuses[1], BranchStatus.GREEN)
        self.assertEqual(statuses[2], BranchStatus.RED)
        self.assertEqual(statuses[3], BranchStatus.BLOCKED)

        blocked = next(b for b in report.branches if b.hypothesis_id == 3)
        self.assertIn("tests/test_tax.py", blocked.blocked_reason)

        # the invalid branch was never forked in the sandbox at all
        self.assertEqual(sandbox.forked, ["branch-1", "branch-2"])

        # the valid fix still wins, and the exported diff is source-only
        self.assertEqual(report.winner_id, 1)
        self.assertIn("(1 - discount)", report.winner_diff)
        self.assertNotIn("tests/", report.winner_diff)

        # the refusal is visible in the audit trail
        kinds = [kind for kind, _ in sink.events]
        self.assertIn("branch_blocked", kinds)
        blocked_events = [d for kind, d in sink.events if kind == "branch_blocked"]
        self.assertEqual(blocked_events[0]["id"], 3)

    def test_all_branches_blocked_means_no_winner_and_no_patch(self):
        router = FakeRouter(
            reason_reply=hyps(
                {
                    1: {"title": "Tweak the tests", "rationale": "r1", "edits": [TEST_EDIT]},
                    2: {"title": "Relax the tests", "rationale": "r2", "edits": [TEST_EDIT_2]},
                    3: {
                        "title": "Fix the harness",
                        "rationale": "r3",
                        "edits": [
                            {
                                "file": "conftest.py",
                                "find": "x",
                                "replace": "y",
                            }
                        ],
                    },
                }
            )
        )
        sandbox = CountingSandbox()
        report = run_pipeline(DEMO_REPO, TEST_CMD, router, sandbox, branches=3)

        self.assertEqual(sandbox.forked, [])
        self.assertTrue(all(b.status is BranchStatus.BLOCKED for b in report.branches))
        self.assertIsNone(report.winner_id)
        self.assertIn("blocked", report.winner_reason)
        self.assertEqual(report.winner_diff, "")

    def test_loop_edit_to_tests_is_refused(self):
        # branch 1 fixes; branches 2 and 3 apply valid source edits that do not
        # fix anything. When the loop model then proposes a test edit, the
        # branch must be blocked instead of racing it.
        router = FakeRouter(
            reason_reply=hyps(
                {
                    2: {
                        "title": "Rename a constant",
                        "rationale": "r2",
                        "edits": [
                            {
                                "file": "src/tax.py",
                                "find": "VAT_RATE = 0.10",
                                "replace": "VAT_RATE = 0.10  # nominal rate",
                            }
                        ],
                    },
                    3: {
                        "title": "Comment the subtotal line",
                        "rationale": "r3",
                        "edits": [
                            {
                                "file": "src/tax.py",
                                "find": "subtotal = sum(prices)",
                                "replace": "subtotal = sum(prices)  # total",
                            }
                        ],
                    },
                }
            ),
            loop_reply=json.dumps({"edits": [TEST_EDIT]}),
        )
        sink = RecordingSink()
        report = run_pipeline(
            DEMO_REPO, TEST_CMD, router, LocalSandbox(), branches=3, events=sink
        )

        statuses = {b.hypothesis_id: b.status for b in report.branches}
        self.assertEqual(statuses[1], BranchStatus.GREEN)
        self.assertEqual(statuses[2], BranchStatus.BLOCKED)
        self.assertEqual(statuses[3], BranchStatus.BLOCKED)
        self.assertEqual(report.winner_id, 1)

        for hid in (2, 3):
            blocked = next(b for b in report.branches if b.hypothesis_id == hid)
            self.assertIn("tests/test_tax.py", blocked.blocked_reason)

    def test_patch_withheld_when_winner_tree_sneaks_in_a_test_change(self):
        # Simulates an upstream bug: the winner's tree contains a test-file
        # change even though its edits never touched tests, so pre-flight had
        # nothing to refuse. The diff-level check must withhold the patch.
        class SneakySandbox(LocalSandbox):
            def read_tree(self, sid):
                tree = super().read_tree(sid)
                if sid == "branch-1":
                    tree["tests/test_tax.py"] += "\n# sneaky\n"
                return tree

        report = run_pipeline(
            DEMO_REPO, TEST_CMD, FakeRouter(), SneakySandbox(), branches=3
        )

        self.assertEqual(report.winner_id, 1)
        self.assertEqual(report.winner_diff, "")
        self.assertIn("withheld", " ".join(report.notes))


if __name__ == "__main__":
    unittest.main()
