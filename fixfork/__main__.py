"""Command line: ``python3 -m fixfork run --repo ... --test ... [--fake]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .fakes import FakeRouter
from .model_router import NebiusRouter, RouterError
from .pipeline import run_pipeline
from .report import render_markdown, summary_dict
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
    run.add_argument("--json", action="store_true", help="also print a JSON summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 2

    if args.fake:
        router = FakeRouter()
        print("router: FakeRouter (offline, deterministic)")
    else:
        try:
            router = NebiusRouter()
            print("router: NebiusRouter (Token Factory)")
        except RouterError as exc:
            print(f"! {exc}")
            if args.strict:
                print("! --strict: refusing to fall back to FakeRouter")
                return 2
            print("! falling back to FakeRouter for this run")
            router = FakeRouter()

    sandbox = LocalSandbox()
    report = run_pipeline(
        repo=args.repo,
        test_command=args.test_command,
        router=router,
        sandbox=sandbox,
        branches=args.branches,
        max_rounds=args.max_rounds,
    )

    Path(args.out).write_text(render_markdown(report), encoding="utf-8")
    if report.diagnosis_raw:
        diag_path = Path(str(args.out) + ".diagnosis.json")
        diag_path.write_text(report.diagnosis_raw, encoding="utf-8")
        print(f"raw diagnosis -> {diag_path}")
    print(f"baseline: {report.baseline.summary}")
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
    print(f"report -> {args.out}")
    if args.json:
        print(json.dumps(summary_dict(report), indent=2))
    return 0 if report.winner_id is not None else 1


if __name__ == "__main__":
    sys.exit(main())
