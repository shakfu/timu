"""A provider that returns scripted replies, for tests and replay."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from timu.provider.base import Message, OnText, Reply, ToolCall
from timu.tool import Tool
from timu.types import Usage


@dataclass(frozen=True)
class Request:
    """What the fake received on one call."""

    messages: tuple[Message, ...]
    tools: tuple[str, ...]


class FakeProvider:
    """Returns the scripted replies in order. Once they run out, every reply is an error."""

    def __init__(self, replies: Iterable[Reply]) -> None:
        self._replies = list(replies)
        self.requests: list[Request] = []

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        on_text: OnText | None = None,
    ) -> Reply:
        self.requests.append(Request(tuple(messages), tuple(t.name for t in tools)))
        if len(self.requests) > len(self._replies):
            return Reply("", stop="error", error="fake provider: script exhausted")
        reply = self._replies[len(self.requests) - 1]
        if on_text and reply.text:
            on_text(reply.text)
        return reply


def text(content: str, tokens: tuple[int, int] = (10, 5), cost: float = 0.0) -> Reply:
    """A final reply: text and no tool calls."""
    return Reply(
        content,
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=cost),
    )


def call(tool: str, /, id: str = "", **arguments: Any) -> ToolCall:
    return ToolCall(id or f"call_{tool}", tool, arguments, json.dumps(arguments))


def calls(
    *tool_calls: ToolCall, content: str = "", tokens: tuple[int, int] = (10, 5)
) -> Reply:
    """A reply that asks for tools."""
    usage = Usage(input_tokens=tokens[0], output_tokens=tokens[1])
    return Reply(content, tool_calls, stop="tool_use", usage=usage)
