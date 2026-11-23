import unittest

from versions import newer


class Oracle(unittest.TestCase):
    def test_missing_components_are_zero(self):
        self.assertFalse(newer("1.2.0", "1.2"))
        self.assertFalse(newer("1.2", "1.2.0"))
        self.assertTrue(newer("1.2.1", "1.2"))
        self.assertFalse(newer("1.2", "1.2.1"))

    def test_numeric(self):
        self.assertTrue(newer("1.10", "1.9"))
        self.assertFalse(newer("1.9", "1.10"))
        self.assertTrue(newer("2", "1.99.99"))
        self.assertFalse(newer("1.0", "1.0"))
