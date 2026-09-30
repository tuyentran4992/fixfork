"""Unit tests for the live Token Factory router pieces (no network)."""

import os
import unittest
from unittest import mock

from fixfork.model_router import (
    MODEL_IDS,
    EmptyModelReply,
    NebiusRouter,
    ReasoningBudgetExhausted,
    RouterError,
    estimate_cost,
    parse_chat_completion,
    resolve_model,
)


def _body(
    content: str | None = "[...]",
    finish: str = "stop",
    prompt: int = 1000,
    completion: int = 2000,
    reasoning: str | None = None,
):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


class ParseChatCompletionTest(unittest.TestCase):
    def test_happy_path_and_cost(self):
        reply = parse_chat_completion(_body(content="hello"), role="loop")
        self.assertEqual(reply.text, "hello")
        self.assertEqual(reply.tokens_used, 3000)
        # 1000 in @0.06/M + 2000 out @0.24/M = 0.00006 + 0.00048
        self.assertAlmostEqual(reply.cost_usd, 0.00054, places=9)

    def test_reason_cost_table(self):
        reply = parse_chat_completion(
            _body(prompt=1_000_000, completion=1_000_000), role="reason"
        )
        self.assertAlmostEqual(reply.cost_usd, 1.20, places=9)

    def test_none_content_length_raises_budget_exhausted(self):
        with self.assertRaises(ReasoningBudgetExhausted):
            parse_chat_completion(_body(content=None, finish="length"), role="loop")

    def test_partial_content_length_raises_budget_exhausted(self):
        # observed live 2026-09-30 (tomlkit race): a 3002-char reply truncated
        # mid-string passed as "successful", then the whole run died at
        # hypothesis parsing with 0 branches. Partial text + finish_reason=
        # "length" must be retryable at the router level, same as the empty case.
        with self.assertRaises(ReasoningBudgetExhausted):
            parse_chat_completion(
                _body(content='[{"title": "truncated mid-', finish="length"),
                role="reason",
            )

    def test_none_content_with_reasoning(self):
        with self.assertRaises(RouterError) as ctx:
            parse_chat_completion(
                _body(content=None, finish="stop", reasoning="thinking..."), role="loop"
            )
        self.assertIn("reasoning", str(ctx.exception))

    def test_whitespace_only_content_raises_empty_reply(self):
        # observed live 2026-09-29: content="\n" must be a retryable error, not
        # a "successful" reply that dies later inside JSON parsing
        with self.assertRaises(EmptyModelReply):
            parse_chat_completion(_body(content="\n"), role="loop")
        with self.assertRaises(EmptyModelReply):
            parse_chat_completion(_body(content="   \n  "), role="loop")

    def test_message_not_an_object(self):
        body = {"choices": [{"finish_reason": "stop", "message": ["not", "a", "dict"]}]}
        with self.assertRaises(RouterError):
            parse_chat_completion(body, role="loop")

    def test_garbage_body(self):
        with self.assertRaises(RouterError):
            parse_chat_completion({"error": "boom"}, role="loop")


