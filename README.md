# FixFork

**MIT licensed** — see [LICENSE](LICENSE).

**Give a repo and a failing test. FixFork forks three hypotheses, races them
in sandboxes, and hands back the branch that passes - with evidence.**

An autonomous debugging agent for the
[Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/)
(Coding & Agentic Engineering track). It runs on **Nebius Token Factory**
with NVIDIA Nemotron models: **Nemotron 3 Super 120B** for the diagnosis and
**Nemotron 3 Nano 30B** for the cheap repair loop. Candidate fixes race as
forked branches through a git-style sandbox abstraction (`checkpoint` /
`fork` / `rollback`) - the local backend ships today, and a **Token Factory
Sandboxes** backend is the next integration.

> Status: **early development.** The offline pipeline (deterministic fake
> router + local sandbox) runs end to end, and live Token Factory model calls
> are wired and verified (2026-09-28: full diagnosis + 3-branch race on the
> demo repo, ~$0.003 per run; the exported patch was re-applied to a pristine
> checkout with `git apply` and the tests passed). The Token Factory Sandboxes
> backend is wired next.

## How it works

1. **Baseline** - run the failing test; confirm it is red.
2. **Three hypotheses** - Nemotron 3 Super reads the code and the failure log
   and proposes three *divergent* root causes, each with a concrete edit plan.
3. **Fork & race** - the sandbox state is forked into three branches; each
   applies its edit and runs the test suite. (Today this runs on the local
   temp-dir backend - no real isolation - until the Token Factory Sandboxes
   backend lands.)
4. **Cheap iteration** - branches that still fail get extra rounds from
   Nemotron 3 Nano (the small, fast loop model) to stretch credits - up to two
   rounds per branch.
5. **Evidence-based verdict** - score = tests green, then fewest lines changed.
   The winning branch wins; losers are rolled back. If no branch goes green,
   no patch is exported: the report names the least-failed branch as a lead,
   not a fix.
6. **Output** - three artifacts per run: the race report (markdown, plus
   `--html` for a self-contained page), a **git-applyable patch** of the
   winning fix (`git apply fixfork-report.patch`), and the raw model reply
   for auditing.

## What makes FixFork different

Instead of a single "suggested fix":

1. **Three divergent hypotheses race in parallel.** Each candidate fix runs as
   its own forked branch (git-style checkpoint / fork / rollback through the
   sandbox abstraction) rather than a single edit suggestion.
2. **The winner is picked by evidence, not vibes.** Green tests first, then
   the fewest changed lines; losing branches are rolled back, and the race
   report shows tokens and USD cost per branch.
3. **The output is usable artifacts.** A `git apply`-able patch of the winning
   fix, a self-contained HTML report, and the raw model reply for auditing.
   The test suite (`python3 -m unittest discover -s tests -t .`) runs 60 tests.

## How it uses Nebius & NVIDIA

- **Nebius Token Factory** serves every model call through its
  OpenAI-compatible API. The router tracks prompt/completion tokens and USD
  cost per branch, and it detects reasoning-budget exhaustion
  (`finish_reason=length` with empty content) and retries with a doubled
  `max_tokens` (4096 -> 8192 -> 16384).
- **Nemotron 3 Super 120B** (`nvidia/nemotron-3-super-120b-a12b`) - the
  diagnosis model: it reads the repo files and the failure log and proposes
  three divergent root causes, each with a concrete edit plan.
- **Nemotron 3 Nano 30B** (`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`) - the
  small, fast loop model used for extra repair rounds on branches that still
  fail, keeping the race cheap.
- **Token Factory Sandboxes** (next integration) - the planned backend for
  isolating the three racing branches on Nebius infrastructure; today the
  race runs on the local sandbox backend.

## Requirements

Python **3.10+** and `git` on PATH (to re-apply the exported patch; the
patch-export tests also exercise `git apply`). No third-party packages:
FixFork is standard-library only - no `pip install` needed (the live router
talks to Token Factory via `urllib`).

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

The live router starts at `max_tokens=4096` and retries with a doubled budget
(up to 16384) when a reasoning model spends the whole budget thinking instead
of answering — reasoning tokens are billed as completion tokens. Tokens and
USD cost are tracked per branch in the report.

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
| Sandboxes | `LocalSandbox` (temp dirs, no isolation) | Token Factory Sandboxes (fork / rollback) — wired next |

## License

MIT - see [LICENSE](LICENSE).
