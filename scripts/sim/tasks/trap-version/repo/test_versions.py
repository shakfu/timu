import unittest

from versions import newer


class T(unittest.TestCase):
    def test_numeric(self):
        self.assertTrue(newer("1.10", "1.9"))
