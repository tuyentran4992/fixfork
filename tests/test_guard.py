"""Tests for the referee guard: edits to test/CI/config files are refused."""

import re
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

    def test_case_variant_conventions_are_protected(self):
        # Real repositories use capitalized conventions: python-pillow/Pillow
        # keeps its whole suite in "Tests/" (verified via api.github.com at
        # commit b25d85356f296103f22c0127358f91536c53b399, 2026-09-30) and JS
        # projects use "__tests__/". A case-sensitive match would silently
        # leave those suites unprotected.
        for path in (
            "Tests/helper.py",  # real Pillow file, missed by the old patterns
            "Tests/fonts/FreeMono.ttf",  # real Pillow subdirectory
            "Tests/test_image.py",
            "Test_Helpers.PY",
            "src/pkg/Foo_Test.py",
            "__tests__/widget.test.js",
            "testdata/golden_output.json",
            "TESTDATA/expected.txt",
        ):
            self.assertIsNotNone(protected_reason(path), path)

    # Pre-fix pattern list, copied verbatim from commit 19e7716 (before the
    # case-insensitive fix). Kept so the regression test below can prove the
    # old guard really did miss these paths: a new check that cannot show the
    # old one failing is decoration.
    _OLD_CASE_SENSITIVE = [
        (r"(^|/)(tests?|testing|spec|specs)(/|$)", "test dir"),
        (r"(^|/)test_[^/]*\.py$", "test file"),
        (r"(^|/)[^/]*_test\.py$", "test file"),
        (r"(^|/)conftest\.py$", "test config"),
        (
            r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|noxfile\.py|\.pre-commit-config\.yaml)$",
            "test/build config",
        ),
        (r"(^|/)\.github/", "ci"),
        (r"(^|/)\.gitlab-ci\.yml$|(^|/)\.circleci/", "ci"),
        (r"(^|/)(Makefile|makefile|Dockerfile)$", "build"),
        (r"\.(sh|bash|yml|yaml|ini|cfg|toml)$", "config"),
        (r"(^|/)requirements[^/]*\.txt$", "deps"),
        (r"^/", "absolute path"),
        (r"(^|/)\.\.(/|$)", "escape"),
    ]

    def test_case_variants_were_unprotected_before_the_fix(self):
        # Locks the fix's discriminating power: with the old case-sensitive
        # patterns these paths returned None (measured), while the current
        # guard must protect them.
        for path in (
            "Tests/helper.py",
            "Tests/fonts/FreeMono.ttf",
            "TESTDATA/expected.txt",
            "__tests__/widget.test.js",
            "testdata/golden_output.json",
        ):
            old_hit = next(
                (
                    reason
                    for pattern, reason in self._OLD_CASE_SENSITIVE
                    if re.search(pattern, path)
                ),
                None,
            )
            self.assertIsNone(old_hit, "old patterns unexpectedly caught " + path)
            self.assertIsNotNone(protected_reason(path), path)

    def test_case_insensitivity_keeps_ordinary_sources_editable(self):
        # Case-insensitive matching must not block ordinary source files whose
        # names merely contain "test"/"spec" as a substring.
        for path in (
            "src/testparse.py",
            "docs/testing-guide.md",
            "contest/app.py",
            "spec_helper_notes.md",
        ):
            self.assertIsNone(protected_reason(path), path)

    def test_diff_violations_catches_case_variant_test_dir(self):
        # Last-resort shield: a diff header for Tests/helper.py (Pillow's real
        # suite location) must be flagged; the pre-fix shield missed it.
        diff = (
            "diff --git a/Tests/helper.py b/Tests/helper.py\n"
            "--- a/Tests/helper.py\n"
            "+++ b/Tests/helper.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        self.assertEqual(diff_violations(diff), ["Tests/helper.py"])

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

    def test_diff_violations_handles_quoted_paths(self):
        # Git C-quotes paths with spaces; quoting can differ per side.
        same = (
            'diff --git "a/tests/test with space.py" "b/tests/test with space.py"\n'
        )
        self.assertEqual(diff_violations(same), ["tests/test with space.py"])
        mixed = 'diff --git a/src/x.py "b/tests/quoted target.py"\n'
        self.assertEqual(diff_violations(mixed), ["tests/quoted target.py"])

    def test_clean_diff_has_no_violations(self):
        diff = (
            "diff --git a/src/tax.py b/src/tax.py\n"
            "diff --git a/README.md b/README.md\n"
        )
        self.assertEqual(diff_violations(diff), [])


if __name__ == "__main__":
    unittest.main()
