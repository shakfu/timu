"""timu's own message format and the interface every provider adapter implements.

This module is public API (see timu.provider). A provider outside timu registers a
ProviderFactory under the `timu.providers` entry-point group, and `[provider] type`
in the user's config selects it by name. The contract, beyond the signatures:

- complete() does not raise. A failure returns stop="error" with error set. Retrying
  transient failures is the provider's job; the agent does not retry.
- An error that starts with CONTEXT_OVERFLOW makes the agent trim old tool output
  and call again. Use it only when the conversation is too long for the model.
- usage carries tokens and, when known, cost. input_tokens counts every prompt
  token; cached_input_tokens is the part read from a prompt cache, and
  cache_write_input_tokens the part written to one. Set
  cost_known=False when the server reports no cost: --max-cost is then enforced
  only if the config sets a price, and the run warns otherwise.
- Reply.extra comes back unchanged as Message.extra on the next call, for data the
  API needs returned, such as reasoning or thinking signatures.
- on_text gets the reply's text as it streams. Text sent there is shown and traced,
  not added to the conversation; only Reply.text is.
- A provider may expose a `model` attribute; the trace records it.
- The CLI builds one provider per agent run. Parallel graph nodes call different
  instances at the same time, so a factory's shared state must be thread-safe.
"""

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
    cost_known: bool = True  # False: the server reported tokens but no cost


OnText = Callable[[str], None]

CONTEXT_OVERFLOW = "context length exceeded"  # the prefix of Reply.error for it


def usage_problem(usage: Usage) -> str | None:
    """Why usage breaks the contract, or None. The likely cause is an adapter for
    an API that counts cached tokens apart from input_tokens, such as Anthropic's,
    passing input_tokens through without adding them."""
    cache = usage.cached_input_tokens + usage.cache_write_input_tokens
    if min(usage.cached_input_tokens, usage.cache_write_input_tokens) < 0:
        return "cache token counts must not be negative"
    if cache > usage.input_tokens:
        return (
            f"cached_input_tokens {usage.cached_input_tokens} + "
            f"cache_write_input_tokens {usage.cache_write_input_tokens} > "
            f"input_tokens {usage.input_tokens}; input_tokens must count every "
            "prompt token"
        )
    return None


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


@dataclass(frozen=True)
class ProviderSettings:
    """The `[provider]` table, resolved for one role. An empty base_url or api_key
    means the provider's own default, such as an SDK reading its key from the
    environment. options is the `[provider.options]` table, for the provider alone."""

    model: str
    base_url: str = ""
    api_key: str = ""
    timeout: float = 600  # seconds
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)


ProviderFactory = Callable[[ProviderSettings], Provider]
"""Builds a provider. Raises ValueError for bad settings; the message is shown."""
