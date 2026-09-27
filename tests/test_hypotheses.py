"""Tests for hypothesis parsing, validation and divergence checks."""

import json
import unittest

from fixfork.fakes import DEMO_HYPOTHESES, FakeRouter
from fixfork.hypotheses import (
    HypothesisError,
    generate_hypotheses,
    parse_hypotheses,
)


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


class GenerateHypothesesTest(unittest.TestCase):
    def test_fake_router_round_trip(self):
        router = FakeRouter()
        hypotheses, tokens = generate_hypotheses(
            router, "examples/demo-repo", "python3 -m unittest", "boom", n=3
        )
        self.assertEqual(len(hypotheses), 3)
        self.assertEqual(tokens, 0)
        self.assertEqual(router.calls[0][0], "reason")


if __name__ == "__main__":
    unittest.main()
