"""Pipeline pre-flight: a branch whose blocks do not locate is never run.

Measured failure class (30/09 run #2): the model emitted a block with a
one-character syntax slip; the branch forked anyway, died mid-apply and was
still counted among the branches that "ran". Now such a branch is refused
before any sandbox op and reported as ``not_run`` - the tests never
executed it, so it is never presented as a tested approach.
"""

import copy
import json
import unittest
from pathlib import Path

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.models import BranchStatus
from fixfork.pipeline import run_pipeline
from fixfork.report import render_markdown
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"

BROKEN = {
    "title": "Fix the rate constant",
    "rationale": "syntax slip in the find block (the measured run #2 case)",
    "edits": [
        {"file": "src/tax.py", "find": "VAT_RATE = 0.10,", "replace": "VAT_RATE = 0.11"}
    ],
}
BROKEN_2 = {
    "title": "Tidy the subtotal line",
    "rationale": "another syntax slip",
    "edits": [
        {"file": "src/tax.py", "find": "subtotal = sum(prices),", "replace": "subtotal = sum(prices)"}
    ],
}
BROKEN_3 = {
    "title": "Tidy the return line",
    "rationale": "another syntax slip",
    "edits": [
        {"file": "src/tax.py", "find": "return round(discounted,", "replace": "return discounted"}
    ],
}


def hyps(replacements: dict[int, dict]) -> str:
    items = copy.deepcopy(DEMO_HYPOTHESES)
    for index, hypothesis in replacements.items():
        items[index - 1] = hypothesis
    return json.dumps(items)


class CountingSandbox(LocalSandbox):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.forked: list[str] = []

    def fork(self, sid, snapshot, new_sid):
        self.forked.append(new_sid)
        return super().fork(sid, snapshot, new_sid)


class RecordingSink:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind, **data):
        self.events.append((kind, data))


class PipelinePreflightTest(unittest.TestCase):
    def test_broken_branch_is_not_forked_and_not_counted_as_run(self):
        router = FakeRouter(reason_reply=hyps({2: BROKEN}))
        sandbox = CountingSandbox()
        sink = RecordingSink()
        report = run_pipeline(
            DEMO_REPO, TEST_CMD, router, sandbox, branches=3, events=sink
        )

        statuses = {b.hypothesis_id: b.status for b in report.branches}
        self.assertEqual(statuses[1], BranchStatus.GREEN)
        self.assertEqual(statuses[2], BranchStatus.NOT_RUN)
        self.assertEqual(statuses[3], BranchStatus.RED)

        broken = next(b for b in report.branches if b.hypothesis_id == 2)
        self.assertEqual(broken.rounds, 0)
        self.assertEqual(broken.lines_changed, 0)
        self.assertEqual(broken.outcome.passed, 0)  # no outcome invented
        self.assertEqual(broken.outcome.failed, 0)
        self.assertIn("not found", broken.blocked_reason)
        self.assertIn("src/tax.py", broken.blocked_reason)

        # the broken branch never reached the sandbox; the others still race
        self.assertEqual(sandbox.forked, ["branch-1", "branch-3"])
        self.assertEqual(report.winner_id, 1)

        kinds = [kind for kind, _ in sink.events]
        self.assertIn("branch_not_run", kinds)
        done = [d for k, d in sink.events if k == "branch_done" and d.get("id") == 2]
        self.assertEqual(done[0]["status"], "not_run")

        text = render_markdown(report)
        self.assertIn("not_run", text)
        self.assertIn("src/tax.py", text)

    def test_all_broken_branches_mean_no_winner_and_no_patch(self):
        router = FakeRouter(
            reason_reply=hyps({1: BROKEN, 2: BROKEN_2, 3: BROKEN_3})
        )
        sandbox = CountingSandbox()
        report = run_pipeline(DEMO_REPO, TEST_CMD, router, sandbox, branches=3)

        self.assertEqual(sandbox.forked, [])
        self.assertTrue(
            all(b.status is BranchStatus.NOT_RUN for b in report.branches)
        )
        self.assertIsNone(report.winner_id)
        self.assertIn("no branch could be run", report.winner_reason)
        self.assertEqual(report.winner_diff, "")


if __name__ == "__main__":
    unittest.main()
