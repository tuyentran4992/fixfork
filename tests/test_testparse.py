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


if __name__ == "__main__":
    unittest.main()
