"""Tests for prompt building with repository files."""

import unittest

from fixfork.hypotheses import build_prompt, extract_refs, render_files


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


class ExtractRefsTest(unittest.TestCase):
    def test_traceback_path_maps_to_repo_key(self):
        # Sandbox runs happen in temp dirs: the log shows an absolute path and
        # the repo key must still be recognised via path-boundary suffix match.
        files = {"tests/test_items.py": "x", "tomlkit/items.py": "y"}
        log = 'File "/tmp/ff-abc/branch-1/tests/test_items.py", line 811, in test_fold\n'
        self.assertEqual(extract_refs(log, files), ["tests/test_items.py"])

    def test_colon_style_paths_kept_in_order_of_mention(self):
        files = {"tomlkit/items.py": "y", "tests/test_items.py": "x"}
        log = "tests/test_items.py:811: TypeError\ntomlkit/items.py:1200: TypeError\n"
        self.assertEqual(extract_refs(log, files), ["tests/test_items.py", "tomlkit/items.py"])

    def test_unknown_paths_are_ignored(self):
        files = {"tests/test_items.py": "x"}
        log = (
            'File "/usr/lib/python3.12/os.py", line 5\n'
            'File "/tmp/ff/tests/test_items.py", line 1\n'
        )
        self.assertEqual(extract_refs(log, files), ["tests/test_items.py"])

    def test_dot_slash_and_windows_separators_normalised(self):
        files = {"tests/test_items.py": "x"}
        log = "File \".\\tests\\test_items.py\", line 3\n"
        self.assertEqual(extract_refs(log, files), ["tests/test_items.py"])

    def test_longest_suffix_match_wins(self):
        # A short key that also endswith-matches must not shadow the longer,
        # correct key: the log line refers to src/utils.py, not utils.py.
        files = {"utils.py": "a", "src/utils.py": "b"}
        log = 'File "/tmp/ff/src/utils.py", line 9, in f\n'
        self.assertEqual(extract_refs(log, files), ["src/utils.py"])

    def test_empty_inputs(self):
        self.assertEqual(extract_refs("", {"a.py": ""}), [])
        self.assertEqual(extract_refs("some log", {}), [])


class RenderFilesRefsTest(unittest.TestCase):
    def test_ref_file_beats_alphabetical_order_under_tight_budget(self):
        files = {"aaa.py": "A" * 30, "zzz/tests/test_x.py": "B" * 30}
        out = render_files(
            files, max_file_chars=100, max_total_chars=80, refs=["zzz/tests/test_x.py"]
        )
        self.assertIn("--- zzz/tests/test_x.py ---", out)
        self.assertLess(out.index("zzz/tests/test_x.py"), out.index("aaa.py"))
        # the ref file was rendered; the alphabetically-earlier file was cut
        self.assertIn("B" * 30, out)
        self.assertIn("not shown", out)
        self.assertNotIn("AAA", out)

    def test_no_refs_keeps_previous_ordering(self):
        out = render_files({"b.py": "2", "a.py": "1"})
        self.assertLess(out.index("a.py"), out.index("b.py"))


class BuildPromptTest(unittest.TestCase):
    def test_files_land_in_prompt(self):
        prompt = build_prompt("repo", "pytest", "log", n=3, files={"src/tax.py": "X = 1\n"})
        self.assertIn("--- src/tax.py ---", prompt)
        self.assertIn("X = 1", prompt)

    def test_prompt_without_files_still_formats(self):
        prompt = build_prompt("repo", "pytest", "log")
        self.assertIn("(no repository files provided)", prompt)

    def test_refs_and_caps_reach_render_files(self):
        # A 50-char file is over the 10-char cap: it must appear only in the
        # skipped note, while the ref file is rendered in full.
        prompt = build_prompt(
            "repo",
            "pytest",
            "log",
            n=1,
            files={"big.py": "z" * 50, "ref.py": "r" * 5},
            refs=["ref.py"],
            max_file_chars=10,
            max_total_chars=100,
        )
        self.assertIn("--- ref.py ---", prompt)
        self.assertNotIn("zzz", prompt)
        self.assertIn("big.py", prompt)


if __name__ == "__main__":
    unittest.main()
