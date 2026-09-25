"""Tests for role validation and context building (design 4.4)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from timu import Agent, Capability, Role, Task, Tool, ToolOutput
from timu.provider.fake import FakeProvider, call, calls, text
from timu.role import RoleError, make_context, validate
from timu.tools.fs import EDIT, LIST, READ, SEARCH, WRITE

R, W, X, N = Capability.FS_READ, Capability.FS_WRITE, Capability.EXEC, Capability.NET


def _tool(name: str, *needs: Capability) -> Tool:
    return Tool(
        name, "", {"type": "object"}, frozenset(needs), lambda ctx, args: ToolOutput("")
    )


def test_valid_role() -> None:
    validate(
        Role("coder", "", (READ, WRITE, EDIT), frozenset({R, W}), write_roots=(".",))
    )


def test_tool_needs_ungranted_capability() -> None:
    with pytest.raises(RoleError, match="tool write needs fs.write"):
        validate(Role("r", "", (READ, WRITE), frozenset({R})))


@pytest.mark.parametrize("grants", [{N, W}, {N, X}, {N, W, X, R}])
def test_net_with_write_or_exec(grants: set[Capability]) -> None:
    with pytest.raises(RoleError, match="net cannot be granted"):
        validate(Role("r", "", (), frozenset(grants)))


def test_net_alone_and_with_read_is_allowed() -> None:
    validate(Role("researcher", "", (_tool("web_fetch", N),), frozenset({N})))
    validate(Role("r", "", (), frozenset({N, R})))


def test_duplicate_tools() -> None:
    with pytest.raises(RoleError, match="duplicate tools: read"):
        validate(Role("r", "", (READ, READ), frozenset({R})))


def test_write_roots_need_fs_write() -> None:
    with pytest.raises(RoleError, match="without the fs.write grant"):
        validate(Role("r", "", (), frozenset({R}), write_roots=(".",)))
    with pytest.raises(RoleError, match="without the fs.write grant"):
        validate(Role("r", "", (), frozenset({R}), write_files=("REVIEW.md",)))


def test_make_context_resolves_roots(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    role = Role(
        "r",
        "",
        (READ, WRITE),
        frozenset({R, W}),
        read_roots=(".",),
        write_roots=("docs",),
        write_files=("REVIEW.md",),
        max_output=1234,
    )
    ctx = make_context(role, tmp_path, threading.Event())
    real = Path(os.path.realpath(tmp_path))
    assert ctx.workdir == real
    assert ctx.read_roots == (real,)
    assert ctx.write_roots == (real / "docs",)
    assert ctx.write_files == (real / "REVIEW.md",)
    assert ctx.max_output == 1234


def test_read_roots_dropped_without_fs_read(tmp_path: Path) -> None:
    role = Role("researcher", "", (), frozenset({N}))
    assert make_context(role, tmp_path, threading.Event()).read_roots == ()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing/REVIEW.md", "parent of missing/REVIEW.md does not exist"),
        ("../REVIEW.md", "outside the workdir"),
        ("sub", "is a directory"),
        ("link.md", "is a symlink"),
    ],
)
def test_bad_file_roots(tmp_path: Path, path: str, message: str) -> None:
    work = tmp_path / "ws"
    (work / "sub").mkdir(parents=True)
    (work / "link.md").symlink_to(work / "sub")
    role = Role("r", "", (WRITE,), frozenset({W}), write_files=(path,))
    with pytest.raises(RoleError, match=message):
        make_context(role, work, threading.Event())


def test_agent_construction_validates(tmp_path: Path) -> None:
    bad = Role("r", "", (WRITE,), frozenset({R}))
    with pytest.raises(RoleError):
        Agent(bad, FakeProvider([]), lambda e: None, tmp_path)


def test_agent_edits_inside_and_cannot_touch_outside(tmp_path: Path) -> None:
    """Phase 2 exit: a scripted agent edits a file in its workdir, but its attempts on a
    file outside fail and leave that file unchanged."""
    work, outside = tmp_path / "ws", tmp_path / "outside.txt"
    work.mkdir()
    (work / "app.py").write_text("VALUE = 1\n")
    outside.write_text("keep\n")
    role = Role(
        "coder",
        "",
        (READ, LIST, SEARCH, WRITE, EDIT),
        frozenset({R, W}),
        write_roots=(".",),
    )
    provider = FakeProvider(
        [
            calls(call("search", pattern="VALUE")),
            calls(
                call(
                    "edit",
                    path="app.py",
                    old_string="VALUE = 1",
                    new_string="VALUE = 2",
                )
            ),
            calls(call("write", path="../outside.txt", content="pwned")),
            calls(
                call("edit", path=str(outside), old_string="keep", new_string="pwned")
            ),
            text("done"),
        ]
    )
    result = Agent(role, provider, lambda e: None, work).run(Task("set VALUE to 2"))

    assert result.status == "done"
    assert (work / "app.py").read_text() == "VALUE = 2\n"
    assert outside.read_text() == "keep\n"
    tool_msgs = [m for m in provider.requests[-1].messages if m.role == "tool"]
    assert [m.is_error for m in tool_msgs] == [False, False, True, True]
    assert tool_msgs[0].content == "app.py:1:VALUE = 1"
