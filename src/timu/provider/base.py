"""timu's own message format and the interface every provider adapter implements."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from timu.tool import Tool
from timu.types import Usage


@dataclass(frozen=True)
class ToolCall:
    """A tool call from the model. arguments is None when raw is not a JSON object."""

    id: str
    name: str
    arguments: Mapping[str, Any] | None
    raw: str = ""


@dataclass(frozen=True)
class Message:
    """extra holds provider data that must go back unchanged, such as reasoning."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()  # assistant only
    tool_call_id: str = ""  # tool only
    is_error: bool = False  # tool only
    extra: Mapping[str, Any] = field(default_factory=dict)  # assistant only


Stop = Literal["end", "tool_use", "length", "refused", "error"]


@dataclass(frozen=True)
class Reply:
    """One model response. usage.turns is left at 0; the agent counts turns."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop: Stop = "end"
    usage: Usage = field(default_factory=Usage)
    error: str = ""  # set when stop is "error"
    extra: Mapping[str, Any] = field(default_factory=dict)


OnText = Callable[[str], None]


class Provider(Protocol):
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        on_text: OnText | None = None,
    ) -> Reply:
        """Send the conversation; return the reply. Text is passed to on_text as it
        arrives. Failures return stop="error"; they do not raise."""
        ...
