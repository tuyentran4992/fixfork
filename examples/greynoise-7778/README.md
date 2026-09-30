# Case study: OpenCTI-Platform/connectors issue #7778 (live runs, 2026-10-01)

Context: [connectors #7778](https://github.com/OpenCTI-Platform/connectors/issues/7778) -
the `greynoise` internal-enrichment connector crashes with
`KeyError: 'classification'` when a GreyNoise response omits
`internet_scanner_intelligence.classification` (7 direct reads of that key in one file).
FixFork (dev-stage) was pointed at a sparse checkout of `master` (`c07d72d`) with the
three failing regression tests applied (red baseline: 3 failed, 8 passed).

**Result summary: two live runs did not produce a fully green patch on their own - the
raw outputs below say exactly that.** The final patch in `final-patch/` was completed
by hand from the tool's lead output and verified against the repository's own test
suite (11 passed). The runs are published because the tool's reports, raw model replies
and event logs are also evidence of *where the tool still fails*.

## Contents

- `fixfork-run-1/` - first run: the diagnosis model's hypothesis reply was truncated
  and did not parse (one missing `]`), so the run stopped with no branches
  (cost ~$0.0057). This exact failure drove a bounded JSON-repair fix in the tool
  itself.
- `fixfork-run-2/` - second run after that fix: 3 hypotheses raced, all 3 branches
  red (best branch: 2 of 11 tests still failing); the report's raw replies are in
  `diagnosis.json`, retries in `attempts.jsonl`, the repair-loop sidecar in `loops.jsonl`.
- `final-patch/` - the completed patch for #7778 (7 call sites in
  `connector.py` changed to `.get("classification", "unknown")` plus a new
  regression test file) and the verification output: `git apply` on a fresh clone of
  `master` (`c07d72d`) -> suite `11 passed` (Python 3.12, unprivileged sandbox,
  pytest only).

## Costs (router counts, all responses incl. retries)

- run 1: ~$0.0057 in model calls; run 2: ~$0.0095.
- Model: `nvidia/nemotron-3-super-120b-a12b` via Nebius Token Factory.

## Honesty note

The final patch was **not** produced end-to-end by the tool. The tool reproduced the
failure, identified the right area (every direct read of the missing key), and proposed
candidate edits (run 2, branch 1 - closest); the final patch was then completed and
verified by hand against the repository's own test suite before being submitted
upstream. This is consistent with the tomlkit #619 case in `../tomlkit-619/`.
