"""Failing tests for the demo module (this is the red baseline FixFork fixes)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tax import order_total  # noqa: E402


class OrderTotalTest(unittest.TestCase):
    def test_discount_reduces_total(self):
        self.assertLess(order_total([100.0], discount=0.10), 100.0)

    def test_vat_applied_after_discount(self):
        self.assertEqual(order_total([100.0], discount=0.10, vat_rate=0.10), 99.0)


if __name__ == "__main__":
    unittest.main()
