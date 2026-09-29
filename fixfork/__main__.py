"""Command line: ``python3 -m fixfork run --repo ... --test ... [--fake]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .events import JsonlEventSink, NullSink
from .fakes import FakeRouter
from .model_router import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOKENS_CAP,
    NebiusRouter,
    RouterError,
)
from .pipeline import run_pipeline
from .report import render_html, render_markdown, summary_dict
from .research import ResearchError, TavilyResearch
from .sandbox_runner import LocalSandbox


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fixfork",
        description="Fork three hypotheses, race them in sandboxes, keep the fix that passes.",
    )
    parser.add_argument("--version", action="version", version=f"fixfork {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the pipeline on a repo with a failing test")
    run.add_argument("--repo", required=True, help="path to the target repository")
    run.add_argument(
        "--test",
        required=True,
        dest="test_command",
        help="test command to run inside the repo (quote it)",
    )
    run.add_argument(
        "--fake",
        action="store_true",
        help="force the offline fake router (no API key needed)",
    )
    run.add_argument("--branches", type=int, default=3, help="number of hypotheses/branches")
    run.add_argument("--max-rounds", type=int, default=2, help="max test rounds per branch")
    run.add_argument(
        "--strict",
        action="store_true",
        help="fail (exit 2) instead of falling back to FakeRouter when the live router cannot start",
    )
    run.add_argument("--out", default="fixfork-report.md", help="report output path")
    run.add_argument(
        "--patch",
        default=None,
        help="write the winning diff as a git-applyable patch "
        "(default: same path as --out with a .patch suffix)",
    )
    run.add_argument("--html", action="store_true", help="also write an HTML report (<out>.html)")
    run.add_argument("--json", action="store_true", help="also print a JSON summary")
    run.add_argument(
        "--events",
        default=None,
        metavar="PATH",
        help="write a JSONL event log of the run (also streams live progress on stdout)",
    )
    run.add_argument(
        "--research",
        choices=["off", "tavily"],
        default=None,
        help="web-search grounding for diagnosis via the Tavily API "
        "(default: tavily on live runs, off with --fake)",
    )
    run.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Token Factory read timeout in seconds "
        "(large prompts + long reasoning can run for minutes)",
    )
    run.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        dest="max_tokens",
        help="starting completion budget per model call; reasoning models "
        "retry with a doubled budget up to --max-tokens-cap",
    )
    run.add_argument(
        "--max-tokens-cap",
        type=int,
        default=DEFAULT_MAX_TOKENS_CAP,
        dest="max_tokens_cap",
        help="hard cap for the retry ladder (default measured on a real repo)",
    )

    research = sub.add_parser(
        "research",
        help="run one Tavily search and print a compact JSON summary (keyless, no account)",
    )
    research.add_argument("--query", required=True, help="search query")
    research.add_argument(
        "--max-results", type=int, default=5, dest="max_results", help="number of results"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "research":
        client = TavilyResearch(max_results=args.max_results)
        try:
            result = client.search(args.query)
        except ResearchError as exc:
            print(f"! {exc}")
            return 2
        summary = {
            "query": result.query,
            "n_sources": result.n_sources,
            "access": "api-key" if client.api_key else "keyless",
            "sources": [{"title": s["title"], "url": s["url"]} for s in result.sources],
        }
        print(json.dumps(summary, indent=2))
        return 0

    if args.command != "run":
        parser.print_help()
        return 2

    if args.fake:
        router = FakeRouter()
        print("router: FakeRouter (offline, deterministic)")
    else:
        try:
            router = NebiusRouter(
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                max_tokens_cap=args.max_tokens_cap,
            )
            print("router: NebiusRouter (Token Factory)")
        except RouterError as exc:
            print(f"! {exc}")
            if args.strict:
                print("! --strict: refusing to fall back to FakeRouter")
                return 2
            print("! falling back to FakeRouter for this run")
            router = FakeRouter()

    research_client = None
    research_mode = args.research or ("off" if args.fake else "tavily")
    if research_mode == "tavily":
        research_client = TavilyResearch()
        access = "api key from env" if research_client.api_key else "keyless"
        print(f"research: Tavily web search ({access})")
    else:
        print("research: off")

    sandbox = LocalSandbox()
    sink = JsonlEventSink(args.events, echo=True) if args.events else NullSink()
    try:
        report = run_pipeline(
            repo=args.repo,
            test_command=args.test_command,
            router=router,
            sandbox=sandbox,
            branches=args.branches,
            max_rounds=args.max_rounds,
            events=sink,
            research=research_client,
        )
    finally:
        if isinstance(sink, JsonlEventSink):
            sink.close()

    Path(args.out).write_text(render_markdown(report), encoding="utf-8")
    if report.winner_diff:
        patch_path = Path(args.patch) if args.patch else Path(args.out).with_suffix(".patch")
        patch_path.write_text(report.winner_diff, encoding="utf-8")
        print(f"patch -> {patch_path}")
    else:
        print("patch: none written (no winning diff this run)")
    if args.html:
        html_path = Path(args.out).with_suffix(".html")
        html_path.write_text(render_html(report), encoding="utf-8")
        print(f"html -> {html_path}")
    if report.diagnosis_raw:
        diag_path = Path(str(args.out) + ".diagnosis.json")
        diag_path.write_text(report.diagnosis_raw, encoding="utf-8")
        print(f"raw diagnosis -> {diag_path}")
    print(f"baseline: {report.baseline.summary}")
    if report.research_query:
        print(
            f"research: {len(report.research_sources)} source(s) for "
            f"\"{report.research_query[:60]}\""
        )
    for branch in report.branches:
        print(
            f"  branch {branch.hypothesis_id}: {branch.status.value} | "
            f"{branch.outcome.summary} | {branch.lines_changed} line(s) changed"
        )
    print(f"winner: {report.winner_id} - {report.winner_reason}")
    print(
        f"tokens: {report.total_tokens} ({report.diagnosis_tokens} diagnosis) "
        f"| cost ~ ${report.total_cost_usd:.4f}"
    )
    if isinstance(router, NebiusRouter):
        print(
            f"router spend: ${router.spent_usd:.4f} incl. retry attempts "
            "(all responses with usage)"
        )
    print(f"report -> {args.out}")
    if args.json:
        print(json.dumps(summary_dict(report), indent=2))
    return 0 if report.winner_id is not None else 1


if __name__ == "__main__":
    sys.exit(main())
