"""Tests for the local sandbox backend (dev/demo only, no isolation)."""

import unittest
from pathlib import Path

from fixfork.models import Edit
from fixfork.sandbox_runner import LocalSandbox, SandboxError, locate_edit

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"

FIX = Edit(
    file="src/tax.py",
    find="discounted = subtotal * (1 + discount)",
    replace="discounted = subtotal * (1 - discount)",
)


class LocalSandboxTest(unittest.TestCase):
    def setUp(self):
        self.box = LocalSandbox()
        self.sid = self.box.create(DEMO_REPO, "t1")

    def test_failing_run_apply_fix_then_rollback(self):
        res = self.box.run(self.sid, TEST_CMD)
        self.assertNotEqual(res.returncode, 0)

        snapshot = self.box.checkpoint(self.sid)
        changed = self.box.apply_edits(self.sid, [FIX])
        self.assertEqual(changed, 1)

        fixed = self.box.run(self.sid, TEST_CMD)
        self.assertEqual(fixed.returncode, 0, msg=fixed.output)
        self.assertIn("OK", fixed.output)

        self.box.rollback(self.sid, snapshot)
        again = self.box.run(self.sid, TEST_CMD)
        self.assertNotEqual(again.returncode, 0)

    def test_fork_copies_state_without_sharing(self):
        self.box.apply_edits(self.sid, [FIX])
        snapshot = self.box.checkpoint(self.sid)
        forked = self.box.fork(self.sid, snapshot, "t2")
        self.assertIn("(1 - discount)", self.box.read_tree(forked)["src/tax.py"])
        # editing the fork must not leak into the origin
        self.box.apply_edits(
            forked,
            [Edit(file="src/tax.py", find="(1 - discount)", replace="(1 - discount) + 0")],
        )
        self.assertNotIn("+ 0", self.box.read_tree(self.sid)["src/tax.py"])

    def test_missing_edit_target_raises(self):
        with self.assertRaises(SandboxError):
            self.box.apply_edits(
                self.sid, [Edit(file="src/tax.py", find="not in the file", replace="x")]
            )

    def test_missing_file_raises(self):
        with self.assertRaises(SandboxError):
            self.box.apply_edits(
                self.sid, [Edit(file="src/nope.py", find="x", replace="y")]
            )

    def test_unknown_sandbox_raises(self):
        with self.assertRaises(SandboxError):
            self.box.run("missing", TEST_CMD)

    def test_blank_line_drift_edit_applies(self):
        # The measured failure class on a real repo: the model dropped one
        # blank line from the find block. The fallback must apply it once, at
        # the right spot (tax.py has TWO blank lines; the find has one).
        drift = Edit(
            file="src/tax.py",
            find=(
                "VAT_RATE = 0.10\n\n"
                "def order_total(prices, discount=0.0, vat_rate=VAT_RATE):"
            ),
            replace=(
                "VAT_RATE = 0.20\n\n"
                "def order_total(prices, discount=0.0, vat_rate=VAT_RATE):"
            ),
        )
        changed = self.box.apply_edits(self.sid, [drift])
        self.assertEqual(changed, 3)
        content = self.box.read_tree(self.sid)["src/tax.py"]
        self.assertIn("VAT_RATE = 0.20", content)
        self.assertIn("def order_total", content)
        self.assertEqual(content.count("VAT_RATE = 0.20"), 1)


class LocateEditTest(unittest.TestCase):
    def test_exact_match_wins(self):
        self.assertEqual(locate_edit("x = 1\n", "x = 1"), (0, 5, "exact"))

    def test_blank_line_drift_matches_normalised(self):
        content = "a = 1\n\n\nb = 2\n"
        start, end, mode = locate_edit(content, "a = 1\n\nb = 2")
        self.assertEqual(mode, "normalised")
        self.assertEqual(content[start:end], "a = 1\n\n\nb = 2")

    def test_trailing_space_drift_matches_normalised(self):
        content = "a = 1   \nb = 2\n"
        start, end, mode = locate_edit(content, "a = 1\nb = 2")
        self.assertEqual(mode, "normalised")
        self.assertEqual(content[start:end], "a = 1   \nb = 2")

    def test_ambiguous_fallback_refused(self):
        # Two candidate regions under the normalised comparison: refuse
        # instead of guessing which one the model meant.
        self.assertIsNone(locate_edit("p\nq\n\np\nq\n", "p\n\nq"))

    def test_not_found_returns_none(self):
        self.assertIsNone(locate_edit("a = 1\n", "zzz"))

    def test_span_touches_last_line_without_trailing_newline(self):
        # Cross-check round 2 flagged a possible off-by-one here; measured:
        # no bug. This locks the case (file without a trailing newline, the
        # matched span ending at EOF).
        content = "a = 1 \nb = 2"
        start, end, mode = locate_edit(content, "a = 1\nb = 2")
        self.assertEqual(mode, "normalised")
        self.assertEqual(content[start:end], "a = 1 \nb = 2")
        self.assertEqual(end, len(content))


if __name__ == "__main__":
    unittest.main()
