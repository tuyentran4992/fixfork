"""Render a run report (markdown / dict) from a ``RunReport``."""

from __future__ import annotations

from .models import RunReport


def summary_dict(report: RunReport) -> dict:
    return {
        "repo": report.repo,
        "test_command": report.test_command,
        "baseline": {
            "ok": report.baseline.ok,
            "passed": report.baseline.passed,
            "failed": report.baseline.failed,
            "summary": report.baseline.summary,
        },
        "branches": [
            {
                "hypothesis_id": b.hypothesis_id,
                "status": b.status.value,
                "passed": b.outcome.passed,
                "failed": b.outcome.failed,
                "lines_changed": b.lines_changed,
                "tokens_used": b.tokens_used,
                "rounds": b.rounds,
            }
            for b in report.branches
        ],
        "winner_id": report.winner_id,
        "winner_reason": report.winner_reason,
        "tokens_total": sum(b.tokens_used for b in report.branches),
        "notes": report.notes,
    }


def render_markdown(report: RunReport) -> str:
    lines: list[str] = [
        "# FixFork run report",
        "",
        f"- Repo: `{report.repo}`",
        f"- Test command: `{report.test_command}`",
        f"- Baseline: **{report.baseline.summary}**",
        "",
        "## Branches",
        "",
        "| Branch | Status | Tests | Lines changed | Tokens |",
        "|---|---|---|---|---|",
    ]
    for branch in report.branches:
        lines.append(
            f"| {branch.hypothesis_id} | {branch.status.value} | "
            f"{branch.outcome.summary} | {branch.lines_changed} | {branch.tokens_used} |"
        )
    lines += ["", "## Verdict", "", f"- Winner: **branch {report.winner_id}**", f"- Why: {report.winner_reason}", ""]

    if report.winner_diff:
        lines += ["## Winning diff", "", "```diff", report.winner_diff.rstrip("\n"), "```", ""]

    if report.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in report.notes]
        lines.append("")

    return "\n".join(lines)
