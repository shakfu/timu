import unittest

from money import split


class T(unittest.TestCase):
    def test_remainder(self):
        self.assertEqual(split(10, 3), [4, 3, 3])
