"""Tests for prompt building with repository files."""

import unittest

from fixfork.hypotheses import build_prompt, render_files


class RenderFilesTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(render_files(None), "(no repository files provided)")

    def test_includes_content_sorted(self):
        out = render_files({"b.py": "print(2)", "a.py": "print(1)"})
        self.assertIn("--- a.py ---\nprint(1)", out)
        self.assertIn("--- b.py ---\nprint(2)", out)
        self.assertLess(out.index("a.py"), out.index("b.py"))

    def test_large_file_skipped_with_note(self):
        out = render_files({"big.py": "x" * 50}, max_file_chars=10)
        self.assertIn("not shown", out)
        self.assertIn("big.py", out)
        self.assertNotIn("xxxx", out)

    def test_total_budget_note(self):
        files = {f"f{i}.py": "y" * 10 for i in range(5)}
        out = render_files(files, max_file_chars=100, max_total_chars=40)
        self.assertIn("not shown", out)

    def test_code_files_take_budget_priority(self):
        # Real repos carry CI/docs/lockfiles that sort before src/; they must
        # not crowd the source files out of a tight budget.
        files = {"00-config.yml": "k" * 30, "src/app.py": "x" * 30}
        out = render_files(files, max_file_chars=100, max_total_chars=80)
        self.assertIn("--- src/app.py ---", out)
        self.assertIn("not shown", out)
        self.assertIn("00-config.yml", out)  # named in the skipped note
        self.assertNotIn("kkk", out)  # config content was not rendered

    def test_trailing_whitespace_preserved_exactly(self):
        # The prompt tells the model to copy `find` text exactly; the shown
        # content must not be stripped at the end of the file.
        out = render_files({"a.py": "x = 1\n\n"})
        self.assertEqual(out, "--- a.py ---\nx = 1\n\n\n")

    def test_skipped_note_is_bounded(self):
        # Many skipped files: the note must summarize instead of growing with
        # every skipped file.
        files = {f"f{i:03d}.txt": "z" * 40 for i in range(30)}
        out = render_files(files, max_file_chars=100, max_total_chars=1)
        self.assertIn("+22 more", out)
        self.assertLessEqual(len(out), 700)


class BuildPromptTest(unittest.TestCase):
    def test_files_land_in_prompt(self):
        prompt = build_prompt("repo", "pytest", "log", n=3, files={"src/tax.py": "X = 1\n"})
        self.assertIn("--- src/tax.py ---", prompt)
        self.assertIn("X = 1", prompt)

    def test_prompt_without_files_still_formats(self):
        prompt = build_prompt("repo", "pytest", "log")
        self.assertIn("(no repository files provided)", prompt)


if __name__ == "__main__":
    unittest.main()
