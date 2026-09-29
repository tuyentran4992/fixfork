"""Tests for hypothesis parsing, validation and divergence checks."""

import json
import unittest

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.hypotheses import (
    HypothesisError,
    generate_hypotheses,
    parse_edit_object,
    parse_hypotheses,
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
        with self.assertRaises(HypothesisError):
            _parse_loop_edits('{"edits": [}')

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


if __name__ == "__main__":
    unittest.main()
