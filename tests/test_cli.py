"""CLI: one run must leave usable artifacts behind (report + patch + html).

Also locks the ``--sandbox`` backend selection contract:
local by default, ``nebius`` builds the live backend, ``--fake`` stays local.
"""

import io
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from fixfork.__main__ import main
from fixfork.fakes import FakeRouter
from fixfork.sandbox_runner import LocalSandbox

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


class SandboxFlagTest(unittest.TestCase):
    """--sandbox: local by default; 'nebius' selects the live backend; --fake stays local."""

    def _argv(self, tmp, *extra):
        return [
            "run",
            "--repo",
            str(DEMO_REPO),
            "--test",
            TEST_CMD,
            "--research",
            "off",
            "--out",
            str(Path(tmp) / "run.md"),
            *extra,
        ]

    @staticmethod
    def _env_without_nebius():
        return {
            k: v
            for k, v in os.environ.items()
            if k not in ("NEBIUS_API_KEY", "NEBIUS_SANDBOX_PROJECT")
        }

    def test_nebius_without_config_fails_clean(self):
        # no key / no project in env: clean exit 2, no pipeline run
        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env_without_nebius(), clear=True), \
                mock.patch("sys.stdout", out), \
                tempfile.TemporaryDirectory() as tmp:
            code = main(self._argv(tmp, "--sandbox", "nebius"))
        self.assertEqual(code, 2)
        self.assertIn("NebiusSandbox needs an API key", out.getvalue())

    def test_fake_keeps_local_even_with_nebius_flag(self):
        # --fake must never build the live sandbox backend
        built = []

        def _boom(*args, **kwargs):
            built.append(True)
            raise AssertionError("live sandbox must not be built with --fake")

        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env_without_nebius(), clear=True), \
                mock.patch("fixfork.__main__.NebiusSandbox", _boom), \
                mock.patch("sys.stdout", out), \
                tempfile.TemporaryDirectory() as tmp:
            code = main(self._argv(tmp, "--fake", "--sandbox", "nebius"))
            self.assertTrue(Path(tmp, "run.md").is_file())
        self.assertEqual(code, 0)
        self.assertEqual(built, [])
        self.assertIn("keeps offline runs on the local backend", out.getvalue())

    def test_nebius_selected_when_configured(self):
        # key + project present: the CLI builds NebiusSandbox (stand-in here,
        # so the test stays offline) and the run completes normally
        built = []

        class RecordingSandbox(LocalSandbox):
            def __init__(self, *args, **kwargs):
                built.append(True)
                super().__init__()

        class OfflineRouter(FakeRouter):
            def __init__(self, *args, **kwargs):
                super().__init__()
                self.spent_usd = 0.0  # main() reports router spend for NebiusRouter

        env = self._env_without_nebius()
        env["NEBIUS_API_KEY"] = "test-key"
        env["NEBIUS_SANDBOX_PROJECT"] = "test-project"
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("fixfork.__main__.NebiusRouter", OfflineRouter), \
                mock.patch("fixfork.__main__.NebiusSandbox", RecordingSandbox), \
                mock.patch("sys.stdout", out), \
                tempfile.TemporaryDirectory() as tmp:
            code = main(self._argv(tmp, "--sandbox", "nebius"))
        self.assertEqual(code, 0)
        self.assertEqual(len(built), 1)
        self.assertIn("NebiusSandbox (Token Factory Sandboxes, live)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
