"""Model providers: the public interface for adapters (see base) and the lookup of
a provider by its `[provider] type`."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import cache
from importlib.metadata import entry_points

from timu.provider.base import (
    CONTEXT_OVERFLOW,
    Message,
    OnText,
    Provider,
    ProviderFactory,
    ProviderSettings,
    Reply,
    Stop,
    ToolCall,
)
from timu.tool import Tool
from timu.types import Usage

ENTRY_POINTS = "timu.providers"
BUILTIN = "chat-completions"  # OpenAI-compatible chat completions, over urllib


@cache
def factory(name: str) -> ProviderFactory:
    """The factory for provider type name. The built-in name cannot be replaced by a
    package. Raises LookupError if no installed package provides name, TypeError if
    its entry point is not callable, and whatever importing the package raises."""
    if name == BUILTIN:
        from timu.provider.openai import from_settings

        return from_settings
    found = entry_points(group=ENTRY_POINTS, name=name)
    if not found:
        names = sorted({BUILTIN, *entry_points(group=ENTRY_POINTS).names})
        raise LookupError(f"no provider type {name!r}; installed: {', '.join(names)}")
    loaded = next(iter(found)).load()
    if not callable(loaded):
        raise TypeError(f"provider type {name!r} is not callable")
    return loaded  # type: ignore[no-any-return]


@dataclass(frozen=True)
class Price:
    """USD per million tokens. cached_input is the rate for prompt tokens read from
    a cache, and cache_write for those written to one; None bills them at the input
    rate. One cache_write rate covers every cache lifetime: where writes cost more
    for a longer one, as on Anthropic, set the rate of the longest used, and the
    cost is an upper bound."""

    input: float
    output: float
    cached_input: float | None = None
    cache_write: float | None = None

    def __post_init__(self) -> None:
        rates = (self.input, self.output, self.cached_input, self.cache_write)
        for v in (r for r in rates if r is not None):
            number = isinstance(v, int | float) and not isinstance(v, bool)
            if not (number and 0 <= v < math.inf):  # also rejects nan
                raise ValueError("prices must be numbers >= 0")

    def cost(self, usage: Usage) -> float:
        """The cost of usage. Cache counts beyond input_tokens, which break the
        provider contract (usage_problem), are cut to fit."""
        read = max(0, min(usage.cached_input_tokens, usage.input_tokens))
        write = max(0, min(usage.cache_write_input_tokens, usage.input_tokens - read))
        fresh = usage.input_tokens - read - write
        read_rate = self.input if self.cached_input is None else self.cached_input
        write_rate = self.input if self.cache_write is None else self.cache_write
        cost = fresh * self.input + read * read_rate + write * write_rate
        return (cost + usage.output_tokens * self.output) / 1e6


class Priced:
    """inner, with cost computed from price for each reply whose server reports
    none, so --max-cost applies. A reported cost is kept: it knows the discounts."""

    def __init__(self, inner: Provider, price: Price) -> None:
        self.inner, self.price = inner, price
        self.model = getattr(inner, "model", "")

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        on_text: OnText | None = None,
    ) -> Reply:
        reply = self.inner.complete(messages, tools, on_text)
        if reply.cost_known:
            return reply
        usage = replace(reply.usage, cost_usd=self.price.cost(reply.usage))
        return replace(reply, usage=usage, cost_known=True)


__all__ = [
    "BUILTIN",
    "CONTEXT_OVERFLOW",
    "ENTRY_POINTS",
    "Message",
    "OnText",
    "Price",
    "Priced",
    "Provider",
    "ProviderFactory",
    "ProviderSettings",
    "Reply",
    "Stop",
    "ToolCall",
    "factory",
]