class RetryBudgetTest(unittest.TestCase):
    def test_retries_with_doubled_budget(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=400)
        bodies = [
            _body(content=None, finish="length"),
            _body(content=None, finish="length"),
            _body(content="ok"),
        ]
        calls: list[int] = []

        def fake_call(role, prompt, max_tokens):
            calls.append(max_tokens)
            return bodies.pop(0)

        with mock.patch.object(router, "_call", side_effect=fake_call):
            reply = router.complete("loop", "p")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(calls, [100, 200, 400])

    def test_partial_answer_length_retries_with_doubled_budget(self):
        # a cut-off (partial) answer followed by finish_reason="length" must
        # trigger the same doubling ladder as the empty-content case. The
        # second fixture ([{}] + length) is deliberate: a length-stop is
        # retried even when the visible text looks complete - see the guard
        # comment in parse_chat_completion.
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=400)
        bodies = [
            _body(content='[{"cut', finish="length"),
            _body(content="[{}]", finish="length"),
            _body(content="[{}]"),
        ]
        calls: list[int] = []

        def fake_call(role, prompt, max_tokens):
            calls.append(max_tokens)
            return bodies.pop(0)

        with mock.patch.object(router, "_call", side_effect=fake_call):
            reply = router.complete("reason", "p")
        self.assertEqual(reply.text, "[{}]")
        self.assertEqual(calls, [100, 200, 400])

    def test_gives_up_at_cap(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=200)
        with mock.patch.object(
            router, "_call", return_value=_body(content=None, finish="length")
        ):
            with self.assertRaises(ReasoningBudgetExhausted):
                router.complete("loop", "p")

    def test_empty_reply_retried_with_same_budget(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=400)
        bodies = [_body(content="\n"), _body(content="ok")]
        calls: list[int] = []

        def fake_call(role, prompt, max_tokens):
            calls.append(max_tokens)
            return bodies.pop(0)

        with mock.patch.object(router, "_call", side_effect=fake_call):
            reply = router.complete("loop", "p")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(calls, [100, 100])  # doubling does not help emptiness

    def test_empty_reply_gives_up_after_max_attempts(self):
        router = NebiusRouter(
            api_key="test", max_tokens=100, max_tokens_cap=400, max_attempts=3
        )
        calls: list[int] = []

        def fake_call(role, prompt, max_tokens):
            calls.append(max_tokens)
            return _body(content="   ")

        with mock.patch.object(router, "_call", side_effect=fake_call):
            with self.assertRaises(EmptyModelReply):
                router.complete("loop", "p")
        self.assertEqual(len(calls), 3)  # bounded: no endless money loop

    def test_spent_usd_counts_failed_attempts(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=200)
        with mock.patch.object(
            router,
            "_call",
            return_value=_body(content=None, finish="length", prompt=1000, completion=2000),
        ):
            with self.assertRaises(ReasoningBudgetExhausted):
                router.complete("loop", "p")
        # two attempts (100 then 200 budget) burned real tokens; the report
        # must not pretend they were free
        per_attempt = (1000 * 0.06 + 2000 * 0.24) / 1_000_000
        self.assertAlmostEqual(router.spent_usd, 2 * per_attempt, places=9)


class AttemptLogTest(unittest.TestCase):
    """Per-call attempt log: retries are OBSERVABLE, not inferred from spend.

    (Blind spot found live 2026-09-30: a race retried calls and only the
    spend delta revealed it - no record said which call failed or why.)
    """

    def test_attempts_record_each_ladder_step(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=400)
        bodies = [_body(content=None, finish="length"), _body(content="ok")]
        calls: list[int] = []

        def fake_call(role, prompt, max_tokens):
            calls.append(max_tokens)
            return bodies.pop(0)

        with mock.patch.object(router, "_call", side_effect=fake_call):
            router.complete("loop", "p")
        self.assertEqual(calls, [100, 200])
        log = router.attempts
        self.assertEqual([e["outcome"] for e in log], ["budget_exhausted", "ok"])
        self.assertEqual([e["attempt"] for e in log], [1, 2])
        self.assertEqual([e["max_tokens"] for e in log], [100, 200])
        self.assertEqual(log[0]["finish_reason"], "length")
        self.assertEqual(log[1]["finish_reason"], "stop")
        self.assertEqual(router.retries, 1)
        self.assertGreater(log[0]["cost_usd"], 0)  # failed call burned tokens
        # the log explains every cent: spend reconciles with the records
        self.assertAlmostEqual(
            router.spent_usd, sum(e["cost_usd"] for e in log), places=9
        )

    def test_attempts_record_give_up_at_cap(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=200)
        with mock.patch.object(
            router, "_call", return_value=_body(content=None, finish="length")
        ):
            with self.assertRaises(ReasoningBudgetExhausted):
                router.complete("loop", "p")
        self.assertEqual(len(router.attempts), 2)
        self.assertTrue(all(e["outcome"] == "budget_exhausted" for e in router.attempts))
        self.assertEqual(router.retries, 1)  # 2nd call was a retry; no retry after it
        self.assertAlmostEqual(
            router.spent_usd, sum(e["cost_usd"] for e in router.attempts), places=9
        )

    def test_attempts_record_empty_reply_same_budget(self):
        router = NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=400)
        bodies = [_body(content="\n"), _body(content="ok")]
        with mock.patch.object(
            router, "_call", side_effect=lambda r, p, m: bodies.pop(0)
        ):
            router.complete("loop", "p")
        self.assertEqual(
            [(e["outcome"], e["max_tokens"]) for e in router.attempts],
            [("empty_reply", 100), ("ok", 100)],
        )
        self.assertEqual(router.retries, 1)

    def test_fatal_reply_is_logged_and_billed(self):
        # behaviour change 2026-09-30: a non-retryable parse failure
        # (content_filter) used to propagate WITHOUT its usage being billed;
        # now every response with usage counts, so the attempt log and
        # spent_usd reconcile even on a fatal path.
        router = NebiusRouter(api_key="test")
        with mock.patch.object(
            router, "_call", return_value=_body(content=None, finish="content_filter")
        ):
            with self.assertRaises(RouterError):
                router.complete("loop", "p")
        self.assertEqual(len(router.attempts), 1)
        self.assertEqual(router.attempts[0]["outcome"], "fatal")
        self.assertEqual(router.retries, 0)
        self.assertGreater(router.spent_usd, 0)
        self.assertAlmostEqual(
            router.spent_usd, router.attempts[0]["cost_usd"], places=9
        )

    def test_transport_failure_is_logged_without_cost(self):
        router = NebiusRouter(api_key="test")

        def boom(role, prompt, max_tokens):
            raise RouterError("Token Factory call failed: network down")

        with mock.patch.object(router, "_call", side_effect=boom):
            with self.assertRaises(RouterError):
                router.complete("loop", "p")
        self.assertEqual(len(router.attempts), 1)
        self.assertEqual(router.attempts[0]["outcome"], "call_failed")
        self.assertIn("network down", router.attempts[0]["error"])
        self.assertEqual(router.spent_usd, 0.0)  # nothing was billed

    def test_non_dict_body_is_fatal_not_crash(self):
        # regression guard (cross-check 30/09): a non-dict body (e.g. [] from
        # a broken proxy) must surface as RouterError with a fatal record -
        # not as AttributeError from the billing/log helpers
        router = NebiusRouter(api_key="test")
        with mock.patch.object(router, "_call", return_value=[]):
            with self.assertRaises(RouterError):
                router.complete("loop", "p")
        self.assertEqual(len(router.attempts), 1)
        self.assertEqual(router.attempts[0]["outcome"], "fatal")
        self.assertEqual(router.spent_usd, 0.0)


