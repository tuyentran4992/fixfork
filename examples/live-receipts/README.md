# Live model-call receipts

FixFork's router writes one receipt per model call into `.attempts.jsonl`, the
events JSONL and `loops.jsonl`. This folder holds three samples captured from
**live Nebius Token Factory calls** made on 2026-10-19 through
`fixfork.model_router.NebiusRouter`. They are stored exactly as the router
wrote them; no field was edited by hand.

Each attempt record carries:

| field | meaning |
|---|---|
| `latency_ms` | elapsed time measured around the HTTP read, in milliseconds |
| `prompt_tokens`, `completion_tokens` | token counts as returned by the API |
| `cost_usd` | USD cost computed from the catalog prices in `fixfork/model_router.py` |
| `response_sha256` | SHA-256 of the **raw HTTP response bytes**, taken before JSON parsing |
| `outcome`, `finish_reason`, `error` | how the call ended (failed retries are logged too) |

## What a receipt proves, and what it does not

It proves that the bytes which were parsed are exactly the bytes identified by
the digest, and it records when the call happened, how long it took and what it
cost. It does **not** identify which model produced those bytes, and the body
itself is not retained: only its digest is.

## Re-check the samples

```
python3 examples/live-receipts/check_receipts.py
```

The script validates every record (integer latency, positive token counts and
cost, 64-hex digest) and checks that the three digests are distinct. It exits
non-zero on any violation.
