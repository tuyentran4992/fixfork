"""Model routing for FixFork: Nemotron on Nebius Token Factory.

Two roles keep credits stretchy:
  * ``reason`` -> Nemotron 3 Super 120B ($0.30/M in, $0.90/M out): reads the
    repository files + failure log and forms the hypotheses.
  * ``loop``   -> Nemotron 3 Nano 30B ($0.06/M in, $0.24/M out): cheap extra
    iterations inside a branch.

The real backend is the OpenAI-compatible Token Factory API
(``https://api.tokenfactory.nebius.com/v1/``, key in env ``NEBIUS_API_KEY``).
``FakeRouter`` (see ``fakes.py``) keeps the pipeline runnable offline.

Verified live on 2026-09-27:
  * slugs checked against ``GET /v1/models`` (25 models) - exact match below;
  * prices from the official catalog (https://tokenfactory.nebius.com/model-catalog.md).
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

MODEL_IDS = {
    "reason": "nvidia/nemotron-3-super-120b-a12b",
    "loop": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
}

# USD per million tokens, official catalog (checked 2026-09-27).
MODEL_PRICES = {
    "reason": {"in": 0.30, "out": 0.90},
    "loop": {"in": 0.06, "out": 0.24},
}

# Reasoning models spend completion budget on hidden reasoning BEFORE the
# answer: a low cap leaves ``content`` empty (measured 2026-09-27:
# max_tokens=15 -> content=None, finish_reason=length; and 4096 -> same thing on
# a prompt that had succeeded a minute earlier). The router starts at
# DEFAULT_MAX_TOKENS and retries, doubling up to DEFAULT_MAX_TOKENS_CAP.
DEFAULT_MAX_TOKENS = 4096
# Measured 2026-09-29 on a real repo prompt (humanize #329, ~24KB): the model
# spent the FULL 16384-token budget on hidden reasoning and returned no answer,
# so the old cap was below what this workload needs. 32768 is the next
# doubling; hitting the cap again means the task/prompt needs rethinking, not a
# larger budget.
DEFAULT_MAX_TOKENS_CAP = 32768


class RouterError(RuntimeError):
    """Raised when a model call cannot be made, or its reply cannot be used."""


class ReasoningBudgetExhausted(RouterError):
    """Completion budget ran out before the model produced a usable answer.

    Reasoning tokens count as completion tokens: with a low ``max_tokens`` a
    reasoning model can spend the whole budget thinking and return
    ``content=None`` with ``finish_reason="length"``. It can also happen
    MID-ANSWER: the reply is non-empty but cut off mid-way (measured
    2026-09-30 live: a 3002-char reply truncated mid-string passed as
    "successful" and the run died at JSON parsing with 0 branches). Retry with
    a larger budget in both cases. (Measured 2026-09-27: max_tokens=15 ->
    None; max_tokens=4096 -> sometimes None on the same prompt that succeeded
    before.)"""


class EmptyModelReply(RouterError):
    """Reply carried no usable text while the budget was NOT the limit.

    ``finish_reason`` was not ``length`` but ``content`` was None or
    whitespace-only (measured 2026-09-29: a reply with 25k+ tokens of reasoning
    and ``content="\\n"`` slipped through as a "successful" reply). Retryable
    with the SAME budget: doubling does not help when the model produced
    nothing on its own.
    """


@dataclass
class ModelReply:
    text: str
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class ModelRouter(Protocol):
    def complete(self, role: str, prompt: str) -> ModelReply: ...


def resolve_model(role: str) -> str:
    """Model slug for ``role``; ``FIXFORK_<ROLE>_MODEL`` env overrides it."""
    override = os.environ.get(f"FIXFORK_{role.upper()}_MODEL", "").strip()
    return override or MODEL_IDS[role]


def estimate_cost(role: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = MODEL_PRICES.get(role)
    if not price:
        return 0.0
    return (prompt_tokens * price["in"] + completion_tokens * price["out"]) / 1_000_000


def _int_or_zero(value: object) -> int:
    """Token counts come from an external API: anything unparsable counts as 0
    instead of raising a ValueError that would escape the callers' error
    handling. Negatives clamp to 0 too - a negative count would poison
    ``spent_usd`` and break the attempt-log invariant. (Cross-check findings,
    2026-09-29 + 2026-09-30.)"""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def _finish_reason(body: dict | None) -> str:
    """``finish_reason`` from a chat-completion body; "" when absent/malformed.

    Kept separate from ``parse_chat_completion`` on purpose: the attempt log
    must stay readable even for bodies that parser rejects.
    """
    try:
        value = body["choices"][0]["finish_reason"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def _usage_cost_usd(role: str, body: dict) -> float:
    """Cost of a response that could NOT be used as an answer.

    Retry attempts still burn real tokens server-side; a run that reports only
    successful calls would understate what it spent. A body that is not even a
    JSON object (e.g. ``[]`` from a broken proxy) has no readable usage: 0 -
    and it must not crash the error path that is about to report it.
    (Cross-check finding, 2026-09-30.)"""
    if not isinstance(body, dict):
        return 0.0
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        return 0.0
    return estimate_cost(
        role,
        _int_or_zero(usage.get("prompt_tokens")),
        _int_or_zero(usage.get("completion_tokens")),
    )


def parse_chat_completion(body: dict, role: str) -> ModelReply:
    """Turn a raw chat-completions response into a ``ModelReply``.

    Pure function (unit-tested); raises ``RouterError`` with a specific hint
    when the response carries no usable text - most often a reasoning model
    that ran out of completion budget before producing an answer.
    """
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterError(
            f"unexpected Token Factory response: {json.dumps(body)[:400]}"
        ) from exc
    if not isinstance(message, dict):
        raise RouterError(
            "unexpected Token Factory response (message is not an object): "
            f"{json.dumps(body)[:400]}"
        )

    text = message.get("content")
    finish = choice.get("finish_reason")
    # Three states observed live on 2026-09-29 (large prompt): a usable answer,
    # content=None with finish_reason="length" (budget gone), and content="\n"
    # (whitespace-only with finish_reason="stop") - the last one used to pass
    # this function as a "successful" reply and only exploded later, midway
    # through JSON parsing, losing the retry chance.
    if not isinstance(text, str) or not text.strip():
        if finish == "length":
            raise ReasoningBudgetExhausted(
                "finish_reason=length: the completion budget was spent before any "
                f"answer; reasoning tokens count as completion tokens - raise "
                f"max_tokens: {json.dumps(body)[:400]}"
            )
        if finish == "content_filter":
            # Retrying the same prompt through the filter would just burn
            # another call: fail fast instead. (Cross-check finding, 2026-09-29.)
            raise RouterError(
                f"model stopped on content_filter with no answer: {json.dumps(body)[:400]}"
            )
        if message.get("reasoning"):
            raise EmptyModelReply(
                f"empty model content (reply carried reasoning but no usable text): "
                f"{json.dumps(body)[:400]}"
            )
        raise EmptyModelReply(f"empty model content: {json.dumps(body)[:400]}")

    # A NON-empty answer can still be cut mid-way by the completion budget:
    # observed live 2026-09-30 (tomlkit race) - a 3002-char diagnosis reply
    # truncated mid-string with finish_reason="length" passed as a "successful"
    # reply, then the whole run died at hypothesis parsing with 0 branches.
    # Every caller parses the full reply as JSON (verified: the only call sites
    # are the diagnosis call and the in-branch loop; both JSON), so partial
    # text from a length-stop is never usable: treat it as budget exhaustion
    # and let the retry ladder double the budget. A length-stop that happens to
    # end after a complete JSON body is retried too - one bounded extra call is
    # the accepted price of never feeding a truncated reply to a parser.
    if finish == "length":
        raise ReasoningBudgetExhausted(
            "finish_reason=length with a PARTIAL answer (reply cut mid-way by "
            f"the completion budget); raise max_tokens: {json.dumps(body)[:400]}"
        )

    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = _int_or_zero(usage.get("prompt_tokens"))
    completion_tokens = _int_or_zero(usage.get("completion_tokens"))
    return ModelReply(
        text=text,
        tokens_used=prompt_tokens + completion_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=estimate_cost(role, prompt_tokens, completion_tokens),
    )


class NebiusRouter:
    """OpenAI-compatible client for Nebius Token Factory (stdlib only).

    Every server call leaves one record in ``attempts`` (role, ladder step,
    budget, outcome, finish_reason, tokens, cost) - including failed retries,
    so a run's spend can be read back call by call instead of inferred from
    totals (blind spot found live 2026-09-30: a race retried calls and only
    the spend delta revealed it). ``retries`` counts retries actually issued.
    Invariant, unit-tested: ``spent_usd`` equals the sum of ``cost_usd``
    across ``attempts``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: int = 120,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tokens_cap: int = DEFAULT_MAX_TOKENS_CAP,
        max_attempts: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY", "")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_tokens_cap = max_tokens_cap
        # Bound on total calls per request: budget doublings AND empty-reply
        # retries both count, so a stuck model cannot loop forever on money.
        self.max_attempts = max_attempts
        # Every response with usage, successful or not: retries burn tokens too.
        # Instance-level total for the whole run (new router = fresh counter).
        self.spent_usd = 0.0
        # Per-call log (see class docstring): one record per server call.
        self.attempts: list[dict] = []
        # Retries actually issued (ladder steps after the first call).
        self.retries = 0
        # Nonsensical limits would disable those loop bounds (a zero budget
        # never grows under doubling); reject them up front rather than
        # discovering it as a hang. (Cross-check finding, 2026-09-29.)
        if max_tokens < 1:
            raise RouterError(f"max_tokens must be >= 1 (got {max_tokens})")
        if max_tokens_cap < max_tokens:
            raise RouterError(
                f"max_tokens_cap must be >= max_tokens (got {max_tokens_cap} < {max_tokens})"
            )
        if max_attempts < 1:
            raise RouterError(f"max_attempts must be >= 1 (got {max_attempts})")
        if not self.api_key:
            raise RouterError(
                "NEBIUS_API_KEY is not set - cannot call Token Factory. "
                "Use FakeRouter for offline runs."
            )

    def complete(self, role: str, prompt: str) -> ModelReply:
        if role not in MODEL_IDS:
            raise RouterError(f"unknown role {role!r} (expected one of {sorted(MODEL_IDS)})")
        budget = self.max_tokens
        attempts = 0
        while True:
            attempts += 1
            try:
                body = self._call(role, prompt, budget)
            except RouterError as exc:
                # The call never returned a body; it was still an attempt, and
                # a run that dies here must leave a readable trace. No usage
                # to bill, so cost stays 0 and the spend invariant holds.
                self._log_attempt(role, attempts, budget, "call_failed", error=str(exc))
                raise
            try:
                reply = parse_chat_completion(body, role)
            except ReasoningBudgetExhausted as exc:
                cost = _usage_cost_usd(role, body)
                self.spent_usd += cost
                self._log_attempt(
                    role, attempts, budget, "budget_exhausted", body, cost, str(exc)
                )
                # Retry only while another call is both allowed and able to
                # make progress; a stuck ladder must not loop forever.
                # (Cross-check finding, 2026-09-29.)
                if budget >= self.max_tokens_cap or attempts >= self.max_attempts:
                    raise
                next_budget = min(budget * 2, self.max_tokens_cap)
                if next_budget <= budget:  # belt and braces: no progress
                    raise
                budget = next_budget
                self.retries += 1
                continue
            except EmptyModelReply as exc:
                cost = _usage_cost_usd(role, body)
                self.spent_usd += cost
                self._log_attempt(role, attempts, budget, "empty_reply", body, cost, str(exc))
                if attempts >= self.max_attempts:
                    raise
                self.retries += 1
                continue  # same budget: the budget was not the problem
            except RouterError as exc:
                # Non-retryable failure (content_filter, malformed body): fail
                # fast as before, but bill and log it - every response with
                # usage counts, and a fatal call must not vanish from the log.
                cost = _usage_cost_usd(role, body)
                self.spent_usd += cost
                self._log_attempt(role, attempts, budget, "fatal", body, cost, str(exc))
                raise
            self.spent_usd += reply.cost_usd
            self._log_attempt(role, attempts, budget, "ok", body, reply.cost_usd)
            return reply

    def _log_attempt(
        self,
        role: str,
        attempt: int,
        budget: int,
        outcome: str,
        body: dict | None = None,
        cost_usd: float = 0.0,
        error: str = "",
    ) -> None:
        """Append one record to ``self.attempts`` (see class docstring).

        ``cost_usd`` must be the same number the caller added to
        ``spent_usd`` (0 when nothing was billed) - the unit tests assert the
        two reconcile.
        """
        if not isinstance(body, dict):
            body = None  # non-dict body (cross-check 30/09): no fields to read
        usage = (body or {}).get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        self.attempts.append(
            {
                "role": role,
                "attempt": attempt,
                "max_tokens": budget,
                "outcome": outcome,
                "finish_reason": _finish_reason(body),
                "prompt_tokens": _int_or_zero(usage.get("prompt_tokens")),
                "completion_tokens": _int_or_zero(usage.get("completion_tokens")),
                "cost_usd": cost_usd,
                "error": error[:300],
            }
        )

    def _call(self, role: str, prompt: str, max_tokens: int) -> dict:
        payload = {
            "model": resolve_model(role),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # any network/HTTP failure -> one clear error type
            raise RouterError(f"Token Factory call failed: {exc}") from exc
