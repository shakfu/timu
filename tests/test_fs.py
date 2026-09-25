"""Tests for the filesystem tools and their root checks."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from timu import Context, ToolOutput
from timu.tools import fs
from timu.tools.fs import EDIT, LIST, READ, SEARCH, WRITE

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A workspace with a few files, and a sibling directory outside it."""
    work = tmp_path / "ws"
    (work / "src" / "pkg").mkdir(parents=True)
    (work / "a.txt").write_text("one\ntwo\nthree\n")
    (work / "src" / "main.py").write_text("def main():\n    return 1\n")
    (work / "src" / "pkg" / "util.py").write_text("X = 1\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret\n")
    return Path(os.path.realpath(work))


def context(ws: Path, **kw: Any) -> Context:
    kw.setdefault("read_roots", (ws,))
    kw.setdefault("write_roots", (ws,))
    return Context(workdir=ws, cancel=threading.Event(), **kw)


Run = Callable[..., ToolOutput]


@pytest.fixture
def run(ws: Path) -> Run:
    """run(tool, ctx=None, **args): call a tool with a read-write context by default."""

    def call(tool: Any, ctx: Context | None = None, **args: Any) -> ToolOutput:
        return tool.run(ctx or context(ws), args)  # type: ignore[no-any-return]

    return call


def ok(out: ToolOutput) -> str:
    assert not out.is_error, out.text
    return out.text


def err(out: ToolOutput) -> str:
    assert out.is_error, out.text
    return out.text


# ---- read ----


def test_read(run: Run) -> None:
    assert ok(run(READ, path="a.txt")) == "one\ntwo\nthree\n"


def test_read_offset_limit(run: Run) -> None:
    assert ok(run(READ, path="a.txt", offset=2, limit=1)) == "two\n[lines 2-2 of 3]"
    assert ok(run(READ, path="a.txt", offset=2)) == "two\nthree\n[lines 2-3 of 3]"
    assert "past the end" in ok(run(READ, path="a.txt", offset=9))


def test_read_keeps_crlf(ws: Path, run: Run) -> None:
    (ws / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    assert ok(run(READ, path="crlf.txt")) == "a\r\nb\r\n"


def test_read_refusals(ws: Path, run: Run) -> None:
    (ws / "bin").write_bytes(b"abc\0def")
    assert "is binary" in err(run(READ, path="bin"))
    assert "is a directory" in err(run(READ, path="src"))
    assert "No such file" in err(run(READ, path="missing.txt"))
    assert "positive integer" in err(run(READ, path="a.txt", offset=0))
    assert "positive integer" in err(run(READ, path="a.txt", limit=True))
    assert "must be a string" in err(run(READ, path=3))


@pytest.mark.parametrize(
    "path", ["../outside/secret.txt", "src/../../outside/secret.txt"]
)
def test_read_dotdot_escape(run: Run, path: str) -> None:
    assert "outside the readable roots" in err(run(READ, path=path))


def test_read_absolute_outside(ws: Path, run: Run) -> None:
    assert "outside the readable roots" in err(
        run(READ, path=str(ws.parent / "outside/secret.txt"))
    )


def test_read_symlink_escape(ws: Path, run: Run) -> None:
    (ws / "link.txt").symlink_to(ws.parent / "outside" / "secret.txt")
    (ws / "linkdir").symlink_to(ws.parent / "outside")
    assert "outside the readable roots" in err(run(READ, path="link.txt"))
    assert "outside the readable roots" in err(run(READ, path="linkdir/secret.txt"))


def test_read_symlink_inside_is_allowed(ws: Path, run: Run) -> None:
    (ws / "alias.txt").symlink_to(ws / "a.txt")
    assert ok(run(READ, path="alias.txt")).startswith("one")


def test_read_without_roots(ws: Path, run: Run) -> None:
    assert "outside the readable roots" in err(
        run(READ, context(ws, read_roots=()), path="a.txt")
    )


# ---- list ----


def test_list_directory(ws: Path, run: Run) -> None:
    (ws / ".git").mkdir()
    assert ok(run(LIST)) == "a.txt\nsrc/"
    assert ok(run(LIST, path="src")) == "src/main.py\nsrc/pkg/"


def test_list_pattern(run: Run) -> None:
    assert ok(run(LIST, pattern="*.py")) == "src/main.py\nsrc/pkg/util.py"
    assert ok(run(LIST, pattern="**/*.py")) == "src/main.py\nsrc/pkg/util.py"
    assert ok(run(LIST, pattern="src/pkg/*")) == "src/pkg/util.py"
    assert ok(run(LIST, pattern="*.rs")) == "no entries"


def test_list_pattern_skips_links_outside(ws: Path, run: Run) -> None:
    (ws / "leak.py").symlink_to(ws.parent / "outside" / "secret.txt")
    assert "leak.py" not in ok(run(LIST, pattern="*.py"))


def test_list_refusals(run: Run) -> None:
    assert "not a directory" in err(run(LIST, path="a.txt"))
    assert "outside the readable roots" in err(run(LIST, path=".."))


def test_list_cap(ws: Path, run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "MAX_ENTRIES", 2)
    assert ok(run(LIST, pattern="*")) == "a.txt\nsrc/main.py\n[1 more not shown]"


@needs_git
def test_list_skips_gitignored(ws: Path, run: Run) -> None:
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / ".gitignore").write_text("*.log\nbuild/\n")
    (ws / "debug.log").write_text("x\n")
    (ws / "build").mkdir()
    (ws / "build" / "out.py").write_text("x\n")
    listed = ok(run(LIST, pattern="*")).splitlines()
    assert "debug.log" not in listed
    assert "build/out.py" not in listed
    assert "src/main.py" in listed
    assert not any(p.startswith(".git/") for p in listed)


