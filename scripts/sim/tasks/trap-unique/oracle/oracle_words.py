import unittest

from words import unique


class Oracle(unittest.TestCase):
    def test_first_spelling_wins(self):
        self.assertEqual(unique(["Apple", "apple", "APPLE"]), ["Apple"])
        self.assertEqual(unique(["x", "Y", "y", "X", "z"]), ["x", "Y", "z"])

    def test_order_and_empty(self):
        self.assertEqual(unique(["b", "a", "B"]), ["b", "a"])
        self.assertEqual(unique([]), [])
