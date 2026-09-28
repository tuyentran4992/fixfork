# Live run — web-grounded diagnosis (2026-09-28)

A real FixFork run on `examples/demo-repo` with the Tavily web-research step
enabled (five sources injected into the diagnosis prompt). Produced by:

```bash
python3 -m fixfork run --repo examples/demo-repo \
  --test "python3 -m unittest discover -s tests" \
  --out fixfork-live.md --html --events events.jsonl
```

Outcome: baseline `FAILED (2 of 2)` -> 3 hypotheses raced -> winner **branch
1** (1 line changed; tests green); losers rolled back. Cost: ~$0.0037 in
model tokens at published Token Factory prices.

Files:

- `fixfork-live.md` — the race report (query, sources, per-branch tokens/$).
- `fixfork-live.patch` — the winning diff; re-applied cleanly with `git apply`
  on a pristine checkout (tests green).
- `fixfork-live.html` — single-file HTML report.
- `fixfork-live.md.diagnosis.json` — the raw diagnosis reply (audit).
- `events.jsonl` — JSONL audit trail of the run (research -> hypotheses ->
  branches -> verdict -> rollbacks).
