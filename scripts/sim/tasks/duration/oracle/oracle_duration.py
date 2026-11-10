import unittest

from duration import parse_duration


class Oracle(unittest.TestCase):
    def test_valid(self):
        cases = {
            "1h30m": 5400,
            "45s": 45,
            "2h": 7200,
            "1h0m5s": 3605,
            "0s": 0,
            "90m": 5400,
            "10h10s": 36010,
            "007s": 7,
            "9999h": 35996400,
        }
        for given, want in cases.items():
            with self.subTest(given=given):
                got = parse_duration(given)
                self.assertEqual(got, want)
                self.assertIs(type(got), int)

    def test_invalid(self):
        for bad in [
            "",
            "12345s",
            "5",
            "h",
            "1m1h",
            "1h1h",
            "-1s",
            "1.5h",
            "1 h",
            "1h ",
            "1d",
            "s5",
            "1H",
            "\u0661s",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_duration(bad)
