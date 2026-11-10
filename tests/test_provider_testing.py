"""Tests for the cache-write pricing, the usage check and the adapter test kit."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import TEST_ROLE
from timu import Agent, Event, Task, Usage
from timu.provider import Price
from timu.provider.base import Reply, ToolCall, usage_problem
from timu.provider.fake import FakeProvider, call, calls, text
from timu.provider.testing import reply_problems


def test_cache_writes_are_priced() -> None:
    u = Usage(
        input_tokens=1_000_000,
        cached_input_tokens=500_000,
        cache_write_input_tokens=300_000,
    )
    price = Price(4.0, 20.0, cached_input=0.2, cache_write=8.0)  # a 2x write rate
    assert price.cost(u) == pytest.approx(0.2 * 4.0 + 0.5 * 0.2 + 0.3 * 8.0)
    assert Price(4.0, 20.0).cost(u) == pytest.approx(4.0)  # no cache rates


def test_bad_cache_counts_are_cut_to_fit() -> None:
    u = Usage(input_tokens=100, cached_input_tokens=80, cache_write_input_tokens=80)
    assert Price(0, 0, cached_input=0, cache_write=1e6).cost(u) == pytest.approx(20)


def test_usage_problem() -> None:
    assert usage_problem(Usage(input_tokens=10, cached_input_tokens=4)) is None
    raw_anthropic = Usage(
        input_tokens=5, cached_input_tokens=90, cache_write_input_tokens=3
    )
    assert "input_tokens must count every prompt token" in (
        usage_problem(raw_anthropic) or ""
    )
    assert usage_problem(Usage(cached_input_tokens=-1)) is not None


def test_bad_usage_warns_once(tmp_path: Path) -> None:
    bad = Usage(input_tokens=5, cached_input_tokens=90)
    replies = [
        replace(calls(call("echo", text="x")), usage=bad),
        replace(text("done"), usage=bad),
    ]
    events: list[Event] = []
    provider = FakeProvider(replies)
    provider.model = "vendor/m"  # type: ignore[attr-defined]
    Agent(TEST_ROLE, provider, events.append, tmp_path).run(Task("go"))
    warnings = [e.data["message"] for e in events if e.kind == "warning"]
    assert len(warnings) == 1 and warnings[0].startswith("vendor/m reported bad usage")


def test_a_good_reply_has_no_problems() -> None:
    good = Reply(
        "ok",
        (ToolCall("c1", "echo", {"text": "x"}, '{"text": "x"}'),),
        "tool_use",
        Usage(input_tokens=10, output_tokens=2, cached_input_tokens=6),
    )
    assert reply_problems(good) == []
    assert reply_problems(Reply("", stop="error", error="502")) == []


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (
            Reply("", usage=Usage(input_tokens=1, cached_input_tokens=5)),
            "input_tokens must count",
        ),
        (Reply("", usage=Usage(input_tokens=-1)), "usage.input_tokens is -1"),
        (Reply("", usage=Usage(turns=1)), "usage.turns and usage.tool_calls"),
        (Reply("", usage=Usage(cost_usd=float("nan"))), "must be finite"),
        (Reply("", usage=Usage(cost_usd=0.1), cost_known=False), "cost_known is False"),
        (Reply("", stop="error"), "error is empty"),
        (
            Reply("", (ToolCall("c1", "echo", None, '{"a": 1}'),), "tool_use"),
            "arguments is None but raw is JSON",
        ),
        (
            Reply("", (ToolCall("c1", "echo", {"a": 2}, '{"a": 1}'),), "tool_use"),
            "arguments do not match raw",
        ),
    ],
)
def test_reply_problems(reply: Reply, problem: str) -> None:
    assert any(problem in p for p in reply_problems(reply)), reply_problems(reply)
