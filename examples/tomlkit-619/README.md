# Case study: python-poetry/tomlkit issue #619 (live runs, 2026-09-30)

Context: [tomlkit #619](https://github.com/python-poetry/tomlkit/issues/619) -
`Time`/`DateTime` are missing the stdlib `fold` argument. FixFork (dev-stage)
was pointed at a checkout of tomlkit (`8c959b51c7da2734662af759a7c9f95e9a78060a`)
with the two failing tests from the issue applied (red baseline: 2 failed,
92 passed).

**Result summary: two live runs did not produce a fully green patch on their
own - the raw outputs below say exactly that.** The final patch in
`final-patch/` was completed after reviewing these runs and verified against
tomlkit's full test suite (1060 passed). The runs are published because the
tool's reports, raw model replies and event logs are also evidence of *where
the tool still fails*.

## Contents

- `fixfork-run-1/` - run before the edit-application fallback existed: report,
  raw model reply (`diagnosis.json`), event log, stdout, and the apply-failure
  analysis (1 of 6 `find` blocks differed only by one dropped blank line -
  enough to kill the branch under byte-exact matching).
- `fixfork-run-2/` - run after the `locate_edit` fallback was added: edits
  applied, but no branch went green (branch 1 red, branch 2 hit a garbled
  model block, branch 3 still red after 3 repair rounds).
- `final-patch/` - the completed patch for #619 (10 added lines in
  `tomlkit/items.py` plus two tests in `tests/test_items.py`) and the
  verification output: `tests/test_items.py` -> 94 passed; full suite ->
  **1060 passed** (Python 3.12, unprivileged sandbox, pytest only).

## Costs (router counts, all responses incl. retries)

- run 1: ~$0.047 in model calls; run 2: ~$0.098.
- Model: `nvidia/nemotron-3-super-120b-a12b` via Nebius Token Factory.

## Honesty note

The final patch was **not** produced end-to-end by the tool. The tool
reproduced the failure, identified the right area (constructors and
`item()`/`replace()` round-trips), and proposed candidate edits; the final
patch was then completed and verified by hand against the repository's own
suite. FixFork itself gained fixes from this exercise (blank-line-tolerant
edit application, tests: 156 total in this repository).
