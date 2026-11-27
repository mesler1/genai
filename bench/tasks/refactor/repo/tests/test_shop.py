import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shop import cart, discount, invoice  # noqa: E402

LINES = [("apple", 50, 4), ("bread", 325, 1)]


class TestShop(unittest.TestCase):
    def test_total_includes_tax(self):
        self.assertEqual(cart.calc_total(LINES), 630)

    def test_add_merges_the_same_item(self):
        self.assertEqual(cart.add([("apple", 50, 1)], "apple", 50, 2), [("apple", 50, 3)])

    def test_invoice(self):
        text = invoice.invoice(LINES)
        self.assertIn("tax (20%)", text)
        self.assertTrue(text.rstrip().endswith("6.30"))

    def test_discount(self):
        self.assertEqual(discount.with_discount(LINES, 10), 567)
        self.assertEqual(discount.saving(LINES, 10), 63)


if __name__ == "__main__":
    unittest.main()
