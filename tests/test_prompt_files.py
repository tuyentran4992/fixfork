"""Tests for prompt building with repository files."""

import unittest

from fixfork.hypotheses import (
    build_prompt,
    defect_scan,
    extract_refs,
    render_files,
    resolve_type_context,
    type_context_note,
)


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


class ResolveTypeContextTest(unittest.TestCase):
    """Definition files for names imported by the failure-referenced files.

    Live evidence (2026-10-05, mcp-atlassian #1578): the fix model guessed
    object types (dict access on a pydantic model) because the defining class
    never reached the prompt. These tests pin the bounded resolver that adds
    those definitions to the render set.
    """

    def test_relative_import_resolves_to_defining_module(self):
        files = {
            "pkg/mod.py": "from .helper import helper_fn\n",
            "pkg/helper.py": "def helper_fn():\n    return 1\n",
        }
        self.assertEqual(resolve_type_context(files, ["pkg/mod.py"]), ["pkg/helper.py"])

    def test_parent_relative_import_resolves(self):
        files = {
            "pkg/sub/app.py": "from ..lib.util import Widget\n",
            "pkg/lib/util.py": "class Widget:\n    pass\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["pkg/sub/app.py"]), ["pkg/lib/util.py"]
        )

    def test_dotted_module_inside_relative(self):
        files = {
            "pkg/mod.py": "from .sub.deep import Deep\n",
            "pkg/sub/deep.py": "class Deep:\n    pass\n",
        }
        self.assertEqual(resolve_type_context(files, ["pkg/mod.py"]), ["pkg/sub/deep.py"])

    def test_absolute_import_matches_src_layout(self):
        files = {
            "src/pk/models/__init__.py": "from .thing import Thing\n",
            "src/pk/models/thing.py": "class Thing:\n    pass\n",
            "src/pk/app.py": "from pk.models import Thing\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["src/pk/app.py"]),
            ["src/pk/models/thing.py"],
        )

    def test_from_dot_import_submodule(self):
        files = {
            "pkg/__init__.py": "from . import helper\n",
            "pkg/helper.py": "x = 1\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["pkg/__init__.py"]), ["pkg/helper.py"]
        )

    def test_stdlib_and_unresolved_names_are_skipped(self):
        files = {"a.py": "import os\nfrom typing import Any\nfrom .missing import Nope\n"}
        self.assertEqual(resolve_type_context(files, ["a.py"]), [])

    def test_refs_are_never_offered_as_type_context(self):
        files = {
            "pkg/mod.py": "from .helper import helper_fn\n",
            "pkg/helper.py": "def helper_fn():\n    return 1\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["pkg/mod.py", "pkg/helper.py"]), []
        )

    def test_order_is_source_order_and_deduped(self):
        files = {
            "pkg/mod.py": "from .a import A\nfrom .b import B\nfrom .a import A2\n",
            "pkg/a.py": "def A():\n    pass\n\n\ndef A2():\n    pass\n",
            "pkg/b.py": "def B():\n    pass\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["pkg/mod.py"]), ["pkg/a.py", "pkg/b.py"]
        )

    def test_cap_is_honoured(self):
        files = {
            "pkg/mod.py": "from .m1 import X1\nfrom .m2 import X2\nfrom .m3 import X3\n",
            "pkg/m1.py": "X1 = 1\n",
            "pkg/m2.py": "X2 = 2\n",
            "pkg/m3.py": "X3 = 3\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["pkg/mod.py"], max_files=2),
            ["pkg/m1.py", "pkg/m2.py"],
        )

    def test_package_re_export_resolves_to_leaf(self):
        # ``from pkg import Re`` where pkg/__init__ re-exports: the package
        # scan must reach the leaf that actually defines the name.
        files = {
            "pkg/__init__.py": "from .leaf import Re\n",
            "pkg/mod.py": "from pkg import Re\n",
            "pkg/leaf.py": "class Re:\n    pass\n",
        }
        self.assertEqual(
            resolve_type_context(files, ["pkg/mod.py"]), ["pkg/leaf.py"]
        )

    def test_non_python_refs_ignored(self):
        self.assertEqual(resolve_type_context({"notes.md": "x"}, ["notes.md"]), [])


