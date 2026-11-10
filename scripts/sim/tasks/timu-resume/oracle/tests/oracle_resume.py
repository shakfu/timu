"""Hidden regression test, from timu 3d78d04 tests/test_review_fixes.py."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from timu import Event
from timu.graph import parse_graph, provision, resumable, spec

NODE = '[nodes.a]\nrepo = "a"\nworkflow = "commands"\nobjective = "o"\n'
NODE += 'params = { steps = ["true"] }\n'


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


def test_resume_reruns_a_node_whose_project_moved_on(tmp_path: Path) -> None:
    r = repo(tmp_path / "a")
    g = parse_graph(tomllib.loads(NODE))
    copy = tmp_path / "copy"
    base, _ = provision(r, copy, "HEAD", "b")
    data = {
        "node": "a",
        "status": "done",
        "summary": "s",
        "branch": "b",
        "base": base,
        "head": base,
        "workdir": str(copy),
        "files": "",
        "spec": spec(g.nodes["a"], r),
        "outputs": [],
    }
    events = [Event("node_result", "r", "", "workflow", 0.0, data)]
    assert list(resumable(events, g, {"a": r})) == ["a"]
    git(r, "commit", "--allow-empty", "-qm", "newer")
    assert list(resumable(events, g, {"a": r})) == []
