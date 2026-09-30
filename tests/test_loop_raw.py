"""Raw follow-up (loop) replies survive in the report - the run #2 gap.

A real run proposed follow-up edits whose raw replies were stored nowhere,
so the loop was not debuggable; these tests lock the contract: every loop
reply (and its cost) is kept, parse failures included, and the CLI writes
them to a sidecar file next to the report.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fixfork.__main__ import main
from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.model_router import ModelReply, RouterError
from fixfork.models import BranchStatus
from fixfork.pipeline import run_pipeline
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"
RED_ONLY = json.dumps([DEMO_HYPOTHESES[2]])  # leaves one test red -> loop runs


class BrokenLoopRouter(FakeRouter):
    """Loop reply that does not parse (the measured debugging gap)."""

    def complete(self, role, prompt):
        if role == "loop":
            self.calls.append((role, prompt))
            return ModelReply(text="{not json at all", tokens_used=13, cost_usd=0.0013)
        return super().complete(role, prompt)


class FailingLoopRouter(FakeRouter):
    """Loop call that errors out - the reason must be recorded too."""

    def complete(self, role, prompt):
        if role == "loop":
            raise RouterError("simulated loop failure")
        return super().complete(role, prompt)


class LoopRawTest(unittest.TestCase):
    def test_unparsable_loop_reply_kept_with_reason(self):
        report = run_pipeline(
            DEMO_REPO,
            TEST_CMD,
            BrokenLoopRouter(reason_reply=RED_ONLY),
            LocalSandbox(),
            branches=1,
            max_rounds=2,
        )
        self.assertEqual(report.branches[0].status, BranchStatus.RED)
        self.assertEqual(len(report.loop_raw), 1)
        entry = report.loop_raw[0]
        self.assertEqual(entry["branch_id"], 1)
        self.assertEqual(entry["text"], "{not json at all")
        self.assertIn("parse_error", entry)
        # the call happened, so its tokens and cost are counted
        self.assertEqual(report.branches[0].tokens_used, 13)
        self.assertAlmostEqual(report.branches[0].cost_usd, 0.0013)

    def test_valid_loop_reply_is_kept(self):
        report = run_pipeline(
            DEMO_REPO,
            TEST_CMD,
            FakeRouter(reason_reply=RED_ONLY),
            LocalSandbox(),
            branches=1,
            max_rounds=2,
        )
        self.assertEqual(len(report.loop_raw), 1)
        self.assertEqual(report.loop_raw[0]["text"], json.dumps({"edits": []}))
        self.assertNotIn("parse_error", report.loop_raw[0])
        self.assertEqual(report.branches[0].status, BranchStatus.RED)

    def test_router_error_is_recorded(self):
        report = run_pipeline(
            DEMO_REPO,
            TEST_CMD,
            FailingLoopRouter(reason_reply=RED_ONLY),
            LocalSandbox(),
            branches=1,
            max_rounds=2,
        )
        self.assertEqual(len(report.loop_raw), 1)
        self.assertIn("error", report.loop_raw[0])
        self.assertEqual(report.branches[0].status, BranchStatus.RED)

    def test_cli_writes_loop_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run.md"
            code = main(
                [
                    "run",
                    "--repo",
                    str(DEMO_REPO),
                    "--test",
                    TEST_CMD,
                    "--fake",
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 0)
            sidecar = Path(str(out) + ".loops.jsonl")
            self.assertTrue(sidecar.is_file(), "loop sidecar missing")
            entries = [
                json.loads(line)
                for line in sidecar.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(entries), 2)  # branches 2 and 3 are red
            for entry in entries:
                self.assertIn("branch_id", entry)
                self.assertIn("text", entry)

    def test_router_error_entry_has_uniform_schema(self):
        # Every sidecar entry carries the same keys (soi chéo 30/09): an
        # error-only entry would break consumers that read `text` on every
        # line.
        report = run_pipeline(
            DEMO_REPO,
            TEST_CMD,
            FailingLoopRouter(reason_reply=RED_ONLY),
            LocalSandbox(),
            branches=1,
            max_rounds=2,
        )
        self.assertEqual(len(report.loop_raw), 1)
        entry = report.loop_raw[0]
        for key in ("branch_id", "round", "text", "tokens", "cost_usd", "error"):
            self.assertIn(key, entry)

    def test_sidecar_writer_sanitises_non_finite_floats(self):
        # NaN/Infinity would make the JSONL invalid for strict consumers;
        # the writer degrades them to strings instead of emitting bare
        # NaN tokens (soi chéo 30/09).
        from fixfork.__main__ import _json_safe

        entry = {"cost_usd": float("nan"), "nested": {"inf": float("inf")}}
        line = json.dumps(_json_safe(entry), ensure_ascii=False, default=str)
        self.assertNotIn("NaN", line)
        self.assertNotIn("Infinity", line)

        def _reject(token):
            raise AssertionError(f"non-finite constant leaked: {token}")

        parsed = json.loads(line, parse_constant=_reject)
        self.assertEqual(parsed["cost_usd"], "nan")
        self.assertEqual(parsed["nested"]["inf"], "inf")


if __name__ == "__main__":
    unittest.main()