# ---- search ----


def test_search(run: Run) -> None:
    assert ok(run(SEARCH, pattern=r"return \d")) == "src/main.py:2:    return 1"
    assert ok(run(SEARCH, pattern="^x", ignore_case=True)) == "src/pkg/util.py:1:X = 1"
    assert ok(run(SEARCH, pattern="^x")) == "no matches"


def test_search_path_and_glob(run: Run) -> None:
    assert ok(run(SEARCH, pattern="1", path="src/pkg")) == "src/pkg/util.py:1:X = 1"
    assert ok(run(SEARCH, pattern="t", glob="*.txt")) == "a.txt:2:two\na.txt:3:three"


def test_search_skips_binary(ws: Path, run: Run) -> None:
    (ws / "bin").write_bytes(b"return 1\0")
    assert "bin" not in ok(run(SEARCH, pattern="return"))


def test_search_bad_regex(run: Run) -> None:
    assert "invalid regex" in err(run(SEARCH, pattern="("))


def test_search_cap(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "MAX_MATCHES", 2)
    lines = ok(run(SEARCH, pattern=".")).splitlines()
    assert lines[-1] == "[stopped after 2 matches]"
    assert len(lines) == 3


def test_search_outside(run: Run) -> None:
    assert "outside the readable roots" in err(
        run(SEARCH, pattern="secret", path="../outside")
    )


@needs_git
def test_search_skips_gitignored(ws: Path, run: Run) -> None:
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / ".gitignore").write_text("*.log\n")
    (ws / "debug.log").write_text("needle\n")
    (ws / "b.txt").write_text("needle\n")
    assert ok(run(SEARCH, pattern="needle")) == "b.txt:1:needle"


# ---- write ----


def test_write_new_file_with_parents(ws: Path, run: Run) -> None:
    assert (
        ok(run(WRITE, path="new/dir/f.txt", content="hi"))
        == "wrote new/dir/f.txt (2 bytes)"
    )
    assert (ws / "new/dir/f.txt").read_text() == "hi"


def test_write_replaces_and_keeps_mode(ws: Path, run: Run) -> None:
    target = ws / "a.txt"
    target.chmod(0o640)
    ok(run(WRITE, path="a.txt", content="new"))
    assert target.read_text() == "new"
    assert target.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt", "src"]  # no temp left


