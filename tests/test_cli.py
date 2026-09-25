"""Tests for `timu run`."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from timu import Usage
from timu.cli import ConsoleSink, main
from timu.events import Event
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.sandbox import MacSandbox, detect

mac_only = pytest.mark.skipif(
    not isinstance(detect(), MacSandbox), reason="needs sandbox-exec"
)


@pytest.fixture(autouse=True)
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Traces go to a temp state dir."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


def config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "timu.toml"
    path.write_text(f'[provider]\nmodel = "m"\napi_key_env = ""\n{extra}')
    return path


def cli(
    tmp_path: Path, argv: list[str], replies: list[Reply], **kw: Any
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    provider = FakeProvider(replies)
    args = ["run", "--config", str(config(tmp_path, kw.pop("toml", ""))), *argv]
    code = main(args, provider_for=lambda role: provider, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a seeded bug and a Makefile test target."""
    r = tmp_path / "repo"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    (r / "Makefile").write_text("test:\n\tpython3 -m unittest -q\n")
    return r


FIX = [
    calls(call("read", path="calc.py")),
    calls(call("edit", path="calc.py", old_string="a - b", new_string="a + b")),
    calls(call("shell", command="make test")),
    text("Fixed add() in calc.py; make test passes."),
]
REVIEW = [
    calls(call("shell", command="make test")),
    text("VERDICT: APPROVE\nadd() now adds; make test passes."),
]


@mac_only
def test_run_fixes_a_seeded_bug(tmp_path: Path, repo: Path, state: Path) -> None:
    """Phase 6 exit: `timu run` fixes a seeded bug, and the reviewer confirms the tests
    pass. Both run `make test` for real, in the sandbox."""
    prefixes = sorted({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix)})
    toml = f"[sandbox]\nextra_read = {json.dumps(prefixes)}\n"
    code, out, err = cli(
        tmp_path, ["-C", str(repo), "fix the bug in add()"], FIX + REVIEW, toml=toml
    )

    assert code == 0, err
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert (repo / "REVIEW.md").read_text().startswith("VERDICT: APPROVE")
    assert "timu: done: approved in round 1" in err
    assert "Fixed add() in calc.py" in out
    traces = list((state / "timu" / "runs").glob("*.jsonl"))
    assert len(traces) == 1
    events = [json.loads(line) for line in traces[0].read_text().splitlines()]
    shells = [
        e
        for e in events
        if e["kind"] == "tool_result" and "exit 0" in e["data"]["text"]
    ]
    assert {e["role"] for e in shells} == {"coder", "reviewer"}  # both ran the tests
    assert events[-1]["kind"] == "workflow_result"


def test_run_without_a_sandbox_flag(tmp_path: Path, repo: Path) -> None:
    code, _, err = cli(
        tmp_path, ["-C", str(repo), "--unsafe-no-sandbox", "fix it"], FIX + REVIEW
    )
    assert code == 0, err
    assert "warning: shell commands run without a sandbox" in err


def test_not_approved_exits_1(tmp_path: Path, repo: Path) -> None:
    replies = [*FIX, text("VERDICT: CHANGES\n- no test for negatives")]
    code, _, err = cli(
        tmp_path,
        ["-C", str(repo), "--unsafe-no-sandbox", "--max-rounds", "1", "x"],
        replies,
    )
    assert code == 1
    assert "not approved after 1 rounds" in err


def test_budget_exits_3(tmp_path: Path, repo: Path) -> None:
    replies = [
        Reply("", (call("read", path="calc.py"),), "tool_use", Usage(cost_usd=2.0))
    ]
    code, _, err = cli(
        tmp_path,
        ["-C", str(repo), "--unsafe-no-sandbox", "--max-cost", "1", "x"],
        replies,
    )
    assert code == 3
    assert "timu: budget" in err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-C", "/nonexistent", "x"], "is not a directory"),
        (["--max-rounds", "0", "x"], "--max-rounds must be at least 1"),
    ],
)
def test_usage_errors(tmp_path: Path, argv: list[str], message: str) -> None:
    code, _, err = cli(tmp_path, argv, [])
    assert (code, message in err) == (2, True)


def test_report_symlink_is_a_usage_error(tmp_path: Path, repo: Path) -> None:
    (repo / "REVIEW.md").symlink_to(repo / "calc.py")
    code, _, err = cli(tmp_path, ["-C", str(repo), "--unsafe-no-sandbox", "x"], [])
    assert code == 2
    assert "symlink" in err


def test_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[provider]\nmodel = 1\n")
    err = io.StringIO()
    assert main(["run", "--config", str(bad), "x"], err=err) == 2
    assert "model has the wrong type" in err.getvalue()


def test_missing_key_is_reported_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = tmp_path / "t.toml"
    path.write_text('[provider]\nmodel = "m"\n')
    err = io.StringIO()
    assert main(["run", "--config", str(path), "x"], err=err) == 2
    assert "OPENROUTER_API_KEY is not set" in err.getvalue()


def test_console_sink() -> None:
    out, err = io.StringIO(), io.StringIO()
    sink = ConsoleSink(out, err)

    def ev(kind: str, **data: Any) -> Event:
        return Event(kind, "a1", "", "coder", 0.0, data)

    sink(ev("model_delta", text="thinking"))
    sink(
        ev(
            "tool_call",
            id="c",
            name="shell",
            arguments={"command": "make   test\n-k x"},
            raw="",
        )
    )
    sink(ev("tool_result", id="c", text="ok\n[exit 0]", is_error=False))
    sink(ev("tool_result", id="c", text="boom\n[exit 2]", is_error=True))
    sink(
        ev(
            "tool_call",
            id="d",
            name="edit",
            arguments={"path": "a.py", "old_string": "x"},
            raw="",
        )
    )
    sink(
        ev(
            "result",
            status="done",
            summary="s",
            usage={"turns": 2, "input_tokens": 5, "output_tokens": 1},
        )
    )
    assert out.getvalue() == "thinking\n"
    assert err.getvalue() == (
        "[coder a1] shell make test -k x\n"
        "    error: boom\n"
        "[coder a1] edit a.py\n"
        "[coder a1] done: 2 turns, 6 tokens\n"
    )
