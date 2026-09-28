"""Events: the structured audit trail of a run (also the demo-video raw material).

Locks: valid JSONL output, monotonic timestamps, the full race timeline for a
fake run (start -> baseline -> hypotheses -> 3 branches -> winner -> done), and
the CLI --events flag.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fixfork.__main__ import main
from fixfork.events import JsonlEventSink, NullSink, human_line, read_events
from fixfork.fakes import FakeRouter
from fixfork.pipeline import run_pipeline
from fixfork.sandbox_runner import LocalSandbox

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"


def _run_fake(tmp: str) -> Path:
    events_path = Path(tmp) / "events.jsonl"
    sink = JsonlEventSink(events_path)
    run_pipeline(
        repo=DEMO_REPO,
        test_command=TEST_CMD,
        router=FakeRouter(),
        sandbox=LocalSandbox(),
        events=sink,
    )
    sink.close()
    return events_path


class JsonlSinkTest(unittest.TestCase):
    def test_writes_valid_jsonl_with_monotonic_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            sink = JsonlEventSink(path)
            sink.emit("run_start", repo="x")
            sink.emit("done", total_tokens=10, cost=0.001)
            sink.close()
            lines = [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([l["event"] for l in lines], ["run_start", "done"])
            self.assertEqual(lines[0]["repo"], "x")
            self.assertGreaterEqual(lines[1]["t"], lines[0]["t"])

    def test_null_sink_is_a_noop(self):
        NullSink().emit("anything", x=1)  # must not raise


class PipelineEventsTest(unittest.TestCase):
    def test_fake_run_emits_full_race_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = read_events(_run_fake(tmp))
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "run_start")
        self.assertIn("baseline_done", kinds)
        self.assertIn("hypotheses_ready", kinds)
        self.assertEqual(kinds.count("branch_started"), 3)
        self.assertEqual(kinds.count("branch_done"), 3)
        self.assertIn("winner", kinds)
        self.assertEqual(kinds[-1], "done")
        timestamps = [e["t"] for e in events]
        self.assertEqual(timestamps, sorted(timestamps))
        winner = [e for e in events if e["event"] == "winner"][0]
        self.assertIsNotNone(winner["id"], "fake run should produce a winner")
        done = events[-1]
        # the fake router has no real tokens/cost — the done event still
        # carries the fields (real values on live runs)
        self.assertIn("total_tokens", done)
        self.assertIn("cost", done)

    def test_no_events_arg_stays_silent(self):
        # pipeline must remain sink-free by default (no file, no crash)
        report = run_pipeline(
            repo=DEMO_REPO,
            test_command=TEST_CMD,
            router=FakeRouter(),
            sandbox=LocalSandbox(),
        )
        self.assertIsNotNone(report.winner_id)


class CliEventsTest(unittest.TestCase):
    def test_events_flag_writes_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run.md"
            ev = Path(tmp) / "events.jsonl"
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
                    "--events",
                    str(ev),
                ]
            )
            self.assertEqual(code, 0)
            events = read_events(ev)
            self.assertEqual(events[0]["event"], "run_start")
            self.assertEqual(events[-1]["event"], "done")


class HumanLineTest(unittest.TestCase):
    def test_rollback_line_mentions_the_branch(self):
        line = human_line("rollback", {"id": 2})
        self.assertIsNotNone(line)
        assert line is not None  # for type checkers
        self.assertIn("branch 2", line)

    def test_unknown_event_has_no_line(self):
        self.assertIsNone(human_line("mystery", {}))


if __name__ == "__main__":
    unittest.main()
