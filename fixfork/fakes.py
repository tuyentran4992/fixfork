"""Deterministic stand-ins used by tests and the offline demo.

``FakeRouter`` is NOT a model: it replays canned replies for the bundled demo
repo so the full pipeline can be exercised without a Token Factory key.
"""

from __future__ import annotations

import json

from .model_router import ModelReply

# Canned "reason" reply: three divergent hypotheses about the planted bug in
# examples/demo-repo (a discount multiplier applied with the wrong sign).
DEMO_HYPOTHESES = [
    {
        "title": "Discount applied with the wrong sign",
        "rationale": "subtotal * (1 + discount) grows the order instead of shrinking it.",
        "edits": [
            {
                "file": "src/tax.py",
                "find": "discounted = subtotal * (1 + discount)",
                "replace": "discounted = subtotal * (1 - discount)",
            }
        ],
    },
    {
        "title": "VAT computed on a wrong base",
        "rationale": "The VAT term should be consistent with the base the discount produced.",
        "edits": [
            {
                "file": "src/tax.py",
                "find": "return round(discounted + discounted * vat_rate, 2)",
                "replace": "return round(discounted + subtotal * vat_rate, 2)",
            }
        ],
    },
    {
        "title": "VAT should not be part of the order total",
        "rationale": "Maybe the total is meant to be pre-VAT and VAT is reported separately.",
        "edits": [
            {
                "file": "src/tax.py",
                "find": "return round(discounted + discounted * vat_rate, 2)",
                "replace": "return round(discounted, 2)",
            }
        ],
    },
]

DEMO_LOOP_EDITS = {"edits": []}  # loop model: no further change -> branch ends red


class FakeRouter:
    """Replays canned replies keyed by role. Deterministic, zero tokens."""

    def __init__(self, reason_reply: str | None = None, loop_reply: str | None = None) -> None:
        self.reason_reply = (
            reason_reply if reason_reply is not None else json.dumps(DEMO_HYPOTHESES)
        )
        self.loop_reply = loop_reply if loop_reply is not None else json.dumps(DEMO_LOOP_EDITS)
        self.calls: list[tuple[str, str]] = []

    def complete(self, role: str, prompt: str) -> ModelReply:
        self.calls.append((role, prompt))
        if role == "reason":
            return ModelReply(text=self.reason_reply, tokens_used=0)
        if role == "loop":
            return ModelReply(text=self.loop_reply, tokens_used=0)
        raise ValueError(f"FakeRouter does not handle role {role!r}")