class RouterConfigGuardTest(unittest.TestCase):
    """Cross-check fixes (qwen3.8-max, 2026-09-29): nonsense limits must be
    rejected up front, and the doubling path must respect the attempt bound."""

    def test_invalid_config_rejected(self):
        with self.assertRaises(RouterError):
            NebiusRouter(api_key="test", max_tokens=0)
        with self.assertRaises(RouterError):
            NebiusRouter(api_key="test", max_tokens=100, max_tokens_cap=50)
        with self.assertRaises(RouterError):
            NebiusRouter(api_key="test", max_attempts=0)

    def test_doubling_path_bounded_by_max_attempts(self):
        router = NebiusRouter(
            api_key="test", max_tokens=100, max_tokens_cap=12800, max_attempts=3
        )
        calls: list[int] = []

        def fake_call(role, prompt, max_tokens):
            calls.append(max_tokens)
            return _body(content=None, finish="length")

        with mock.patch.object(router, "_call", side_effect=fake_call):
            with self.assertRaises(ReasoningBudgetExhausted):
                router.complete("loop", "p")
        # the cap still had room (12800), yet the ladder stopped at attempt 3
        self.assertEqual(calls, [100, 200, 400])


class UsageGuardTest(unittest.TestCase):
    def test_garbage_usage_counts_as_zero_not_crash(self):
        body = _body(content="hello")
        body["usage"] = {"prompt_tokens": "abc", "completion_tokens": None}
        reply = parse_chat_completion(body, role="loop")
        self.assertEqual(reply.text, "hello")
        self.assertEqual(reply.tokens_used, 0)
        body["usage"] = "not a dict"
        reply = parse_chat_completion(body, role="loop")
        self.assertEqual(reply.tokens_used, 0)

    def test_content_filter_fails_fast(self):
        with self.assertRaises(RouterError) as ctx:
            parse_chat_completion(
                _body(content=None, finish="content_filter"), role="loop"
            )
        self.assertNotIsInstance(ctx.exception, EmptyModelReply)
        self.assertIn("content_filter", str(ctx.exception))


class ModelSlugTest(unittest.TestCase):
    def test_slugs_are_the_verified_live_ones(self):
        # exact strings from GET /v1/models on 2026-09-27 (25-model catalog)
        self.assertEqual(MODEL_IDS["reason"], "nvidia/nemotron-3-super-120b-a12b")
        self.assertEqual(MODEL_IDS["loop"], "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"FIXFORK_LOOP_MODEL": "test/model"}):
            self.assertEqual(resolve_model("loop"), "test/model")
            self.assertEqual(resolve_model("reason"), MODEL_IDS["reason"])

    def test_estimate_cost_unknown_role(self):
        self.assertEqual(estimate_cost("nope", 10, 10), 0.0)


if __name__ == "__main__":
    unittest.main()
