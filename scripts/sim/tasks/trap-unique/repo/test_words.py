import unittest

from words import unique


class T(unittest.TestCase):
    def test_order(self):
        self.assertEqual(unique(["b", "a", "B"]), ["b", "a"])