class TypeContextNoteTest(unittest.TestCase):
    def test_empty_when_nothing_rendered(self):
        self.assertEqual(type_context_note(["a.py"], "--- b.py ---\nx\n"), "")

    def test_lists_only_files_actually_shown(self):
        rendered = "--- a.py ---\nA\n"
        note = type_context_note(["a.py", "b.py"], rendered)
        self.assertIn("a.py", note)
        self.assertNotIn("b.py", note)
        self.assertTrue(note.startswith("\n(type context above: "))

    def test_inline_marker_mention_is_not_counted(self):
        # Soi chéo 05/10: a mere inline mention of `--- b.py ---` inside some
        # file's content is not evidence that b.py was rendered.
        rendered = 'x = "--- b.py ---"\n'
        self.assertEqual(type_context_note(["b.py"], rendered), "")


class RenderFilesTypeContextTest(unittest.TestCase):
    def test_type_context_ranks_after_refs_before_other_code(self):
        files = {"r.py": "R" * 30, "t.py": "T" * 30, "m.py": "M" * 30}
        out = render_files(
            files,
            max_file_chars=100,
            max_total_chars=100,
            refs=["r.py"],
            type_context=["t.py"],
        )
        self.assertIn("R" * 30, out)
        self.assertIn("T" * 30, out)
        self.assertLess(out.index("r.py"), out.index("t.py"))
        # the ordinary code file is squeezed out and named in the note
        self.assertNotIn("MMM", out)
        self.assertIn("m.py", out)


class BuildPromptTypeContextTest(unittest.TestCase):
    def test_prompt_includes_type_files_and_note(self):
        files = {
            "pkg/app.py": "from .helpers import Widget\n",
            "pkg/helpers.py": "class Widget:\n    pass\n",
        }
        prompt = build_prompt(
            "repo",
            "pytest",
            "log",
            n=1,
            files=files,
            refs=["pkg/app.py"],
            type_context=["pkg/helpers.py"],
            occurrences_block="",
        )
        self.assertIn("--- pkg/helpers.py ---", prompt)
        self.assertIn("class Widget", prompt)
        self.assertIn("(type context above: pkg/helpers.py", prompt)

    def test_no_note_without_type_context(self):
        prompt = build_prompt("repo", "pytest", "log", n=1, occurrences_block="")
        self.assertNotIn("type context above", prompt)


class GroundingRulesTest(unittest.TestCase):
    """The measured type-blind failure must leave a hard rule in both prompts.

    Live evidence (2026-10-05, mcp-atlassian #1578): fixes called .get() on a
    JiraIssue object and one 'verified' by writing the expected value into the
    returned object; both prompt stages must forbid that explicitly.
    """

    def test_diagnosis_prompt_carries_grounding_rules(self):
        prompt = build_prompt("repo", "pytest", "log", n=3)
        flat = " ".join(prompt.split())
        self.assertIn("Ground every call you write in evidence", flat)
        self.assertIn("Never call `.get(...)`", flat)
        self.assertIn("fabricated, not verified", flat)
        self.assertIn("must cover every exercised site", flat)
        self.assertIn("are the referee", flat)

    def test_loop_prompt_carries_grounding_rules(self):
        from fixfork.pipeline import LOOP_PROMPT_TEMPLATE

        text = " ".join(
            LOOP_PROMPT_TEMPLATE.format(
                title="t", test_command="tc", files="F", log="L"
            ).split()
        )
        self.assertIn("Ground the follow-up in the evidence", text)
        self.assertIn("never assume dict access", text)
        self.assertIn("must read state the system produced", text)


if __name__ == "__main__":
    unittest.main()
