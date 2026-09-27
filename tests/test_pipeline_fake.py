"""End-to-end pipeline test, fully offline (fake router + local sandbox)."""

import unittest
from pathlib import Path

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.models import BranchStatus
from fixfork.pipeline import run_pipeline
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"


class PipelineFakeTest(unittest.TestCase):
    def test_full_offline_race(self):
        router = FakeRouter()
        sandbox = LocalSandbox()
        report = run_pipeline(DEMO_REPO, TEST_CMD, router, sandbox, branches=3, max_rounds=2)

        # 1. baseline is red, with both demo tests failing
        self.assertFalse(report.baseline.ok)
        self.assertEqual(report.baseline.failed, 2)

        # 2. three branches raced
        self.assertEqual(len(report.branches), 3)
        statuses = {b.hypothesis_id: b.status for b in report.branches}
        self.assertEqual(statuses[1], BranchStatus.GREEN)
        self.assertEqual(statuses[2], BranchStatus.RED)
        self.assertEqual(statuses[3], BranchStatus.RED)

        # 2b. hypotheses retained for the report
        self.assertEqual(len(report.hypotheses), 3)
        self.assertEqual(report.hypotheses[0].title, DEMO_HYPOTHESES[0]["title"])

        # 3. winner is the branch that passed, with a usable diff
        self.assertEqual(report.winner_id, 1)
        self.assertIn("(1 - discount)", report.winner_diff)

        # 4. losing branches were rolled back to the baseline state
        base = sandbox.read_tree("baseline")
        self.assertEqual(sandbox.read_tree("branch-2"), base)
        self.assertEqual(sandbox.read_tree("branch-3"), base)

        # 5. loop model was consulted for the two failing branches only
        loop_calls = [c for c in router.calls if c[0] == "loop"]
        self.assertEqual(len(loop_calls), 2)

    def test_already_green_repo_is_left_alone(self):
        sandbox = LocalSandbox()
        # fix the repo copy first, then point the pipeline at a green sandbox
        green_dir = sandbox.root / "already-green"
        sid = sandbox.create(DEMO_REPO, "seed")
        from fixfork.models import Edit

        sandbox.apply_edits(
            sid,
            [Edit(file="src/tax.py", find="(1 + discount)", replace="(1 - discount)")],
        )
        sandbox.path_of(sid).rename(green_dir)

        report = run_pipeline(green_dir, TEST_CMD, FakeRouter(), LocalSandbox(), branches=3)
        self.assertTrue(report.baseline.ok)
        self.assertEqual(report.branches, [])
        self.assertIn("already passes", " ".join(report.notes))


if __name__ == "__main__":
    unittest.main()
