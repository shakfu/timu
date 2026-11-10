import unittest

from lru import LRU


class T(unittest.TestCase):
    def test_evicts_oldest(self):
        c = LRU(2)
        c.put("a", 1)
        c.put("b", 2)
        c.put("c", 3)
        self.assertIsNone(c.get("a"))
        self.assertEqual(len(c), 2)
