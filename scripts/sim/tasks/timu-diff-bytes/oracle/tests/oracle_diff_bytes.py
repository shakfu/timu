"""Hidden regression test, from timu 3d78d04 tests/test_review_fixes.py."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from timu.graph import extract, head


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "a.txt").write_text("one\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "first")
    return Path(os.path.realpath(path))


def test_a_non_utf8_diff_does_not_raise(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    base = head(r)
    (r / "a.txt").write_bytes(b"caf\xe9\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "latin-1")
    assert "caf" in extract("git:diff", r, base)
