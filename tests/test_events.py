"""Tests for event sinks."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from conftest import MakeAgent
from timu import Event, JsonlSink, Task
from timu.provider.fake import text


def test_jsonl_sink_is_private(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        with JsonlSink(tmp_path / "trace.jsonl"):
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "trace.jsonl").stat().st_mode) == 0o600


def test_jsonl_sink_appends_lines(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    with JsonlSink(path) as sink:
        sink(Event("start", "a1", "", "coder", 1.5, {"goal": "g"}))
    with JsonlSink(path) as sink:
        sink(Event("result", "a1", "", "coder", 2.5, {"status": "done"}))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [
        {
            "kind": "start",
            "agent_id": "a1",
            "parent_id": "",
            "role": "coder",
            "ts": 1.5,
            "data": {"goal": "g"},
        },
        {
            "kind": "result",
            "agent_id": "a1",
            "parent_id": "",
            "role": "coder",
            "ts": 2.5,
            "data": {"status": "done"},
        },
    ]


def test_agent_run_to_jsonl(make_agent: MakeAgent, tmp_path: Path) -> None:
    agent, _, _ = make_agent([text("hi")])
    path = tmp_path / "run.jsonl"
    with JsonlSink(path) as sink:
        agent.sink = sink
        agent.run(Task("x"))
    kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
    assert kinds == ["start", "model_delta", "model_reply", "result"]
