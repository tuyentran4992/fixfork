"""Tests for prompt building with repository files."""

import unittest

from fixfork.hypotheses import build_prompt, defect_scan, extract_refs, render_files


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

    def test_prompt_carries_hypothesis_completeness_rule(self):
        # Live evidence (tomlkit #619, 2026-09-30): the model split ONE
        # coordinated fix into three partial hypotheses and every branch went
        # red; the prompt must demand a complete fix per hypothesis.
        prompt = build_prompt("repo", "pytest", "log", n=3)
        flat = " ".join(prompt.split())  # template wraps lines; compare flat
        self.assertIn("a COMPLETE fix for its own explanation", flat)
        self.assertIn("all of those edits belong in the SAME hypothesis", flat)
        self.assertIn("and only the edits that its own explanation needs", flat)
        self.assertIn("Do not split one explanation's edits", flat)

    def test_refs_fallback_still_upgrades_small_defaults(self):
        # Fallback mode (no defect signature): log-referenced files get the
        # larger fallback budget when the caller kept the small defaults.
        files = {"ref.py": "r" * 20000, "huge.py": "h" * 50000}
        prompt = build_prompt(
            "repo",
            "pytest",
            "log",
            n=1,
            files=files,
            refs=["ref.py", "huge.py"],
            occurrences_block="",
            refs_full=True,
        )
        self.assertIn("--- ref.py ---", prompt)  # 20k fits the 48k fallback
        self.assertNotIn("--- huge.py ---", prompt)  # 50k still over it
        # The skip reason must be the per-file cap, not the total budget.
        self.assertIn(
            "huge.py (referenced by the log; 50000 chars over the fallback "
            "per-file budget)",
            prompt,
        )

    def test_refs_fallback_raises_total_to_fit_file_budget(self):
        # Raising ONLY --max-file-chars must be enough: the fallback total is
        # coupled to the per-file value, so a file the caller made room for
        # cannot be dropped by a total cap they did not touch (soi chéo 02/10).
        big = "x" * 74167
        files = {"tomlkit/items.py": big}
        log = "assert 0 == 1\n  where 0 = Time(12, 34, 56).fold"
        scan = defect_scan(log, files, ["tomlkit/items.py"])
        prompt = build_prompt(
            "repo",
            "pytest",
            log,
            n=3,
            files=files,
            refs=["tomlkit/items.py"],
            max_file_chars=100000,
            max_total_chars=24000,  # default total, untouched by the caller
            occurrences_block=scan.block,
            refs_full=scan.fallback,
        )
        self.assertIn("--- tomlkit/items.py ---", prompt)
        self.assertIn(big, prompt)

    def test_refs_fallback_respects_raised_user_budget(self):
        # Pins the rule that a raised user budget is never downgraded by the
        # fallback (found while preparing the tomlkit #619 re-run; the
        # referenced-file scenario here is synthesized to pin the behavior).
        big = "x" * 74167
        files = {"tomlkit/items.py": big}
        log = "assert 0 == 1\n  where 0 = Time(12, 34, 56).fold"
        scan = defect_scan(log, files, ["tomlkit/items.py"])
        self.assertTrue(scan.fallback)
        prompt = build_prompt(
            "repo",
            "pytest",
            log,
            n=3,
            files=files,
            refs=["tomlkit/items.py"],
            max_file_chars=100000,
            max_total_chars=300000,
            occurrences_block=scan.block,
            refs_full=scan.fallback,
        )
        self.assertIn("--- tomlkit/items.py ---", prompt)
        self.assertIn(big, prompt)


if __name__ == "__main__":
    unittest.main()
