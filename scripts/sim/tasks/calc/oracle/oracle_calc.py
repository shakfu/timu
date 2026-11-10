import unittest

from calc import add, mul


class Oracle(unittest.TestCase):
    def test_add(self):
        for a, b in [(2, 3), (-1, 1), (0, 0), (1.5, 2.25), (-4, -6)]:
            self.assertEqual(add(a, b), a + b)

    def test_mul_unchanged(self):
        self.assertEqual(mul(3, -4), -12)
