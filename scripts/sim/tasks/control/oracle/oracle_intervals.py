import unittest

from intervals import merge


class Oracle(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(merge([]), [])
        self.assertEqual(merge([(5, 6), (1, 2)]), [[1, 2], [5, 6]])
        self.assertEqual(merge([(1, 2), (2, 3)]), [[1, 3]])
        self.assertEqual(merge([(1, 10), (2, 3)]), [[1, 10]])
        given = [(1, 2)]
        merge(given)
        self.assertEqual(given, [(1, 2)])
