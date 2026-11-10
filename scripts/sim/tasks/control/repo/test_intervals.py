import unittest

from intervals import merge


class T(unittest.TestCase):
    def test_overlap(self):
        self.assertEqual(merge([(1, 3), (2, 5), (7, 8)]), [[1, 5], [7, 8]])
