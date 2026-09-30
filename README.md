# FixFork

**MIT licensed** — see [LICENSE](LICENSE).

**Give a repo and a failing test. FixFork forks three hypotheses, races them
on forked branches, and hands back the branch that passes - with evidence.**

An autonomous debugging agent for the
[Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/)
(Coding & Agentic Engineering track). It runs on **Nebius Token Factory**
with NVIDIA Nemotron models: **Nemotron 3 Super 120B** for the diagnosis and
**Nemotron 3 Nano 30B** for the cheap repair loop. Before the diagnosis call,
the failure signature is grounded with a real **Tavily** web search. Candidate
fixes race on forked branches through a git-style sandbox abstraction
(`checkpoint` / `fork` / `rollback`) - a local backend is the default, and the
**Token Factory Sandboxes** backend is wired and verified live (opt-in,
`--sandbox nebius`).

> Status: **early development.** The offline pipeline (deterministic fake
> router + local sandbox) runs end to end, and live Token Factory model calls
> are wired and verified (2026-09-28: full diagnosis + 3-branch race on the
> demo repo, ~$0.003 per run; the exported patch was re-applied to a pristine
> checkout with `git apply` and the tests passed). Web grounding via Tavily
> is wired and verified (2026-09-28: a live run injected 5 real search results
> into the diagnosis prompt before proposing hypotheses).
> Validated on a real external repository (2026-09-29): run against
> python-humanize (base commit 823ad60~1 plus the regression tests from
> PR #329), FixFork went from 6 failing tests to a winning patch; the exported
> patch re-applied cleanly with `git apply` outside the pipeline (pristine
> checkout, isolated sandbox, unprivileged user) and the repository's own test
> suite then reported 310 passed (three files whose dev-only dependencies are
> absent from the sandbox excluded). The run took about 7.6 minutes and about
> $0.026 in model calls, retries included.
> **Token Factory Sandboxes backend is wired and verified live** (2026-09-29):
> a full three-branch race ran entirely inside Nebius sandboxes - baseline red
> inside the VM, three branches forked from one state uuid, tests executed in
> the VMs, and the winning 1-line patch exported from the sandbox and
> re-applied with `git apply` on a pristine checkout (tests green). The race
> used 13 sandbox operations, measured $0.0044 in sandbox cost, and took
> ~102 s wall-clock including the live diagnosis.
> **Real-world result** (merged 2026-09-30): run against a live upstream
> issue, python-poetry/tomlkit #619 (stdlib `fold` support) - the runs
> reproduced the failure and located the fix area; the run traces are public
> (`examples/tomlkit-619/`). Neither run finished the patch on its own; the
> final patch (10 source lines plus 2 regression tests, completed and
> verified by hand from the run report) passed tomlkit's own full suite
> (1,060 passed) and was merged into tomlkit master by the maintainer
> 66 minutes after the pull request opened
> ([PR #620](https://github.com/python-poetry/tomlkit/pull/620): +52/-1
> across 2 files, 24/24 CI checks green).

## How it works

1. **Baseline** - run the failing test; confirm it is red.
2. **Web grounding (Tavily)** - one real search call per run for the failure
   signature (failing test + exception line); the top results are injected
   into the diagnosis prompt as *hints*. If the search fails, the run
   continues and the report records an honest note.
3. **Three hypotheses** - Nemotron 3 Super reads the code, the failure log
   and the web hints, and proposes three *divergent* root causes, each with a
   concrete edit plan. Files the failure log points at are rendered first in
   the prompt, and the prompt size caps are adjustable (`--max-file-chars`,
   `--max-total-chars`) for repositories whose failing module outweighs the
   default budget.
4. **Fork & race** - the sandbox state is forked into three branches; each
   applies its edit (exact string match first, with a bounded fallback that
   tolerates blank-line / trailing-space drift only when the match is unique)
   and runs the test suite inside its own sandbox. (The default local backend
   uses temp dirs - no isolation; with `--sandbox nebius` each branch forks
   a VM-level state image on Token Factory Sandboxes.)
5. **Cheap iteration** - branches that still fail get extra rounds from
   Nemotron 3 Nano (the small, fast loop model) to stretch credits - up to two
   rounds per branch.
6. **Evidence-based verdict** - score = tests green, then fewest lines changed.
   The winning branch wins; losers are rolled back. If no branch goes green,
   no patch is exported: the report names the least-failed branch as a lead,
   not a fix. A branch that proposes edits to test/CI/config files is refused
   before it runs (`blocked`) - the test suite is the referee. A branch whose
   edits do not locate in the baseline tree is refused before it forks
   (`not_run`); it still appears in the report (marked `not_run`), but never
   as a branch that ran - its approach was never tested.
7. **Output** - a self-contained race report (markdown, plus `--html` for a
   page), the raw model replies for auditing (the diagnosis reply, every loop
   reply in a `.loops.jsonl` sidecar when follow-up rounds ran, and a per-call
   `.attempts.jsonl` sidecar whenever the live router ran - failed retries
   included, so the run's spend is auditable call by call),
   and - when a branch wins - a **git-applyable patch** of the winning fix
   (`git apply fixfork-report.patch`).

## What makes FixFork different

Instead of a single "suggested fix":

1. **Three divergent hypotheses race in parallel.** Each candidate fix runs as
   its own forked branch (git-style checkpoint / fork / rollback through the
   sandbox abstraction) rather than a single edit suggestion.
2. **The winner is picked by evidence, not vibes.** Green tests first, then
   the fewest changed lines; losing branches are rolled back, and the race
   report shows tokens and USD cost per branch.
3. **The output is usable artifacts.** A `git apply`-able patch of the winning
   fix, a self-contained HTML report, and the raw model replies for auditing.
   The test suite (`python3 -m unittest discover -s tests -t .`) runs 187 tests.
4. **The referee is protected.** Every proposed edit is checked before any
   sandbox work: edits to test files, CI workflows or build/config files are
   refused, the branch is marked `blocked`, and it can never win or be
   suggested as a lead. `tests/test_race_replay.py` replays a recorded race
   where a test-editing branch used to score green - it is now refused while
   the real fix still wins. Protected paths are matched case-insensitively -
   `Tests/` (as in Pillow), `__tests__/` and `testdata/` all count - and the
   exported patch is re-checked against the same list, so a renamed, quoted
   or differently-cased protected path is withheld, not shipped.
5. **The diagnosis is web-grounded.** One real Tavily search per run over the
   failure signature seeds the hypothesis prompt with outside context - the
   model still has to produce exact-match edits that the tests verify, so the
   search is a hint, never a verdict.

## How it uses Nebius & NVIDIA

- **Nebius Token Factory** serves every model call through its
  OpenAI-compatible API. The router tracks prompt/completion tokens and USD
  cost per branch, writes a per-call `.attempts.jsonl` sidecar (every call,
  failed retries included - spend is auditable call by call), and it detects
  reasoning-budget exhaustion (`finish_reason=length`, empty OR partial
  content) and retries with a doubled `max_tokens` (4096 -> 8192 -> 16384 ->
  32768).
- **Nemotron 3 Super 120B** (`nvidia/nemotron-3-super-120b-a12b`) - the
  diagnosis model: it reads the repo files and the failure log and proposes
  three divergent root causes, each with a concrete edit plan.
- **Nemotron 3 Nano 30B** (`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`) - the
  small, fast loop model used for extra repair rounds on branches that still
  fail, keeping the race cheap.
- **Token Factory Sandboxes** - the live backend for isolating the three
  racing branches on Nebius infrastructure (opt-in: `--sandbox nebius`).
  Checkpoint, fork and rollback are content-addressed state-image moves; a
  full race measured 13 operations and $0.0044 in sandbox cost (2026-09-29).

## Web research (Tavily)

- Before the diagnosis call, FixFork runs **one real web search per run** via
  the **Tavily Search API** for the failure signature (failing test +
  exception line). The top results are injected into the Nemotron prompt as
  *hints* - the model must still produce concrete edits that the tests verify.
- Access: **keyless by default** (no account, rate-limited) - or set
  `TAVILY_API_KEY` to use an API key (identical response schema, free tier).
  Search results, the query and source URLs are recorded in the run report.
- A failed search never breaks a run: FixFork records "web research skipped:
  ..." and continues without the section.
- A recorded run (report, patch, raw replies, event log):
  `examples/live-run-web-grounded/`.
- Probe the integration directly:
  `python3 -m fixfork research --query "python unittest AssertionError ..."`.

## Requirements

Python **3.10+** and `git` on PATH (to re-apply the exported patch; the
patch-export tests also exercise `git apply`). No third-party packages:
FixFork is standard-library only - no `pip install` needed (the live router
talks to Token Factory via `urllib`, and the Tavily search call is `urllib`
too). Live runs need internet access (Token Factory + Tavily).

## Quickstart (offline demo, no API key needed)

```bash
cd fixfork
python3 -m fixfork run \
  --repo examples/demo-repo \
  --test "python3 -m unittest discover -s tests -v" \
  --fake --out fixfork-report.md
```

Runs the full pipeline against the bundled demo repo (a small Python module
with a planted bug) using the deterministic fake router and the local sandbox.
Outputs a branch race report, a git-applyable `fixfork-report.patch` with the
winning fix, and (with `--html`) a single-file HTML report.

Apply the winner anywhere and re-run the tests:

```bash
git apply fixfork-report.patch   # on a pristine checkout
```

## Quickstart (live, Nebius Token Factory)

```bash
export NEBIUS_API_KEY=...            # Token Factory key (never commit it)
python3 -m fixfork run \
  --repo examples/demo-repo \
  --test "python3 -m unittest discover -s tests -v" \
  --out fixfork-report.md
```

Live runs include one Tavily web search for the failure signature by default
(keyless needs no extra key; set `TAVILY_API_KEY` to use a free-tier key, or
pass `--research off` to skip the search).

To race the branches on Token Factory Sandboxes instead of local temp dirs:

```bash
export NEBIUS_API_KEY=...            # Token Factory key
export NEBIUS_SANDBOX_PROJECT=...    # Sandboxes project id
python3 -m fixfork run \
  --repo examples/demo-repo \
  --test "python3 -m unittest discover -s tests" \
  --sandbox nebius --out fixfork-report.md
```

`--fake` always keeps the local backend, so offline demo runs never call the
Sandboxes API.

The live router starts at `max_tokens=4096` and retries with a doubled budget
(up to 32768) when a reasoning model spends the whole budget thinking instead
of answering — reasoning tokens are billed as completion tokens. Tokens and
USD cost are tracked per branch in the report, and every call (failed retries
included) lands in a `.attempts.jsonl` sidecar next to the report.

## Layout

```
fixfork/            package (pipeline, router, sandbox, judge, report)
tests/              unittest suite for the package
examples/demo-repo/ tiny buggy repo used by the offline demo
```

## Backends

| Piece | Offline (default here) | Nebius |
|---|---|---|
| Models | `FakeRouter` (deterministic fixtures) | wired & verified: Nemotron 3 Super 120B (diagnosis) + Nano 30B (loop) via Token Factory |
| Sandboxes | `LocalSandbox` (temp dirs, no isolation) | wired & verified: fork / rollback on content-addressed state uuids (`--sandbox nebius`); full race: 13 ops, $0.0044 (2026-09-29) |
| Web research | `FakeResearch` / `--research off` | Tavily Search — wired & verified (one call per run; keyless or free-tier key) |

## License

MIT - see [LICENSE](LICENSE).
