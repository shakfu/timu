import unittest

from text import slugify


class Oracle(unittest.TestCase):
    def test_rules(self):
        cases = {
            "Hello World": "hello-world",
            "  Hello,   World!  ": "hello-world",
            "Caf\u00e9 d\u00e9j\u00e0 vu": "cafe-deja-vu",
            "a--b__c": "a-b-c",
            "---": "",
            "": "",
            "\u00dcn\u00efc\u00f6d\u00e9 2026": "unicode-2026",
            "snake_case and CamelCase": "snake-case-and-camelcase",
            "\u65e5\u672c tokyo": "tokyo",
            "\uff21\uff22\uff23 x": "x",
            "\u2460 2": "2",
            "\ufb01ne": "ne",
            "\u212aey": "key",
            "\u01ff x": "x",
            "\u00f8\u00e9": "e",
        }
        for given, want in cases.items():
            with self.subTest(given=given):
                self.assertEqual(slugify(given), want)
