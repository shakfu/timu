"""Workspaces and objectives shared by the live tests and the trace replay tests, so a
recorded trace replays against the same files it was recorded on."""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from pathlib import Path

from timu.events import Event
from timu.trace import substitute

TMP = "{TMP}"


@dataclass(frozen=True)
class Scenario:
    workflow: str
    objective: str
    files: dict[str, str]

    def build(self, repo: Path) -> Path:
        repo.mkdir(parents=True, exist_ok=True)
        for name, text in self.files.items():
            (repo / name).write_text(text)
        return repo


SCENARIOS = {
    "fix-review-calc": Scenario(
        "fix-review",
        "make test fails; fix calc.py",
        {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": (
                "import unittest\nfrom calc import add\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
            ),
            "Makefile": "test:\n\tpython3 -m unittest -q\n",
        },
    ),
    "lead-tomllib": Scenario(
        "lead",
        "facts.py: make tomllib_added() return the Python version, as 'X.Y', in which "
        "tomllib joined the standard library. Look it up in the official docs at "
        "https://docs.python.org/3/library/tomllib.html rather than relying on memory.",
        {"facts.py": 'def tomllib_added() -> str:\n    return "TODO"\n'},
    ),
}


def redact(events: list[Event], root: Path) -> list[Event]:
    """Replace root (the run's temp dir), $HOME and the username with placeholders.
    root goes first, so a path inside it becomes {TMP}/..., whatever contains it."""
    home = os.environ.get("HOME", "")
    pairs = [(os.path.realpath(root), TMP), (str(root), TMP)]
    pairs += [(os.path.realpath(home), "{HOME}"), (home, "{HOME}")] if home else []
    pairs.append((getpass.getuser(), "{USER}"))
    return substitute(events, pairs)


def restore(events: list[Event], root: Path) -> list[Event]:
    """Point {TMP} at a replay's own temp dir."""
    return substitute(events, [(TMP, os.path.realpath(root))])