def test_failed_write_keeps_original(
    ws: Path, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*a: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    assert "No space left" in err(run(WRITE, path="a.txt", content="new"))
    assert (ws / "a.txt").read_text() == "one\ntwo\nthree\n"
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt", "src"]


@pytest.mark.parametrize(
    "path", ["../outside/secret.txt", "src/../../outside/x.txt", "/tmp/timu-test-x.txt"]
)
def test_write_outside(run: Run, path: str) -> None:
    assert "outside the writable roots" in err(run(WRITE, path=path, content="x"))


def test_write_through_symlink_outside(ws: Path, run: Run) -> None:
    (ws / "link.txt").symlink_to(ws.parent / "outside" / "secret.txt")
    (ws / "dangling.txt").symlink_to(ws.parent / "outside" / "new.txt")
    assert "outside the writable roots" in err(run(WRITE, path="link.txt", content="x"))
    assert "outside the writable roots" in err(
        run(WRITE, path="dangling.txt", content="x")
    )
    assert (ws.parent / "outside" / "secret.txt").read_text() == "secret\n"
    assert not (ws.parent / "outside" / "new.txt").exists()


def test_write_refusals(ws: Path, run: Run) -> None:
    (ws / ".git").mkdir()
    assert ".git are not allowed" in err(run(WRITE, path=".git/config", content="x"))
    assert ".git are not allowed" in err(run(WRITE, path=".GIT/config", content="x"))
    assert "is a directory" in err(run(WRITE, path="src", content="x"))
    read_only = context(ws, write_roots=())
    assert "outside the writable roots" in err(
        run(WRITE, read_only, path="a.txt", content="x")
    )


@pytest.mark.parametrize(
    "path", ["timu.toml", "TIMU.toml", ".timu/skills/s/SKILL.md", ".Timu/roles/r.md"]
)
def test_write_refuses_files_later_runs_read(ws: Path, run: Run, path: str) -> None:
    assert "agents may not write it" in err(run(WRITE, path=path, content="x"))
    assert not (ws / "timu.toml").exists()
    assert not (ws / ".timu").exists()


def test_write_allows_protected_names_below_the_root(ws: Path, run: Run) -> None:
    ok(run(WRITE, path="src/timu.toml", content="x"))
    ok(run(WRITE, path="src/.timu/x", content="x"))


# ---- edit ----


def test_edit(ws: Path, run: Run) -> None:
    assert (
        ok(run(EDIT, path="a.txt", old_string="two", new_string="2")) == "edited a.txt"
    )
    assert (ws / "a.txt").read_text() == "one\n2\nthree\n"


def test_edit_refusals(ws: Path, run: Run) -> None:
    (ws / "dup.txt").write_text("x x\n")
    (ws / "bin").write_bytes(b"x\0")
    e = "edit"
    assert "not found" in err(run(EDIT, path="a.txt", old_string="four", new_string=e))
    assert "occurs 2 times" in err(
        run(EDIT, path="dup.txt", old_string="x", new_string=e)
    )
    assert "is empty" in err(run(EDIT, path="a.txt", old_string="", new_string=e))
    assert "no such file" in err(
        run(EDIT, path="nope.txt", old_string="x", new_string=e)
    )
    assert "is binary" in err(run(EDIT, path="bin", old_string="x", new_string=e))
    assert "outside the writable roots" in err(
        run(EDIT, path="../outside/secret.txt", old_string="secret", new_string=e)
    )
    assert (ws / "dup.txt").read_text() == "x x\n"


# ---- single-file write roots ----


def test_file_root(ws: Path, run: Run) -> None:
    report = context(ws, write_roots=(), write_files=(ws / "REVIEW.md",))
    assert ok(run(WRITE, report, path="REVIEW.md", content="# Review\n"))
    assert ok(run(WRITE, report, path="REVIEW.md", content="# Review v2\n"))
    assert (ws / "REVIEW.md").read_text() == "# Review v2\n"
    for other in ("REVIEW.txt", "a.txt", "src/REVIEW.md", "REVIEW.md/x"):
        assert "outside the writable roots" in err(
            run(WRITE, report, path=other, content="x")
        )


def test_file_root_symlink_is_refused(ws: Path, run: Run) -> None:
    report = context(ws, write_roots=(), write_files=(ws / "REVIEW.md",))
    (ws / "REVIEW.md").symlink_to(ws / "src" / "main.py")
    assert "is a symlink" in err(run(WRITE, report, path="REVIEW.md", content="x"))
    assert (ws / "src" / "main.py").read_text().startswith("def main")
