import unittest

from money import split


class Oracle(unittest.TestCase):
    def test_parts_differ_by_at_most_one(self):
        self.assertEqual(split(11, 3), [4, 4, 3])
        self.assertEqual(split(7, 4), [2, 2, 2, 1])

    def test_sums_and_order(self):
        for total in range(40):
            for n in range(1, 7):
                parts = split(total, n)
                self.assertEqual(sum(parts), total)
                self.assertEqual(len(parts), n)
                self.assertLessEqual(max(parts) - min(parts), 1)
                self.assertEqual(parts, sorted(parts, reverse=True))

    def test_invalid(self):
        for args in ((10, 0), (10, -1), (-1, 2)):
            with self.assertRaises(ValueError):
                split(*args)
