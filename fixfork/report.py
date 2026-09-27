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
                "cost_usd": round(b.cost_usd, 6),
                "rounds": b.rounds,
            }
            for b in report.branches
        ],
        "winner_id": report.winner_id,
        "winner_reason": report.winner_reason,
        "hypotheses": [
            {"id": h.id, "title": h.title, "rationale": h.rationale,
             "edit_files": sorted({e.file for e in h.edits})}
            for h in report.hypotheses
        ],
        "tokens_total": report.total_tokens,
        "cost_usd_total": round(report.total_cost_usd, 6),
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
        "| Branch | Status | Tests | Lines changed | Tokens | Cost ($) |",
        "|---|---|---|---|---|---|",
    ]
    for branch in report.branches:
        lines.append(
            f"| {branch.hypothesis_id} | {branch.status.value} | "
            f"{branch.outcome.summary} | {branch.lines_changed} | {branch.tokens_used} | "
            f"{branch.cost_usd:.5f} |"
        )
    lines += [
        "",
        "## Verdict",
        "",
        f"- Winner: **branch {report.winner_id}**",
        f"- Why: {report.winner_reason}",
        f"- Tokens: **{report.total_tokens}** total ({report.diagnosis_tokens} diagnosis) "
        f"· cost ~ **${report.total_cost_usd:.4f}**",
        "",
    ]

    if report.hypotheses:
        lines += ["## Hypotheses (raced)", ""]
        for h in report.hypotheses:
            files = ", ".join(sorted({e.file for e in h.edits}))
            lines.append(f"- **{h.id}. {h.title}** — {h.rationale} _(files: {files})_")
        lines.append("")

    if report.winner_diff:
        lines += ["## Winning diff", "", "```diff", report.winner_diff.rstrip("\n"), "```", ""]

    if report.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in report.notes]
        lines.append("")

    return "\n".join(lines)
