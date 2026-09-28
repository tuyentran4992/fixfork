# FixFork

**Give a repo and a failing test. FixFork forks three hypotheses, races them
in sandboxes, and hands back the branch that passes - with evidence.**

An autonomous debugging agent for the
[Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/)
(Coding & Agentic Engineering track). It runs on **Nebius Token Factory**
(Nemotron 3 Super / Nano) and is built around **Token Factory Sandboxes**
(git-style fork / rollback) to race candidate fixes in parallel.

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
   applies its edit and runs the test suite.
4. **Cheap iteration** - branches that still fail get extra rounds from
   Nemotron 3 Nano (the small, fast loop model) to stretch credits.
5. **Evidence-based verdict** - score = tests green, then fewest lines changed.
   The winning branch wins; losers are rolled back.
6. **Output** - three artifacts per run: the race report (markdown, plus
   `--html` for a self-contained page), a **git-applyable patch** of the
   winning fix (`git apply fixfork-report.patch`), and the raw model reply
   for auditing.

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
