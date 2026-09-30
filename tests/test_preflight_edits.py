"""Pre-flight edit check: every edit must locate before a branch is forked.

The check must mirror apply semantics (same ``locate_edit``, same order) or
it would wave through exactly the replies the guard exists for.
"""

import unittest

from fixfork.models import Edit
from fixfork.sandbox_runner import preflight_edits

FILES = {"src/a.py": "x = 1\ny = 2\n", "src/b.py": "z = 3\n"}


def edit(file, find, replace="R"):
    return Edit(file=file, find=find, replace=replace)


class PreflightEditsTest(unittest.TestCase):
    def test_all_edits_locate_exactly(self):
        problems = preflight_edits(
            FILES, [edit("src/a.py", "x = 1"), edit("src/b.py", "z = 3")]
        )
        self.assertEqual(problems, [])

    def test_missing_file_is_a_problem(self):
        problems = preflight_edits(FILES, [edit("src/ghost.py", "x = 1")])
        self.assertEqual(len(problems), 1)
        self.assertIn("not in the baseline tree", problems[0])

    def test_missing_find_is_a_problem(self):
        problems = preflight_edits(FILES, [edit("src/a.py", "w = 0")])
        self.assertEqual(len(problems), 1)
        self.assertIn("not found", problems[0])

    def test_blank_line_drift_still_locates(self):
        # Same tolerance as apply: exact first, bounded blank-line
        # normalisation second (the measured drift class on a real repo).
        files = {"src/a.py": "def f():\n    a = 1\n\n    return a\n"}
        drifted = "def f():\n    a = 1\n    return a"  # one blank line dropped
        self.assertEqual(preflight_edits(files, [edit("src/a.py", drifted)]), [])

    def test_ambiguous_normalised_match_is_refused(self):
        files = {"src/a.py": "a = 1\n\nb = 2\na = 1\n\nb = 2\n"}
        problems = preflight_edits(files, [edit("src/a.py", "a = 1\nb = 2")])
        self.assertEqual(len(problems), 1)
        self.assertIn("not found", problems[0])

    def test_sequential_chain_locates(self):
        # Edit 2 targets text produced by edit 1's replace (applied in order):
        # the simulation must not false-flag it.
        files = {"src/a.py": "x = 1\n"}
        e1 = edit("src/a.py", "x = 1", "x = 2")
        e2 = edit("src/a.py", "x = 2", "x = 3")
        self.assertEqual(preflight_edits(files, [e1, e2]), [])

    def test_stops_at_first_problem(self):
        problems = preflight_edits(
            FILES, [edit("src/a.py", "w = 0"), edit("src/b.py", "z = 3")]
        )
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
