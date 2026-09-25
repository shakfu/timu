"""Tests for traces: loading, the agent tree, `timu trace`, and replay (plan phase 9)."""

from __future__ import annotations

import getpass
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from scenarios import SCENARIOS, redact, restore
from timu import Capability, Event, Result, Tool, ToolOutput, Usage
from timu.cli import main
from timu.provider.base import Provider, Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.sandbox import MacSandbox, NoSandbox, Sandbox, detect
from timu.trace import (
    TraceError,
    _cost,
    compare,
    load,
    recorded_tool,
    render,
    replies,
    save,
    tree,
)
from timu.workflow import Run, fix_review, lead

FIXTURES = Path(__file__).parent / "fixtures" / "traces"
N = Capability.NET

FIX_SCRIPT = [
    calls(call("read", path="calc.py")),
    calls(call("edit", path="calc.py", old_string="a - b", new_string="a + b")),
    calls(call("shell", command="make test")),
    text("Fixed add()."),
    calls(call("shell", command="make test")),
    text("VERDICT: APPROVE\nadd() adds."),
]
LEAD_SCRIPT = [
    calls(call("delegate", role="researcher", goal="find the version")),
    calls(call("web_fetch", url="https://docs.python.org/3/library/tomllib.html")),
    text("3.11\nSources:\nhttps://docs.python.org/3/library/tomllib.html"),
    calls(
        call(
            "delegate",
            role="coder",
            goal="set tomllib_added() to the version",
            inputs=["a2"],
        )
    ),
    calls(call("read", path="facts.py")),
    calls(call("edit", path="facts.py", old_string='"TODO"', new_string='"3.11"')),
    text("done"),
    calls(call("delegate", role="reviewer", goal="review facts.py")),
    calls(call("shell", command="cat facts.py")),
    text("VERDICT: APPROVE"),
    text("Objective met."),
]
PAGE = "tomllib -- Added in version 3.11."


def sandbox() -> Sandbox:
    """The recorded runs used the macOS sandbox with this Python readable."""
    if isinstance(detect(), MacSandbox):
        prefixes = {Path(os.path.realpath(p)) for p in (sys.prefix, sys.base_prefix)}
        return MacSandbox(extra_read=tuple(prefixes))
    return NoSandbox()


def execute(
    name: str, repo: Path, provider: Provider, events: list[Event], fetch: Any = None
) -> Result:
    """Run scenario name as the CLI would, with provider and, for lead, fetch."""
    scenario = SCENARIOS[name]
    scenario.build(repo)
    run = Run(repo, lambda role: provider, events.append, sandbox=sandbox())
    if scenario.workflow == "lead":
        return lead(run, scenario.objective, fetch=fetch)
    return fix_review(run, scenario.objective)


def fake_fetch(page: str) -> Any:
    return Tool(
        "web_fetch",
        "fetch",
        {"type": "object"},
        frozenset({N}),
        lambda c, a: ToolOutput(page),
    )


def record(tmp_path: Path, name: str, script: list[Reply]) -> list[Event]:
    """A trace of a scripted run of scenario name in tmp_path/rec/repo, redacted and
    saved as the live tests save it, then read back."""
    events: list[Event] = []
    root = tmp_path / "rec"
    execute(name, root / "repo", FakeProvider(script), events, fake_fetch(PAGE))
    path = tmp_path / f"{name}.jsonl"
    save(redact(events, root), path)
    return load(path)


def replay(tmp_path: Path, name: str, trace: list[Event]) -> list[Event]:
    """Replay a redacted trace in tmp_path/rep/repo, the same layout as the recording."""
    root = tmp_path / "rep"
    trace = restore(trace, root)
    events: list[Event] = []
    fetch = recorded_tool(trace, "web_fetch", frozenset({N}))
    execute(name, root / "repo", FakeProvider(replies(trace)), events, fetch)
    return redact(events, root)  # its own temp root as {TMP}, to compare with the trace


# ---- load ----


def test_load_errors_name_the_line(tmp_path: Path) -> None:
    good = json.dumps(
        {
            "kind": "start",
            "agent_id": "a1",
            "parent_id": "",
            "role": "r",
            "ts": 1,
            "data": {},
        }
    )
    path = tmp_path / "t.jsonl"
    path.write_text(f"{good}\n{good}\n{{broken\n")
    with pytest.raises(TraceError, match=r"t.jsonl:3: not a JSON object"):
        load(path)
    path.write_text(f"{good}\n" + json.dumps({"kind": "start", "ts": "x"}) + "\n")
    with pytest.raises(TraceError, match=r"t.jsonl:2: not an event"):
        load(path)
    with pytest.raises(TraceError, match="missing.jsonl"):
        load(tmp_path / "missing.jsonl")


# ---- tree and render ----


def test_tree_and_render(tmp_path: Path) -> None:
    trace = record(tmp_path, "lead-tomllib", list(LEAD_SCRIPT))
    (root,) = tree(trace)
    assert (root.agent_id, root.role, root.status) == ("a1", "lead", "done")
    assert [(c.agent_id, c.role) for c in root.children] == [
        ("a2", "researcher"),
        ("a3", "coder"),
        ("a4", "reviewer"),
    ]
    lines = render(trace).splitlines()
    assert lines[0].startswith("run ") and " done: 11 turns, 7 tool calls, " in lines[0]
    assert lines[1] == "  a1 lead done: 4 turns, 3 tool calls, 60 tokens untrusted"
    assert (
        lines[2] == "    a2 researcher done: 2 turns, 1 tool call, 30 tokens untrusted"
    )
    assert lines[-1] == "summary: Objective met."


