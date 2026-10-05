"""End-to-end pipeline test, fully offline (fake router + local sandbox)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.models import BranchStatus
from fixfork.pipeline import run_pipeline
from fixfork.sandbox_runner import LocalSandbox, SandboxError

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

    def test_duplicate_hypotheses_collapse_instead_of_sinking_the_run(self):
        # race shape: three differently titled hypotheses, the same edits.
        # Before the collapse fix the whole reply was rejected -> 0 branches.
        edit = DEMO_HYPOTHESES[0]["edits"][0]
        duplicate_reply = json.dumps(
            [
                {"title": f"Theory {i}", "rationale": "same fix", "edits": [dict(edit)]}
                for i in range(1, 4)
            ]
        )
        router = FakeRouter(reason_reply=duplicate_reply)
        sandbox = LocalSandbox()
        report = run_pipeline(DEMO_REPO, TEST_CMD, router, sandbox, branches=3, max_rounds=1)

        # the fix still raced: one collapsed branch, and it won
        self.assertEqual(len(report.branches), 1)
        self.assertEqual(report.branches[0].status, BranchStatus.GREEN)
        self.assertEqual(report.winner_id, 1)
        # honesty: the collapse is recorded, and the reply was not re-asked
        self.assertTrue(any("collapsed" in note for note in report.notes))
        self.assertFalse(any("did not parse" in note for note in report.notes))
        reason_calls = [c for c in router.calls if c[0] == "reason"]
        self.assertEqual(len(reason_calls), 1)

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


class TypeContextPipelineTest(unittest.TestCase):
    """The type-context files must reach BOTH prompt stages (measured gap).

    Live evidence (2026-10-05, mcp-atlassian #1578): the defining class of the
    object each fix tried to verify never reached either prompt, and every
    branch (and its follow-up loop) guessed the type. This test drives the
    real pipeline on a synthetic repo with a cross-module import and asserts
    the definition file is rendered, annotated, and noted in the report.
    """

    def _make_repo(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="fixfork-typectx-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "app").mkdir()
        (tmp / "tests").mkdir()
        (tmp / "app" / "__init__.py").write_text("")
        (tmp / "app" / "core.py").write_text("def add(a, b):\n    return a + b\n")
        (tmp / "app" / "helpers.py").write_text("class Widget:\n    pass\n")
        (tmp / "tests" / "__init__.py").write_text("")
        (tmp / "tests" / "test_core.py").write_text(
            "import unittest\n\n"
            "from app.core import add\n"
            "from app.helpers import Widget  # noqa: F401\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(1, 1), 3)\n"
        )
        return tmp

    def test_type_context_reaches_diagnosis_and_loop_prompts(self):
        repo = self._make_repo()
        reply = json.dumps(
            [
                {
                    "title": "Off-by-one in add",
                    "rationale": "the addition returns the wrong value",
                    "edits": [
                        {
                            "file": "app/core.py",
                            "find": "return a + b",
                            "replace": "return a + b + 0",
                        }
                    ],
                }
            ]
        )
        router = FakeRouter(reason_reply=reply)
        report = run_pipeline(
            repo,
            "python3 -m unittest discover -s tests",
            router,
            LocalSandbox(),
            branches=1,
            max_rounds=2,
        )

        # branch stayed red (the edit applies but does not fix) -> loop ran
        self.assertEqual(report.branches[0].status, BranchStatus.RED)
        reason_prompts = [c[1] for c in router.calls if c[0] == "reason"]
        self.assertEqual(len(reason_prompts), 1)
        prompt = reason_prompts[0]
        self.assertIn("--- app/helpers.py ---", prompt)
        self.assertIn("class Widget", prompt)
        self.assertIn("(type context above: app/core.py, app/helpers.py", prompt)
        self.assertTrue(any(n.startswith("type context:") for n in report.notes))

        loop_prompts = [c[1] for c in router.calls if c[0] == "loop"]
        self.assertTrue(loop_prompts)  # red branch consults the loop model
        loop_text = loop_prompts[0]
        self.assertIn("--- app/helpers.py ---", loop_text)
        self.assertIn("class Widget", loop_text)
        self.assertIn("(type context above:", loop_text)


class TailTest(unittest.TestCase):
    def test_zero_and_negative_budget_do_not_return_whole_output(self):
        # Soi chéo 05/10: output[-0:] is output[0:] - a zero budget used to
        # return the WHOLE log, the opposite of the request.
        from fixfork.pipeline import _tail

        self.assertEqual(_tail("abcdef", 0), "")
        self.assertEqual(_tail("abcdef", -3), "")
        self.assertEqual(_tail(None, 10), "")
        self.assertEqual(_tail("abcdef", 3), "def")
        self.assertEqual(_tail("ab", 10), "ab")


class SandboxFailureTest(unittest.TestCase):
    """A failing sandbox step must cost the offending branch, not the run."""

    def _make_red_repo(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="fixfork-sbfail-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "app.py").write_text("VALUE = 1\n")
        (tmp / "tests").mkdir()
        (tmp / "tests" / "__init__.py").write_text("")
        (tmp / "tests" / "test_x.py").write_text(
            "import unittest\n\nfrom app import VALUE\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(VALUE, 2)\n"
        )
        return tmp

    def _reply(self) -> str:
        return json.dumps(
            [
                {
                    "title": "bump VALUE",
                    "rationale": "the module constant is stale",
                    "edits": [
                        {
                            "file": "app.py",
                            "find": "VALUE = 1",
                            "replace": "VALUE = 1  # touched",
                        }
                    ],
                }
            ]
        )

    def test_fork_failure_keeps_the_run_alive(self):
        # Soi chéo 05/10: a SandboxError from fork() used to escape the
        # per-branch handling and kill the whole run.
        repo = self._make_red_repo()

        class FailingForkSandbox(LocalSandbox):
            def fork(self, sid, snapshot, new_sid):
                raise SandboxError("no capacity")

        report = run_pipeline(
            repo,
            TEST_CMD,
            FakeRouter(reason_reply=self._reply()),
            FailingForkSandbox(),
            branches=1,
        )
        self.assertEqual(len(report.branches), 1)
        self.assertEqual(report.branches[0].status, BranchStatus.ERROR)
        self.assertIn("fork failed", " ".join(report.notes))
        # the branch never ran, so no fix can be exported: at most a lead
        self.assertEqual(report.winner_diff, "")
        self.assertIn("lead", report.winner_reason)

    def test_branch_run_failure_is_contained(self):
        repo = self._make_red_repo()

        class FailingBranchRunSandbox(LocalSandbox):
            def run(self, sid, command, timeout=120):
                if sid != "baseline":
                    raise SandboxError("exec timeout")
                return super().run(sid, command, timeout=timeout)

        report = run_pipeline(
            repo,
            TEST_CMD,
            FakeRouter(reason_reply=self._reply()),
            FailingBranchRunSandbox(),
            branches=1,
        )
        self.assertEqual(report.branches[0].status, BranchStatus.ERROR)

    def test_loop_read_tree_failure_keeps_measured_outcome(self):
        # Soi chéo 05/10: read_tree failing inside the loop used to flip a
        # measured red branch to error via the outer except, discarding the
        # outcome the tests had already produced.
        repo = self._make_red_repo()

        class BrokenReadTreeSandbox(LocalSandbox):
            def read_tree(self, sid):
                if sid != "baseline":
                    raise SandboxError("tree unreadable")
                return super().read_tree(sid)

        report = run_pipeline(
            repo,
            TEST_CMD,
            FakeRouter(reason_reply=self._reply()),
            BrokenReadTreeSandbox(),
            branches=1,
            max_rounds=2,
        )
        branch = report.branches[0]
        self.assertEqual(branch.status, BranchStatus.RED)
        self.assertIn("read_tree failed", " ".join(report.notes))


if __name__ == "__main__":
    unittest.main()
