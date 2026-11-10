import unittest

from stats import clamp, mean, median


class Oracle(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)
        self.assertEqual(mean([4]), 4)
        self.assertAlmostEqual(mean([0.5, 1.5, 2.5, 3.5]), 2.0)

    def test_median_unchanged(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_clamp_unchanged(self):
        self.assertEqual(clamp(-1, 0, 3), 0)
        self.assertEqual(clamp(2, 0, 3), 2)
