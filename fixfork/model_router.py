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
DEFAULT_MAX_TOKENS_CAP = 16384


class RouterError(RuntimeError):
    """Raised when a model call cannot be made, or its reply cannot be used."""


class ReasoningBudgetExhausted(RouterError):
    """Completion budget ran out before the model produced any answer text.

    Reasoning tokens count as completion tokens: with a low ``max_tokens`` a
    reasoning model can spend the whole budget thinking and return
    ``content=None`` with ``finish_reason="length"``. Retry with a larger
    budget. (Measured 2026-09-27: max_tokens=15 -> None; max_tokens=4096 ->
    sometimes None on the same prompt that succeeded before.)"""


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

    text = message.get("content")
    if not text:
        finish = choice.get("finish_reason")
        if finish == "length":
            raise ReasoningBudgetExhausted(
                "finish_reason=length: the completion budget was spent before any "
                f"answer; reasoning tokens count as completion tokens - raise "
                f"max_tokens: {json.dumps(body)[:400]}"
            )
        if message.get("reasoning"):
            raise RouterError(
                f"empty model content (reply carried reasoning but no content): "
                f"{json.dumps(body)[:400]}"
            )
        raise RouterError(f"empty model content: {json.dumps(body)[:400]}")

    usage = body.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    return ModelReply(
        text=text,
        tokens_used=prompt_tokens + completion_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=estimate_cost(role, prompt_tokens, completion_tokens),
    )


class NebiusRouter:
    """OpenAI-compatible client for Nebius Token Factory (stdlib only)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: int = 120,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tokens_cap: int = DEFAULT_MAX_TOKENS_CAP,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY", "")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_tokens_cap = max_tokens_cap
        if not self.api_key:
            raise RouterError(
                "NEBIUS_API_KEY is not set - cannot call Token Factory. "
                "Use FakeRouter for offline runs."
            )

    def complete(self, role: str, prompt: str) -> ModelReply:
        if role not in MODEL_IDS:
            raise RouterError(f"unknown role {role!r} (expected one of {sorted(MODEL_IDS)})")
        budget = self.max_tokens
        while True:
            body = self._call(role, prompt, budget)
            try:
                return parse_chat_completion(body, role)
            except ReasoningBudgetExhausted:
                if budget >= self.max_tokens_cap:
                    raise
                budget = min(budget * 2, self.max_tokens_cap)

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
