"""Tests for the referee guard: edits to test/CI/config files are refused."""

import unittest

from fixfork.guard import (
    check_edits,
    describe_violations,
    diff_violations,
    protected_reason,
)
from fixfork.models import Edit


class ProtectedReasonTest(unittest.TestCase):
    def test_test_files_are_protected(self):
        for path in (
            "tests/test_tax.py",
            "test_utils.py",
            "src/test_helpers.py",
            "pkg/foo_test.py",
            "conftest.py",
            "specs/order_spec.py",
        ):
            self.assertIsNotNone(protected_reason(path), path)

    def test_ci_build_and_config_are_protected(self):
        for path in (
            ".github/workflows/ci.yml",
            ".gitlab-ci.yml",
            "pyproject.toml",
            "setup.cfg",
            "requirements-dev.txt",
            "run.sh",
            "Makefile",
            "Dockerfile",
        ):
            self.assertIsNotNone(protected_reason(path), path)

    def test_source_files_are_editable(self):
        for path in (
            "src/tax.py",
            "lib/model.py",
            "README.md",
            "src/deep/nested/core.py",
        ):
            self.assertIsNone(protected_reason(path), path)

    def test_escape_hatches_are_protected(self):
        self.assertIsNotNone(protected_reason("../outside.py"))
        self.assertIsNotNone(protected_reason("/etc/passwd"))
        self.assertIsNotNone(protected_reason(""))

    def test_check_edits_reports_file_and_reason(self):
        edits = [
            Edit(file="src/tax.py", find="a", replace="b"),
            Edit(file="tests/test_tax.py", find="c", replace="d"),
        ]
        violations = check_edits(edits)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0][0], "tests/test_tax.py")
        self.assertIn("tests/test_tax.py", describe_violations(violations))

    def test_diff_violations_flags_only_protected_paths(self):
        diff = (
            "diff --git a/src/tax.py b/src/tax.py\n"
            "--- a/src/tax.py\n"
            "+++ b/src/tax.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/tests/test_tax.py b/tests/test_tax.py\n"
            "--- a/tests/test_tax.py\n"
            "+++ b/tests/test_tax.py\n"
        )
        self.assertEqual(diff_violations(diff), ["tests/test_tax.py"])

    def test_diff_violations_catches_renames_from_protected_paths(self):
        # A rename FROM a protected path must be caught even though the target
        # name looks editable - both sides of the header are checked.
        diff = (
            "diff --git a/tests/test_tax.py b/src/renamed_from_tests.py\n"
            "similarity index 100%\n"
            "rename from tests/test_tax.py\n"
            "rename to src/renamed_from_tests.py\n"
        )
        self.assertEqual(diff_violations(diff), ["tests/test_tax.py"])

    def test_clean_diff_has_no_violations(self):
        diff = (
            "diff --git a/src/tax.py b/src/tax.py\n"
            "diff --git a/README.md b/README.md\n"
        )
        self.assertEqual(diff_violations(diff), [])


if __name__ == "__main__":
    unittest.main()
