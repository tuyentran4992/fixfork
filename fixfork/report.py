"""Render a run report (markdown / html / dict) from a ``RunReport``."""

from __future__ import annotations

import html

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
        "research_query": report.research_query,
        "research_sources": report.research_sources,
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

    if report.research_query:
        lines += [
            "## Web research (Tavily)",
            "",
            f"- Query: `{report.research_query}`",
        ]
        lines += [f"- Source: {url}" for url in report.research_sources]
        lines.append("")

    if report.winner_diff:
        lines += ["## Winning diff", "", "```diff", report.winner_diff.rstrip("\n"), "```", ""]

    if report.notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in report.notes]
        lines.append("")

    return "\n".join(lines)


_CSS = """
:root { color-scheme: light dark; }
body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       margin: 0 auto; max-width: 960px; padding: 2rem 1rem 4rem; line-height: 1.5; }
h1 { font-size: 1.6rem; margin-bottom: .25rem; }
h2 { font-size: 1.15rem; margin-top: 2rem; }
.meta { color: #777; font-size: .9rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }
th, td { border: 1px solid #cfcfcf; padding: .4rem .6rem; text-align: left; font-size: .92rem; }
th { background: rgba(127,127,127,.12); }
.badge { padding: .1rem .5rem; border-radius: .75rem; font-size: .8rem; font-weight: 600; }
.badge.green { background: #d7f5dd; color: #0b5e21; }
.badge.red { background: #fbdede; color: #8c1a1a; }
.badge.error { background: #ffe9c7; color: #7a4a00; }
.badge.pending { background: #e8e8e8; color: #444; }
.verdict { background: rgba(59,108,255,.08); border-left: 4px solid #3b6cff;
           padding: .75rem 1rem; margin: 1rem 0; }
pre { background: #1c1f24; color: #e8e8e8; padding: 1rem; border-radius: 6px;
      overflow-x: auto; font-size: .85rem; }
.note { color: #777; font-size: .9rem; }
footer { margin-top: 2.5rem; color: #999; font-size: .8rem; }
"""


def render_html(report: RunReport) -> str:
    """Single-file HTML report (no external assets) for humans and judges."""
    esc = html.escape
    parts: list[str] = []
    parts.append("<h1>FixFork run report</h1>")
    parts.append(
        f'<p class="meta">Repo: <code>{esc(report.repo)}</code> &middot; '
        f"Test command: <code>{esc(report.test_command)}</code></p>"
    )

    parts.append("<h2>Baseline</h2>")
    parts.append(
        f"<p>Baseline test run: <b>{esc(report.baseline.summary)}</b> "
        f"({report.baseline.passed} passed, {report.baseline.failed} failed)</p>"
    )

    parts.append("<h2>Branches raced</h2>")
    if report.branches:
        rows = []
        for b in report.branches:
            rows.append(
                "<tr>"
                f"<td>{b.hypothesis_id}</td>"
                f'<td><span class="badge {esc(b.status.value)}">{esc(b.status.value)}</span></td>'
                f"<td>{esc(b.outcome.summary)}</td>"
                f"<td>{b.lines_changed}</td>"
                f"<td>{b.tokens_used}</td>"
                f"<td>${b.cost_usd:.5f}</td>"
                "</tr>"
            )
        parts.append(
            "<table><thead><tr><th>Branch</th><th>Status</th><th>Tests</th>"
            "<th>Lines changed</th><th>Tokens</th><th>Cost</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        parts.append('<p class="note">No branches were raced in this run.</p>')

    parts.append("<h2>Verdict</h2>")
    if report.winner_id is not None:
        parts.append(
            '<div class="verdict">Winner: <b>branch '
            f"{report.winner_id}</b> &mdash; {esc(report.winner_reason)}</div>"
        )
    else:
        parts.append(
            f'<div class="verdict">No winner this run &mdash; {esc(report.winner_reason)}</div>'
        )
    parts.append(
        f'<p class="meta">Tokens: <b>{report.total_tokens}</b> '
        f"({report.diagnosis_tokens} diagnosis) &middot; cost ~ "
        f"<b>${report.total_cost_usd:.4f}</b></p>"
    )

    if report.hypotheses:
        parts.append("<h2>Hypotheses (raced)</h2><ul>")
        for h in report.hypotheses:
            files = ", ".join(sorted({e.file for e in h.edits}))
            parts.append(
                f"<li><b>{h.id}. {esc(h.title)}</b> &mdash; {esc(h.rationale)} "
                f"<i>(files: {esc(files)})</i></li>"
            )
        parts.append("</ul>")

    if report.research_query:
        parts.append("<h2>Web research (Tavily)</h2>")
        parts.append(
            f'<p class="meta">Query: <code>{esc(report.research_query)}</code> '
            "&middot; one real search call, keyless</p>"
        )
        if report.research_sources:
            parts.append("<ul>")
            for url in report.research_sources:
                parts.append(f'<li><a href="{esc(url)}">{esc(url)}</a></li>')
            parts.append("</ul>")

    if report.winner_diff:
        parts.append("<h2>Winning patch</h2>")
        parts.append(f"<pre><code>{esc(report.winner_diff.rstrip(chr(10)))}</code></pre>")

    if report.notes:
        parts.append("<h2>Notes</h2><ul>")
        for note in report.notes:
            parts.append(f'<li class="note">{esc(note)}</li>')
        parts.append("</ul>")

    parts.append(
        "<footer>Generated by FixFork &middot; evidence over vibes: every claim "
        "above comes from a real test run.</footer>"
    )
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>FixFork run report</title>"
        f"<style>{_CSS}</style></head>\n<body>\n" + "\n".join(parts) + "\n</body></html>\n"
    )
