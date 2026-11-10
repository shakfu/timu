import unittest

from lru import LRU


class Oracle(unittest.TestCase):
    def test_capacity(self):
        c = LRU(3)
        for i in range(10):
            c.put(i, i)
            self.assertLessEqual(len(c), 3)
        self.assertEqual([c.get(i) for i in range(7, 10)], [7, 8, 9])

    def test_get_refreshes(self):
        c = LRU(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertEqual(c.get("a"), 1)
        c.put("c", 3)
        self.assertEqual(c.get("a"), 1)
        self.assertIsNone(c.get("b"))

    def test_put_existing_refreshes(self):
        c = LRU(2)
        c.put("a", 1)
        c.put("b", 2)
        c.put("a", 10)
        c.put("c", 3)
        self.assertEqual(c.get("a"), 10)
        self.assertIsNone(c.get("b"))

    def test_default(self):
        self.assertEqual(LRU(1).get("x", 7), 7)
