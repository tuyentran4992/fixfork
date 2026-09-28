"""Report rendering: evidence lands in both markdown and HTML, escaped."""

import unittest

from fixfork.models import (
    BranchResult,
    BranchStatus,
    Edit,
    Hypothesis,
    RunReport,
    TestOutcome,
)
from fixfork.report import render_html, render_markdown, summary_dict


def sample_report() -> RunReport:
    report = RunReport(repo="/tmp/ff-demo", test_command="python3 -m unittest")
    report.baseline = TestOutcome(ok=False, passed=0, failed=2, summary="FAILED (2 of 2)")
    report.hypotheses = [
        Hypothesis(
            id=1,
            title="Sign flip <script>alert(1)</script>",
            rationale="discount applied as a surcharge",
            edits=[Edit(file="src/tax.py", find="(1 + discount)", replace="(1 - discount)")],
        ),
        Hypothesis(id=2, title="VAT removal", rationale="drop VAT", edits=[]),
        Hypothesis(id=3, title="Rounding", rationale="round earlier", edits=[]),
    ]
    report.branches = [
        BranchResult(
            hypothesis_id=1,
            status=BranchStatus.GREEN,
            outcome=TestOutcome(ok=True, passed=2, failed=0, summary="OK (2 tests)"),
            lines_changed=1,
            tokens_used=900,
            cost_usd=0.002,
        ),
        BranchResult(
            hypothesis_id=2,
            status=BranchStatus.RED,
            outcome=TestOutcome(ok=False, passed=1, failed=1, summary="FAILED (1 of 2)"),
            lines_changed=1,
            tokens_used=800,
            cost_usd=0.0015,
        ),
        BranchResult(hypothesis_id=3, status=BranchStatus.ERROR, log_tail="sandbox error: nope"),
    ]
    report.winner_id = 1
    report.winner_reason = "only green branch"
    report.winner_diff = (
        "diff --git a/src/tax.py b/src/tax.py\n"
        "--- a/src/tax.py\n"
        "+++ b/src/tax.py\n"
        "@@ -7,7 +7,7 @@\n"
        "-    discounted = subtotal * (1 + discount)\n"
        "+    discounted = subtotal * (1 - discount)\n"
    )
    report.notes = ["diagnosis model proposed 3 hypotheses"]
    report.diagnosis_tokens = 1200
    report.diagnosis_cost_usd = 0.003
    return report


class RenderHtmlTest(unittest.TestCase):
    def test_html_carries_the_evidence(self):
        html = render_html(sample_report())
        self.assertIn("branch 1", html)
        self.assertIn("badge green", html)
        self.assertIn("FAILED (2 of 2)", html)
        self.assertIn("1 - discount", html)
        self.assertIn("$0.0065", html)  # 0.002 + 0.0015 + 0.003
        self.assertIn("diagnosis model proposed 3 hypotheses", html)

    def test_html_escapes_model_output(self):
        html = render_html(sample_report())
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_html_without_winner_still_renders(self):
        report = RunReport(repo="/tmp/x", test_command="pytest")
        report.winner_reason = "no branch passed"
        html = render_html(report)
        self.assertIn("No winner this run", html)


class RenderMarkdownTest(unittest.TestCase):
    def test_markdown_has_verdict_and_diff(self):
        md = render_markdown(sample_report())
        self.assertIn("## Verdict", md)
        self.assertIn("Winner: **branch 1**", md)
        self.assertIn("diff --git a/src/tax.py", md)
        self.assertIn("| 1 | green |", md)

    def test_summary_dict_keys(self):
        data = summary_dict(sample_report())
        self.assertEqual(data["winner_id"], 1)
        self.assertEqual(len(data["branches"]), 3)
        self.assertEqual(data["cost_usd_total"], 0.0065)


if __name__ == "__main__":
    unittest.main()
