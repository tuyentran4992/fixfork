# FixFork

**Give a repo and a failing test. FixFork forks three hypotheses, races them
in sandboxes, and hands back the branch that passes - with evidence.**

An autonomous debugging agent for the
[Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/)
(Coding & Agentic Engineering track). It runs on **Nebius Token Factory**
(Nemotron 3 Super / Nano) and is built around **Token Factory Sandboxes**
(git-style fork / rollback) to race candidate fixes in parallel.

> Status: **early development.** The offline pipeline (deterministic fake
> router + local sandbox) runs end to end today. The Nebius sandbox backend and
> live Token Factory model calls are wired next.

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
6. **Output** - a unified diff of the winning fix plus a report of every branch
   tried (what it changed, test outcome, tokens spent).

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
Outputs a branch race report + the winning patch.

## Layout

```
fixfork/            package (pipeline, router, sandbox, judge, report)
tests/              unittest suite for the package
examples/demo-repo/ tiny buggy repo used by the offline demo
```

## Backends

| Piece | Offline (default here) | Nebius (wired next) |
|---|---|---|
| Models | `FakeRouter` (deterministic fixtures) | Nemotron 3 Super 120B + Nano 30B via Token Factory |
| Sandboxes | `LocalSandbox` (temp dirs, no isolation) | Token Factory Sandboxes (fork / rollback) |

## License

MIT - see [LICENSE](LICENSE).
