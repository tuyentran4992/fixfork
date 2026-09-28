"""CLI: one run must leave usable artifacts behind (report + patch + html)."""

import tempfile
import unittest
from pathlib import Path

from fixfork.__main__ import main

DEMO_REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"
TEST_CMD = "python3 -m unittest discover -s tests"


class CliArtifactsTest(unittest.TestCase):
    def test_run_writes_report_patch_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run.md"
            code = main(
                [
                    "run",
                    "--repo",
                    str(DEMO_REPO),
                    "--test",
                    TEST_CMD,
                    "--fake",
                    "--out",
                    str(out),
                    "--html",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(out.is_file())

            patch = out.with_suffix(".patch")
            self.assertTrue(patch.is_file(), "patch artifact missing")
            patch_text = patch.read_text(encoding="utf-8")
            self.assertIn("diff --git a/src/tax.py b/src/tax.py", patch_text)
            self.assertIn("(1 - discount)", patch_text)

            html = out.with_suffix(".html")
            self.assertTrue(html.is_file(), "html artifact missing")
            self.assertIn("FixFork run report", html.read_text(encoding="utf-8"))

    def test_no_winner_writes_no_patch(self):
        # a repo whose baseline already passes: pipeline stops early, no patch
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "greenrepo"
            repo.mkdir()
            (repo / "test_ok.py").write_text(
                "import unittest\n\nclass T(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            out = Path(tmp) / "run.md"
            code = main(
                [
                    "run",
                    "--repo",
                    str(repo),
                    "--test",
                    "python3 -m unittest discover -s .",
                    "--fake",
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(code, 1)
            self.assertFalse(out.with_suffix(".patch").exists())


if __name__ == "__main__":
    unittest.main()
