"""Model routing for FixFork: Nemotron on Nebius Token Factory.

Two roles keep credits stretchy:
  * ``reason`` -> Nemotron 3 Super 120B: reads code + failure log, forms the hypotheses.
  * ``loop``   -> Nemotron 3 Nano 30B: cheap extra iterations inside a branch.

The real backend is the OpenAI-compatible Token Factory API
(``https://api.tokenfactory.nebius.com/v1/``, key in env ``NEBIUS_API_KEY``).
``FakeRouter`` (see ``fakes.py``) keeps the pipeline runnable offline.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

# Slugs taken from the Token Factory model catalog (read 2026-09-27). Verify
# against ``GET {base_url}/models`` once an API key is available.
MODEL_IDS = {
    "reason": "nvidia/Nemotron-3-Super-120B-A12B",
    "loop": "nvidia/Nemotron-3-Nano-30B-A3B",
}


class RouterError(RuntimeError):
    """Raised when a model call cannot be made, or its reply cannot be used."""


@dataclass
class ModelReply:
    text: str
    tokens_used: int = 0


class ModelRouter(Protocol):
    def complete(self, role: str, prompt: str) -> ModelReply: ...


class NebiusRouter:
    """OpenAI-compatible client for Nebius Token Factory (stdlib only)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise RouterError(
                "NEBIUS_API_KEY is not set - cannot call Token Factory. "
                "Use FakeRouter for offline runs."
            )

    def complete(self, role: str, prompt: str) -> ModelReply:
        if role not in MODEL_IDS:
            raise RouterError(f"unknown role {role!r} (expected one of {sorted(MODEL_IDS)})")
        payload = {
            "model": MODEL_IDS[role],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
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
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # any network/HTTP failure -> one clear error type
            raise RouterError(f"Token Factory call failed: {exc}") from exc
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RouterError(f"unexpected Token Factory response: {json.dumps(body)[:400]}") from exc
        usage = body.get("usage") or {}
        tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        return ModelReply(text=text, tokens_used=tokens)
