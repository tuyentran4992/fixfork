"""Tests for hypothesis parsing, validation and divergence checks."""

import json
import unittest
from pathlib import Path

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.hypotheses import (
    HypothesisError,
    PARSE_FAILURE,
    generate_hypotheses,
    loads_json_tolerant,
    parse_edit_object,
    parse_hypotheses,
    repair_json_text,
)
from fixfork.pipeline import _parse_loop_edits


class ParseHypothesesTest(unittest.TestCase):
    def test_valid_demo_reply(self):
        hypotheses = parse_hypotheses(json.dumps(DEMO_HYPOTHESES), n=3)
        self.assertEqual([h.id for h in hypotheses], [1, 2, 3])
        self.assertEqual(hypotheses[0].edits[0].file, "src/tax.py")

    def test_json_wrapped_in_prose_and_fences(self):
        reply = "Sure, here you go:\n```json\n" + json.dumps(DEMO_HYPOTHESES) + "\n```\n"
        hypotheses = parse_hypotheses(reply, n=3)
        self.assertEqual(len(hypotheses), 3)

    def test_wrong_count_rejected(self):
        with self.assertRaises(HypothesisError):
            parse_hypotheses(json.dumps(DEMO_HYPOTHESES[:2]), n=3)

    def test_non_json_rejected(self):
        with self.assertRaises(HypothesisError):
            parse_hypotheses("I could not find a bug, sorry.", n=3)

    def test_duplicate_titles_rejected(self):
        items = [dict(item) for item in DEMO_HYPOTHESES[:2]]
        items[1] = dict(items[1], title=items[0]["title"])
        with self.assertRaises(HypothesisError):
            parse_hypotheses(json.dumps(items), n=2)

    def test_identical_edit_targets_rejected(self):
        edit = DEMO_HYPOTHESES[0]["edits"][0]
        items = [
            {"title": "One theory", "rationale": "a", "edits": [edit]},
            {"title": "Another theory", "rationale": "b", "edits": [dict(edit)]},
        ]
        with self.assertRaises(HypothesisError):
            parse_hypotheses(json.dumps(items), n=2)

    def test_missing_edits_rejected(self):
        items = [
            {"title": "No edits", "rationale": "a", "edits": []},
            {"title": "Fine", "rationale": "b", "edits": [DEMO_HYPOTHESES[0]["edits"][0]]},
        ]
        with self.assertRaises(HypothesisError):
            parse_hypotheses(json.dumps(items), n=2)

    def test_noop_edit_rejected(self):
        edit = {"file": "src/tax.py", "find": "x = 1", "replace": "x = 1"}
        items = [
            {"title": "Noop", "rationale": "a", "edits": [edit]},
            {"title": "Fine", "rationale": "b", "edits": [DEMO_HYPOTHESES[0]["edits"][0]]},
        ]
        with self.assertRaises(HypothesisError):
            parse_hypotheses(json.dumps(items), n=2)

    def test_non_string_fields_rejected_not_coerced(self):
        # str() coercion used to turn None into the literal word "None" written
        # into a source file; non-strings are broken replies now
        good = DEMO_HYPOTHESES[0]["edits"][0]
        for bad_item in (
            {"title": 123, "rationale": "a", "edits": [good]},
            {"title": "T", "rationale": ["not", "text"], "edits": [good]},
            {"title": "T", "rationale": "a", "edits": [{"file": "src/tax.py", "find": None, "replace": "x"}]},
            {"title": "T", "rationale": "a", "edits": [{"file": "src/tax.py", "find": "x", "replace": None}]},
        ):
            partner = {"title": "Fine", "rationale": "b", "edits": [dict(good)]}
            with self.assertRaises(HypothesisError):
                parse_hypotheses(json.dumps([bad_item, partner]), n=2)


class ParseEditObjectTest(unittest.TestCase):
    def test_file_stripped_find_kept_byte_exact(self):
        edit = parse_edit_object(
            {"file": " src/tax.py ", "find": " x = 1\n", "replace": "y"}, "test"
        )
        self.assertEqual(edit.file, "src/tax.py")
        self.assertEqual(edit.find, " x = 1\n")

    def test_empty_replace_allowed_but_non_string_rejected(self):
        edit = parse_edit_object({"file": "a.py", "find": "x = 1", "replace": ""}, "t")
        self.assertEqual(edit.replace, "")
        with self.assertRaises(HypothesisError):
            parse_edit_object({"file": "a.py", "find": "x = 1", "replace": 5}, "t")


