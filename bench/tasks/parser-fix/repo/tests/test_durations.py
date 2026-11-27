import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import durations  # noqa: E402


class TestParse(unittest.TestCase):
    def test_single_units(self):
        self.assertEqual(durations.parse("90s"), 90)
        self.assertEqual(durations.parse("5m"), 300)
        self.assertEqual(durations.parse("2d"), 172800)

    def test_combined_with_and_without_spaces(self):
        self.assertEqual(durations.parse("1h30m"), 5400)
        self.assertEqual(durations.parse("2d 4h"), 187200)
        self.assertEqual(durations.parse("  1m  1s "), 61)

    def test_garbage_is_refused(self):
        for bad in ("", "   ", "10", "h", "1x", "1h junk", "-1h", "1.5h"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    durations.parse(bad)


class TestFormat(unittest.TestCase):
    def test_largest_units_first(self):
        self.assertEqual(durations.format(5400), "1h30m")
        self.assertEqual(durations.format(90061), "1d1h1m1s")
        self.assertEqual(durations.format(0), "0s")
        self.assertEqual(durations.format(59), "59s")

    def test_round_trip(self):
        for n in (1, 60, 61, 3599, 3600, 86399, 86400, 1000000):
            with self.subTest(n=n):
                self.assertEqual(durations.parse(durations.format(n)), n)


if __name__ == "__main__":
    unittest.main()