def test_cost_uses_singular_nouns_for_one() -> None:
    assert _cost(Usage(turns=1, tool_calls=1, input_tokens=1)) == (
        "1 turn, 1 tool call, 1 token"
    )


def test_render_unfinished(tmp_path: Path) -> None:
    trace = record(tmp_path, "fix-review-calc", FIX_SCRIPT[:2])  # the provider runs dry
    assert render(trace).splitlines()[0].startswith("run ")
    assert "coder failed" in render(trace)


# ---- replay ----


@pytest.mark.parametrize(
    ("name", "script"), [("fix-review-calc", FIX_SCRIPT), ("lead-tomllib", LEAD_SCRIPT)]
)
def test_replay_reproduces_a_run(
    tmp_path: Path, name: str, script: list[Reply]
) -> None:
    trace = record(tmp_path, name, list(script))
    assert compare(trace, replay(tmp_path, name, trace)) is None


def test_redacted_trace_has_no_local_paths(tmp_path: Path) -> None:
    script = [
        calls(call("read", path=str(tmp_path / "rec" / "repo" / "calc.py"))),
        *FIX_SCRIPT,
    ]
    record(tmp_path, "fix-review-calc", script)
    raw = (tmp_path / "fix-review-calc.jsonl").read_text()
    assert os.path.realpath(tmp_path) not in raw
    assert os.environ["HOME"] not in raw
    assert f"/{getpass.getuser()}/" not in raw
    assert '"path": "{TMP}/repo/calc.py"' in raw


def test_absolute_paths_replay_in_a_new_location(tmp_path: Path) -> None:
    """A tool call with an absolute path into the recorded workspace still works when
    the replay runs somewhere else."""
    script = [
        calls(call("read", path=str(tmp_path / "rec" / "repo" / "calc.py"))),
        *FIX_SCRIPT,
    ]
    trace = record(tmp_path, "fix-review-calc", script)
    replayed = replay(tmp_path, "fix-review-calc", trace)
    first = next(e for e in replayed if e.kind == "tool_result")
    assert first.data["is_error"] is False
    assert compare(trace, replayed) is None


def test_replay_detects_a_missing_line(tmp_path: Path) -> None:
    """A trace with a model reply removed replays differently, and compare names the
    first trace line that did not reproduce."""
    trace = record(tmp_path, "fix-review-calc", FIX_SCRIPT)
    n = next(i for i, e in enumerate(trace) if e.kind == "model_reply" and i > 5)
    damaged = trace[:n] + trace[n + 1 :]
    diff = compare(damaged, replay(tmp_path, "fix-review-calc", damaged))
    assert diff is not None
    assert diff.startswith("trace line ")


def test_compare_reports_extra_events(tmp_path: Path) -> None:
    trace = record(tmp_path, "fix-review-calc", FIX_SCRIPT)
    assert "more than the trace" in str(compare(trace[:-3], trace))


def test_recorded_tool(tmp_path: Path) -> None:
    trace = record(tmp_path, "lead-tomllib", list(LEAD_SCRIPT))
    tool = recorded_tool(trace, "web_fetch", frozenset({N}))
    ctx: Any = None
    first = tool.run(ctx, {"url": "anything"})
    assert (first.text, first.untrusted) == (PAGE, True)
    assert tool.run(ctx, {}).text == "replay: no recorded result left for web_fetch"


# ---- timu trace ----


def test_timu_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "state" / "timu" / "runs"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    out, err = io.StringIO(), io.StringIO()
    assert main(["trace"], out=out, err=err) == 2
    assert "no traces in" in err.getvalue()

    runs.mkdir(parents=True)
    trace = record(tmp_path, "lead-tomllib", list(LEAD_SCRIPT))
    save(trace, runs / "r1.jsonl")
    for argv in (["trace"], ["trace", "r1"], ["trace", str(runs / "r1.jsonl")]):
        out = io.StringIO()
        assert main(argv, out=out, err=io.StringIO()) == 0
        assert "  a1 lead done" in out.getvalue()
        assert out.getvalue().endswith(f"trace: {runs / 'r1.jsonl'}\n")
    (runs / "bad.jsonl").write_text("nope\n")
    err = io.StringIO()
    assert main(["trace", "bad"], out=io.StringIO(), err=err) == 2
    assert "bad.jsonl:1: not a JSON object" in err.getvalue()


# ---- recorded fixtures ----

RECORDED = sorted(FIXTURES.glob("*.jsonl"))


@pytest.mark.skipif(not RECORDED, reason="no recorded traces in tests/fixtures/traces")
@pytest.mark.skipif(
    not isinstance(detect(), MacSandbox), reason="recorded with the macOS sandbox"
)
@pytest.mark.parametrize("path", RECORDED, ids=[p.stem for p in RECORDED])
def test_recorded_traces_replay(tmp_path: Path, path: Path) -> None:
    """Phase 9 exit: every recorded run replays with the same decisions."""
    meta = json.loads(path.with_suffix(".json").read_text())
    trace = load(path)
    diff = compare(trace, replay(tmp_path, meta["scenario"], trace))
    assert diff is None, f"{path.name}: {diff}"