class LoopReplyParseTest(unittest.TestCase):
    """The in-branch iteration reply must fail as HypothesisError, never as an
    uncaught JSONDecodeError/AttributeError that crashes the whole run."""

    def test_invalid_json_raises_hypothesis_error(self):
        # deliberately UNREPAIRABLE (bare token, no closing possible): the loop
        # reply path must still fail as HypothesisError, never as an uncaught
        # JSONDecodeError - near-valid JSON now goes through the bounded
        # repair pass (see JsonRepairTest).
        with self.assertRaises(HypothesisError):
            _parse_loop_edits('{"edits": [oops')

    def test_near_miss_bracket_repaired_to_empty_edits(self):
        # '{"edits": [}' is a near-miss for an empty edits list: the repair
        # pass fixes it and the loop then simply stops (no more edits).
        self.assertEqual(_parse_loop_edits('{"edits": [}'), [])

    def test_edits_not_list_rejected(self):
        with self.assertRaises(HypothesisError):
            _parse_loop_edits('{"edits": "nope"}')

    def test_bad_edit_object_rejected(self):
        with self.assertRaises(HypothesisError):
            _parse_loop_edits('{"edits": [{"file": "a.py", "find": 3, "replace": "y"}]}')

    def test_valid_loop_reply_parsed(self):
        edits = _parse_loop_edits('{"edits": [{"file": "a.py", "find": "x", "replace": "y"}]}')
        self.assertEqual(len(edits), 1)

    def test_empty_reply_prose_rejected(self):
        with self.assertRaises(HypothesisError):
            _parse_loop_edits("I have no further idea.")


class GenerateHypothesesTest(unittest.TestCase):
    def test_fake_router_round_trip(self):
        router = FakeRouter()
        hypotheses, reply = generate_hypotheses(
            router,
            "examples/demo-repo",
            "python3 -m unittest",
            "boom",
            n=3,
            files={"src/tax.py": "x = 1\n"},
        )
        self.assertEqual(len(hypotheses), 3)
        self.assertEqual(reply.tokens_used, 0)
        self.assertEqual(router.calls[0][0], "reason")
        self.assertIn("--- src/tax.py ---", router.calls[0][1])

    def test_refs_are_derived_from_log_and_prioritised(self):
        # When refs are not passed explicitly, the helper derives them from the
        # log itself - the two call paths (helper + pipeline) must not drift.
        router = FakeRouter()
        generate_hypotheses(
            router,
            "repo",
            "pytest",
            'File "/tmp/ff/branch-1/tests/test_items.py", line 811, in t\n',
            n=3,
            files={"aaa.py": "print(1)\n", "tests/test_items.py": "assert i.fold == 1\n"},
        )
        prompt = router.calls[0][1]
        self.assertLess(
            prompt.index("--- tests/test_items.py ---"),
            prompt.index("--- aaa.py ---"),
        )


