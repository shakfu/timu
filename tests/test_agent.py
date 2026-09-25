"""Tests for the agent loop: statuses, budgets, repeat detection, cancel, events."""

from __future__ import annotations

import itertools
import json

from conftest import MakeAgent
from timu import Artifact, Budget, Origin, Task, Usage
from timu.agent import REPEAT_LIMIT, render_task
from timu.provider.base import Reply, ToolCall
from timu.provider.fake import call, calls, text

TASK = Task("do the thing")


def test_scripted_conversation(make_agent: MakeAgent) -> None:
    """5 turns and 5 tool calls: a batch of 2, an unknown tool the model recovers from."""
    agent, provider, events = make_agent(
        [
            calls(call("echo", text="a")),
            calls(call("echo", id="e2", text="b"), call("add", a=2, b=3)),
            calls(call("nope")),
            calls(call("echo", text="d")),
            text("all done", tokens=(100, 20), cost=0.5),
        ]
    )
    result = agent.run(TASK)

    assert result.status == "done"
    assert result.summary == "all done"
    assert result.usage.turns == 5
    assert result.usage.tool_calls == 5
    assert result.usage.tokens == 4 * 15 + 120
    assert result.usage.cost_usd == 0.5
    assert result.trace_id == "a1"

    last = provider.requests[-1].messages
    assert [m.role for m in last] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert last[1].content == "do the thing"
    assert [
        (m.tool_call_id, m.content, m.is_error) for m in last if m.role == "tool"
    ] == [
        ("call_echo", "a", False),
        ("e2", "b", False),
        ("call_add", "5", False),
        ("call_nope", "unknown tool: nope", True),
        ("call_echo", "d", False),
    ]
    assert provider.requests[0].tools == ("echo", "add", "cancel", "boom", "big")

    assert [e.kind for e in events] == [
        "start",
        "model_reply",
        "tool_call",
        "tool_result",
        "model_reply",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "model_reply",
        "tool_call",
        "tool_result",
        "model_reply",
        "tool_call",
        "tool_result",
        "model_delta",
        "model_reply",
        "result",
    ]
    assert {(e.agent_id, e.parent_id, e.role) for e in events} == {("a1", "", "tester")}
    assert events[-1].data["status"] == "done"
    assert events[-3].data == {"text": "all done"}
    for e in events:
        json.dumps(e.data)  # traces need JSON-serialisable data


def test_ids_are_passed_through(make_agent: MakeAgent) -> None:
    agent, _, events = make_agent([text("hi")], agent_id="a7", parent_id="a2")
    assert agent.run(TASK).trace_id == "a7"
    assert {(e.agent_id, e.parent_id) for e in events} == {("a7", "a2")}


