"""Tests for parsing test-runner output."""

import unittest

from fixfork.testparse import parse_unittest_output

OK_OUTPUT = """
test_a (test_x.T) ... ok
test_b (test_x.T) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.001s

OK
"""

FAILED_OUTPUT = """
test_a (test_x.T) ... FAIL
test_b (test_x.T) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.001s

FAILED (failures=1)
"""

MIXED_OUTPUT = """
Ran 4 tests in 0.002s

FAILED (failures=2, errors=1)
"""

PYTEST_FAILED = """
tests/test_filesize.py ......F.......F [ 92%]
=================================== FAILURES ===================================
FAILED tests/test_filesize.py::test_naturalsize_binary_unit_boundary
6 failed, 70 passed in 0.20s
"""

PYTEST_OK = """

76 passed in 0.14s
"""

PYTEST_ERROR = """
ERROR: file or directory not found: tests/test_filesize.py
1 error in 0.05s
"""


class ParseUnittestOutputTest(unittest.TestCase):
    def test_ok(self):
        outcome = parse_unittest_output(OK_OUTPUT, returncode=0)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.passed, 2)
        self.assertEqual(outcome.failed, 0)

    def test_failed(self):
        outcome = parse_unittest_output(FAILED_OUTPUT, returncode=1)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.passed, 1)
        self.assertEqual(outcome.failed, 1)

    def test_failures_plus_errors(self):
        outcome = parse_unittest_output(MIXED_OUTPUT, returncode=1)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failed, 3)
        self.assertEqual(outcome.passed, 1)

    def test_unknown_format_falls_back_to_exit_code(self):
        outcome = parse_unittest_output("some other runner output", returncode=7)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failed, 1)

    def test_pytest_failed_summary(self):
        outcome = parse_unittest_output(PYTEST_FAILED, returncode=1)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.passed, 70)
        self.assertEqual(outcome.failed, 6)
        self.assertEqual(outcome.summary, "FAILED (6 of 76)")

    def test_pytest_ok_summary(self):
        outcome = parse_unittest_output(PYTEST_OK, returncode=0)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.passed, 76)
        self.assertEqual(outcome.summary, "OK (76 tests)")

    def test_pytest_collection_error(self):
        outcome = parse_unittest_output(PYTEST_ERROR, returncode=2)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failed, 1)

    def test_last_pytest_summary_wins_over_earlier_count_lines(self):
        # counts are not merged across qualifying lines (merging would
        # double-count); the LAST summary line wins
        text = "2 failed in 0.10s\nprogress...\n6 failed, 70 passed in 0.20s\n"
        outcome = parse_unittest_output(text, returncode=1)
        self.assertEqual(outcome.failed, 6)
        self.assertEqual(outcome.passed, 70)
        self.assertEqual(outcome.summary, "FAILED (6 of 76)")

    def test_unittest_ok_still_wins_over_pytest_words(self):
        outcome = parse_unittest_output(OK_OUTPUT, returncode=0)
        self.assertEqual(outcome.summary, "OK (2 tests)")


if __name__ == "__main__":
    unittest.main()
