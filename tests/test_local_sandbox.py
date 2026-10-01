"""Tests for the local sandbox backend (dev/demo only, no isolation)."""

import unittest
from pathlib import Path

from fixfork.models import Edit
from fixfork.sandbox_runner import (
    LocalSandbox,
    SandboxError,
    corrected_replace,
    locate_edit,
)

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

    def test_uniform_indent_shift_matches_with_positive_offset(self):
        # Live evidence (2026-10-01, run 3): every find carried exactly one
        # extra leading space per line - the occurrence list's separator bled
        # into the model's copy; the byte-exact apply refused all 3 branches.
        content = "        if x == 1:\n        elif x == 2:\n"
        find = "         if x == 1:\n         elif x == 2:"
        span = locate_edit(content, find)
        assert span is not None
        start, end, mode = span
        self.assertEqual(mode, "indent+1")
        self.assertEqual(content[start:end], "        if x == 1:\n        elif x == 2:")

    def test_indent_shift_edits_apply_in_order_for_repeated_lines(self):
        # Repeated identical sites resolve in order (same first-match
        # semantics as the exact search): the second edit re-scans the
        # updated content and lands on the next site.
        content = "    a = f()\n    a = f()\n"
        find = "     a = f()"
        span = locate_edit(content, find)
        assert span is not None
        start, end, mode = span
        self.assertEqual((start, end, mode), (0, 11, "indent+1"))
        corrected = corrected_replace("     a = g()", mode)
        assert corrected is not None
        self.assertEqual(corrected, "    a = g()")
        updated = content[:start] + corrected + content[end:]
        second = locate_edit(updated, find)
        assert second is not None
        self.assertEqual(second[0], 12)

    def test_indent_shift_beyond_bound_is_refused(self):
        content = "        x = 1\n"
        self.assertIsNone(locate_edit(content, " " * (8 + 9) + "x = 1"))

    def test_indent_shift_up_to_eight_is_accepted(self):
        # Measured live 2026-10-01: +1 on most lines, +5 on one continuation
        # line; the bound must cover the measured class.
        content = "        x = 1\n"
        span = locate_edit(content, " " * (8 + 5) + "x = 1")
        assert span is not None
        self.assertEqual(span[2], "indent+5")

    def test_indent_shift_must_be_uniform(self):
        content = "    a = 1\n        b = 2\n"
        find = "     a = 1\n        b = 2"  # +1 on the first line only
        self.assertIsNone(locate_edit(content, find))


class CorrectedReplaceTest(unittest.TestCase):
    """The replacement is shifted by the same K locate_edit accepted."""

    def test_positive_shift_strips_every_non_blank_line(self):
        replace = "         n = 2\n\n         m = 3"
        self.assertEqual(
            corrected_replace(replace, "indent+1"), "        n = 2\n\n        m = 3"
        )

    def test_negative_shift_adds_to_every_non_blank_line(self):
        self.assertEqual(
            corrected_replace("  a = 2\nb = 3", "indent-2"),
            "    a = 2\n  b = 3",
        )

    def test_shallow_line_in_replacement_is_refused(self):
        # "top = 2" has no leading space to give back: refuse the correction
        # instead of guessing.
        self.assertIsNone(corrected_replace("            ok = 1\ntop = 2", "indent+1"))

    def test_exact_and_normalised_modes_pass_through(self):
        self.assertEqual(corrected_replace("x", "exact"), "x")
        self.assertEqual(corrected_replace("x", "normalised"), "x")


if __name__ == "__main__":
    unittest.main()