def test_missing_call_id_is_filled(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(
        [
            calls(ToolCall("", "echo", {"text": "x"})),
            text("ok"),
        ]
    )
    assert agent.run(TASK).status == "done"
    msgs = provider.requests[-1].messages
    assert msgs[2].tool_calls[0].id == msgs[3].tool_call_id != ""


def test_provider_error_fails(make_agent: MakeAgent) -> None:
    agent, _, _ = make_agent([calls(call("echo", text="a"), content="working")])
    result = agent.run(TASK)  # the second request exhausts the script
    assert result.status == "failed"
    assert "script exhausted" in result.summary
    assert "working" in result.summary


def test_refused(make_agent: MakeAgent) -> None:
    agent, _, _ = make_agent([Reply("I can't help with that.", stop="refused")])
    result = agent.run(TASK)
    assert (result.status, result.summary) == ("refused", "I can't help with that.")


def test_length_fails(make_agent: MakeAgent) -> None:
    agent, _, _ = make_agent(
        [Reply("partial", (call("echo", text="a"),), stop="length")]
    )
    result = agent.run(TASK)
    assert result.status == "failed"
    assert "length limit" in result.summary


def test_invalid_arguments_are_an_error_result(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(
        [
            calls(ToolCall("c1", "echo", None, "{not json")),
            text("ok"),
        ]
    )
    assert agent.run(TASK).status == "done"
    tool_msg = provider.requests[-1].messages[3]
    assert tool_msg.is_error
    assert "invalid JSON" in tool_msg.content


def test_handler_exception_is_an_error_result(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(
        [calls(call("boom")), calls(call("echo")), text("ok")]
    )
    assert agent.run(TASK).status == "done"
    tools = [m for m in provider.requests[-1].messages if m.role == "tool"]
    assert tools[0].content == "boom failed: RuntimeError: handler bug"
    assert tools[1].content == "echo failed: KeyError: 'text'"  # missing argument
    assert all(m.is_error for m in tools)


def test_tool_output_is_capped(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent([calls(call("big", n=100_000)), text("ok")])
    agent.run(TASK)
    out = provider.requests[-1].messages[3].content
    assert len(out) < 17 * 1024
    assert "characters omitted" in out


# ---- budgets ----


def _distinct_calls(n: int) -> list[Reply]:
    return [calls(call("echo", text=str(i))) for i in range(n)]


def test_budget_turns(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(_distinct_calls(10), Budget(turns=2))
    result = agent.run(TASK)
    assert result.status == "budget"
    assert "turns" in result.summary
    assert len(provider.requests) == 2


def test_budget_tool_calls_within_a_batch(make_agent: MakeAgent) -> None:
    batch = calls(*(call("echo", id=f"c{i}", text=str(i)) for i in range(3)))
    agent, provider, events = make_agent([batch], Budget(tool_calls=2))
    result = agent.run(TASK)
    assert result.status == "budget"
    assert result.usage.tool_calls == 2
    assert len(provider.requests) == 1
    results = [e.data for e in events if e.kind == "tool_result"]
    assert [r["is_error"] for r in results] == [False, False, True]
    assert results[2]["text"].startswith("skipped: budget reached")


def test_budget_tool_calls_before_next_turn(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(_distinct_calls(10), Budget(tool_calls=2))
    assert agent.run(TASK).status == "budget"
    assert len(provider.requests) == 2


def test_budget_tokens(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(
        _distinct_calls(10), Budget(tokens=30)
    )  # 15 per reply
    assert agent.run(TASK).status == "budget"
    assert len(provider.requests) == 2


def test_budget_cost(make_agent: MakeAgent) -> None:
    replies = [
        Reply(
            "",
            (call("echo", id=f"c{i}", text=str(i)),),
            "tool_use",
            Usage(cost_usd=0.1),
        )
        for i in range(10)
    ]
    agent, provider, _ = make_agent(replies, Budget(cost_usd=0.25))
    assert agent.run(TASK).status == "budget"
    assert len(provider.requests) == 3


def test_no_cost_limit_by_default(make_agent: MakeAgent) -> None:
    agent, _, _ = make_agent([text("ok", cost=1e9)])
    assert agent.run(TASK).status == "done"


def test_budget_wall_time(make_agent: MakeAgent) -> None:
    clock = itertools.count(0, 6)  # start 0, first check 6, second 12
    agent, provider, _ = make_agent(
        _distinct_calls(10), Budget(wall_seconds=10), clock=lambda: float(next(clock))
    )
    result = agent.run(TASK)
    assert result.status == "budget"
    assert "wall_seconds" in result.summary
    assert len(provider.requests) == 1


def test_task_budget_overrides_role_budget(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent(_distinct_calls(10), Budget(turns=5))
    assert agent.run(Task("x", budget=Budget(turns=1))).status == "budget"
    assert len(provider.requests) == 1


# ---- repeat detection ----


def test_repeat_limit_stops_the_run(make_agent: MakeAgent) -> None:
    same = calls(call("echo", text="loop"))
    agent, provider, _ = make_agent([same] * 5)
    result = agent.run(TASK)
    assert result.status == "failed"
    assert f"{REPEAT_LIMIT} times in a row" in result.summary
    assert result.usage.tool_calls == REPEAT_LIMIT - 1  # the repeat itself is not run
    assert len(provider.requests) == REPEAT_LIMIT


def test_repeat_below_limit_continues(make_agent: MakeAgent) -> None:
    same = calls(call("echo", text="loop"))
    agent, _, _ = make_agent([same] * (REPEAT_LIMIT - 1) + [text("ok")])
    assert agent.run(TASK).status == "done"


def test_repeat_ignores_key_order(make_agent: MakeAgent) -> None:
    replies = [
        calls(ToolCall("c1", "add", {"a": 1, "b": 2})),
        calls(ToolCall("c2", "add", {"b": 2, "a": 1})),
        calls(ToolCall("c3", "add", {"a": 1, "b": 2})),
    ]
    agent, _, _ = make_agent(replies)
    assert agent.run(TASK).status == "failed"


def test_repeat_resets_on_a_different_call(make_agent: MakeAgent) -> None:
    a, b = calls(call("echo", text="a")), calls(call("echo", text="b"))
    agent, _, _ = make_agent([a, a, b, a, a, text("ok")])
    assert agent.run(TASK).status == "done"


# ---- cancel ----


def test_cancel_between_tool_calls(make_agent: MakeAgent) -> None:
    agent, provider, events = make_agent(
        [
            calls(call("cancel"), call("echo", text="never")),
            text("unreachable"),
        ]
    )
    result = agent.run(TASK)
    assert result.status == "cancelled"
    assert len(provider.requests) == 1
    results = [e.data for e in events if e.kind == "tool_result"]
    assert results[1] == {
        "id": "call_echo",
        "text": "skipped: cancelled",
        "is_error": True,
    }


def test_cancel_before_start(make_agent: MakeAgent) -> None:
    agent, provider, _ = make_agent([text("unreachable")])
    agent.cancel.set()
    assert agent.run(TASK).status == "cancelled"
    assert provider.requests == []


# ---- task rendering ----


def test_render_task() -> None:
    task = Task(
        "fix it",
        inputs=(Artifact('notes"<x>', "body", origin=Origin.AGENT),),
        accept="tests pass",
    )
    assert render_task(task) == (
        "fix it\n\n"
        "Acceptance criteria:\ntests pass\n\n"
        '<input name="notes&quot;&lt;x&gt;" kind="text" origin="agent">\nbody\n</input>'
    )
