# Case study: Nuitka #4063 — a race on a C-toolchain repository (capability test, 2026-10-10)

Context: [Nuitka #4063](https://github.com/Nuitka/Nuitka/issues/4063) — under
CPython 3.14, Nuitka-built programs raise `list.index` failures with the old
message (`'9 is not in list'`) instead of CPython's
`'list.index(x): x not in list'`. The fix lives in C
(`nuitka/build/static_src/HelpersLists.c`, `_LIST_INDEX_COMMON`). FixFork was
pointed at a checkout of Nuitka (`30b5b77fe236`, `develop`) with a build-based
referee (see below).

**Result: the race produced a working fix on its own — branch 1 went green in
one round with a one-line change** (`PyErr_Format(... "%R is not in list" ...)` →
`PyErr_SetString(..., "list.index(x): x not in list")`). A referee script
outside the tool then rebuilt a fresh copy inside an isolated unprivileged box
(CPython 3.14.3) and took the baseline from red to green — 2 of 2 message
checks passed (`referee/verify-after-race.txt`). A second branch stayed red
after two rounds. The raw diagnosis reply did not parse as valid JSON as a
whole; the runner's salvage path kept 2 complete hypotheses of the 3 requested
(its note: `salvaged 2 of 4 top-level object(s) from the raw text`), and both
were raced. A probe before the race verified that the box builds from the
copy's own package, not from the original clone (sample-patched copy: green;
unpatched tree: red at baseline).

**This case was run as a capability test. No pull request was opened for it.**
The runner kept the race's own patch as-is, and separately refined it by hand
into the repository's version-gating convention (`#elif PYTHON_VERSION < 0x3e0`
— the repository's hex gate style, i.e. "before 3.14"), because the race's
one-line change would have altered the message for every
Python 3.x, not only 3.14. The refined form was re-verified in the box under
CPython 3.14.3 (new message, 2/2) and 3.13.12 (old message preserved, 2/2).
In short: the referee verified the race's own patch; the hand refinement only
realigns it with the repository's version gating.

## The referee is build-based, not a unittest suite

The case's checks build the tree inside an isolated box and compare, for two
small programs that trigger the failing `list.index` path (MSG-A, MSG-B), the
resulting error message against the same box's stock CPython. That is
why the raw run report shows `OK (0 tests)` or `FAILED (1 of 0)`: read those
as "run passed / run failed, zero tests counted" — the runner sums
unittest/pytest summary lines, and the build referee prints none. The verdict
rests on the referee's exit code, and the referee's own message-check lines
(`OK (2 of 2 message checks passed)`) are in `referee/` and `final-patch/`.

## Contents

- `fixfork-run-lan1/` — the race: report, stdout (`run.log`), event log, raw
  diagnosis reply (`diagnosis.json` — as received; it does not parse as valid
  JSON as a whole, see the report notes), per-attempt and loop sidecars, and
  the winner patch exported from the race (`winner.patch`).
- `referee/` — the probe before the race (shows the box builds from the copy's
  own package) and the independent verification of the race winner on a fresh
  copy (`verify-after-race.txt`).
- `final-patch/` — the hand-refined gating form (`gating-form.patch`) and its
  re-verification under CPython 3.14.3 and 3.13.12, plus two negative controls:
  forcing the threshold to `0x3f0` selects the `#elif` branch under CPython
  3.14.3 and keeps the old message (2/2) — the gate, not luck, picks the
  message; and 8 of 8 checks confirm the version-macro clamp maps every tested
  version (`2.7`, `3.13.x`, `3.14.x`, `3.15`) to the right gate value.

## Costs

- Race: 91,587 tokens total (52,485 in diagnosis; router-reported cost
  ~$0.0245, retries included — as shown in `fixfork-run-lan1/report.md` and
  `run.log`) —
  `nvidia/nemotron-3-super-120b-a12b` (diagnosis) plus
  `NVIDIA-Nemotron-3-Nano-30B-A3B` (loop), via Nebius Token Factory.
- The hand refinement and its verification ran in local isolated boxes, with
  no model calls.

## Honesty notes

- 1 of 2 raced branches went green; the other stayed red after two rounds.
- The referee checks the two messages (plus the probe and controls); it is not
  a full Nuitka regression suite.
- The referee verified the race's own one-line patch (red → green). The
  upstream-form patch in `final-patch/` was completed by hand from that patch.
- Python <3.14 behavior of the refined form was measured via the 3.13.12
  rebuild and the forced-threshold control; no wider version matrix was run.
