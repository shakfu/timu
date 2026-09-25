"""Shared test helpers: a few trivial tools and an agent factory."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from timu import Agent, Budget, Context, Event, Role, Tool, ToolOutput
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider


def _echo(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    return ToolOutput(str(args["text"]))


def _add(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    return ToolOutput(str(args["a"] + args["b"]))


def _cancel(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    ctx.cancel.set()
    return ToolOutput("cancel requested")


def _boom(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    raise RuntimeError("handler bug")


def _big(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    return ToolOutput("x" * int(args["n"]))


def _tool(name: str, run: Callable[[Context, Mapping[str, Any]], ToolOutput]) -> Tool:
    return Tool(name, f"test tool {name}", {"type": "object"}, frozenset(), run)


ECHO = _tool("echo", _echo)
ADD = _tool("add", _add)
CANCEL = _tool("cancel", _cancel)
BOOM = _tool("boom", _boom)
BIG = _tool("big", _big)
TEST_ROLE = Role("tester", "You are a test agent.", (ECHO, ADD, CANCEL, BOOM, BIG))

MakeAgent = Callable[..., tuple[Agent, FakeProvider, list[Event]]]


@pytest.fixture
def make_agent(tmp_path: Path) -> MakeAgent:
    """make_agent(replies, budget=None, **agent_kwargs) -> (agent, provider, events)."""

    def make(
        replies: Iterable[Reply], budget: Budget | None = None, **kwargs: Any
    ) -> tuple[Agent, FakeProvider, list[Event]]:
        role = (
            TEST_ROLE
            if budget is None
            else Role(TEST_ROLE.name, TEST_ROLE.prompt, TEST_ROLE.tools, budget=budget)
        )
        provider = FakeProvider(replies)
        events: list[Event] = []
        return (
            Agent(role, provider, events.append, tmp_path, **kwargs),
            provider,
            events,
        )

    return make