class JsonRepairTest(unittest.TestCase):
    """Near-valid model JSON is repaired (bounded) instead of aborting the run.

    Live evidence: 2026-10-01 greynoise run (see
    data/greynoise-reply-2026-10-01.txt) - a 3-hypothesis reply with one
    missing "]" mid-stream aborted the run after ~$0.006 while parsing was
    strict.
    """

    def _broken_mid_stream(self) -> str:
        items = DEMO_HYPOTHESES
        second = json.dumps(items[1])
        # drop the "]" that closes the second object's "edits" array
        broken_second = second[:-2] + second[-1]
        return (
            "["
            + json.dumps(items[0])
            + ",\n"
            + broken_second
            + ",\n"
            + json.dumps(items[2])
            + "]"
        )

    def test_missing_bracket_mid_stream_is_repaired(self):
        notes: list[str] = []
        hypotheses = parse_hypotheses(self._broken_mid_stream(), n=3, notes=notes)
        self.assertEqual([h.id for h in hypotheses], [1, 2, 3])
        self.assertTrue(any("repaired" in note for note in notes))

    def test_trailing_comma_is_repaired(self):
        reply = json.dumps(DEMO_HYPOTHESES)[:-1] + ",]"
        hypotheses = parse_hypotheses(reply, n=3)
        self.assertEqual(len(hypotheses), 3)

    def test_missing_final_bracket_is_repaired(self):
        reply = json.dumps(DEMO_HYPOTHESES)[:-1]
        hypotheses = parse_hypotheses(reply, n=3)
        self.assertEqual(len(hypotheses), 3)

    def test_valid_json_is_not_touched(self):
        reply = json.dumps(DEMO_HYPOTHESES)
        repaired, fixes = repair_json_text(reply)
        self.assertEqual(fixes, 0)
        self.assertEqual(repaired, reply)

    def test_unrepairable_reply_still_raises(self):
        with self.assertRaises(HypothesisError):
            parse_hypotheses('[{"title": "A", "rationale', n=3)

    def test_truncated_inside_string_is_rejected_cleanly(self):
        # soi chéo 01/10 (aibox, on the repair patch): EOF while inside a
        # string literal must not be "closed off" - closers appended then
        # parse as string content, and the repair loop burns its whole budget
        # for nothing. Such a reply is budget-truncated: reject at once.
        repaired, fixes = repair_json_text('{"title": "A", "rationale')
        self.assertIsNone(repaired)
        self.assertEqual(fixes, 0)

    def test_truncated_after_dangling_escape_is_rejected_cleanly(self):
        # text ends with a lone backslash inside the string (pending escape)
        repaired, fixes = repair_json_text('["ab\\')
        self.assertIsNone(repaired)
        self.assertEqual(fixes, 0)

    def test_truncated_outside_string_is_still_closed(self):
        # regression guard for the legitimate cut-off case this fallback
        # exists for: EOF outside any string literal -> closers appended
        repaired, fixes = repair_json_text('[{"a": 1')
        self.assertEqual(repaired, '[{"a": 1}]')
        self.assertEqual(fixes, 1)

    def test_loop_edit_reply_with_trailing_comma_is_repaired(self):
        broken = '{"edits": [{"file": "src/tax.py", "find": "a", "replace": "b"},]}'
        edits = _parse_loop_edits(broken)
        self.assertEqual(len(edits), 1)

    def test_loop_repair_leaves_an_audit_note(self):
        # soi chéo 01/10 (aibox): loop-path repairs were applied silently; the
        # run report must say when a follow-up reply was repaired.
        broken = '{"edits": [{"file": "src/tax.py", "find": "a", "replace": "b"},]}'
        notes: list[str] = []
        edits = _parse_loop_edits(broken, notes=notes)
        self.assertEqual(len(edits), 1)
        self.assertTrue(any("repaired" in note for note in notes))

    def test_repaired_loop_reply_without_edits_still_rejected(self):
        # A repaired follow-up reply that still lacks the 'edits' list is
        # rejected, but the repair note stays: it is a fact about the reply,
        # and the parse_error lands next to it in loop_raw for context.
        notes: list[str] = []
        with self.assertRaises(HypothesisError):
            _parse_loop_edits('{"nope": [}', notes=notes)
        self.assertTrue(any("repaired" in note for note in notes))

    def test_json_null_is_not_a_parse_failure(self):
        # `null` is a VALID JSON parse; the failure marker must be distinct
        # from None (the old None sentinel conflated the two - soi chéo 01/10).
        value, fixes = loads_json_tolerant("null")
        self.assertIsNone(value)
        self.assertEqual(fixes, 0)
        value, _ = loads_json_tolerant("{not json at all")
        self.assertIs(value, PARSE_FAILURE)

    def test_real_captured_reply_is_repaired(self):
        reply = (
            Path(__file__).parent / "data" / "greynoise-reply-2026-10-01.txt"
        ).read_text(encoding="utf-8")
        notes: list[str] = []
        hypotheses = parse_hypotheses(reply, n=3, notes=notes)
        self.assertEqual(len(hypotheses), 3)
        self.assertTrue(all(h.edits for h in hypotheses))
        self.assertTrue(any("repaired" in note for note in notes))


class PythonLiteralFallbackTest(unittest.TestCase):
    """A structurally sound reply written as a Python literal parses.

    Live evidence (2026-10-01, greynoise run 3, fixfork dc5782a): every
    find/replace value used Python-style single quotes; strict JSON parsing
    aborted a live run whose reply already covered all 7 defect sites
    (data/greynoise-reply-run3-2026-10-01.txt).
    """

    def test_single_quoted_values_parse_as_one_repair_step(self):
        reply = (
            '[{"title": "t", "rationale": "r", "edits": [{"file": "a.py", '
            '"find": \'old\', "replace": \'new\'}]}]'
        )
        value, steps = loads_json_tolerant(reply)
        self.assertEqual(steps, 1)
        assert isinstance(value, list)
        self.assertEqual(value[0]["edits"][0]["find"], "old")

    def test_real_run3_reply_yields_full_site_coverage(self):
        reply = (
            Path(__file__).parent / "data" / "greynoise-reply-run3-2026-10-01.txt"
        ).read_text(encoding="utf-8")
        notes: list[str] = []
        hypotheses = parse_hypotheses(reply, n=3, notes=notes)
        self.assertEqual(len(hypotheses), 3)
        for hypothesis in hypotheses:
            # the machine scan listed 7 sites; the model covered all of them
            self.assertEqual(len(hypothesis.edits), 7)
        first = hypotheses[0].edits[0]
        self.assertEqual(
            first.find,
            '        if data["internet_scanner_intelligence"]["classification"]'
            ' == "benign":',
        )
        self.assertIn('.get("classification", "unknown")', first.replace)
        self.assertTrue(any("repaired" in note for note in notes))

    def test_python_literal_path_not_taken_for_prose(self):
        value, _ = loads_json_tolerant("definitely not json")
        self.assertIs(value, PARSE_FAILURE)


if __name__ == "__main__":
    unittest.main()
