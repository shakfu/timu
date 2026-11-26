"""Anthropic's Messages API through the anthropic SDK: `[provider] type = "anthropic"`.

Needs the extra: `uv tool install 'timu[extras]'`. The SDK's own retries are off, so
timu's rule applies (provider.retry). Prompt caching is on: the request sets a
top-level cache_control, which caches the longest prefix it can on every turn.

Assistant turns with thinking come back as their content blocks in Reply.extra and
go back unchanged. An edited history, such as one the agent trimmed, invalidates the
thinking blocks after the edit (preserved thinking); the API then answers 400 and
names the block. The provider removes the thinking blocks and sends the request
again, once: the turn loses its earlier reasoning, not the run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import anthropic

from timu.provider.base import (
    CONTEXT_OVERFLOW,
    Message,
    OnText,
    ProviderSettings,
    Reply,
    Stop,
    ToolCall,
)
from timu.provider.retry import (
    Failure,
    is_context_error,
    retry_after,
    retryable_status,
    retrying,
)
from timu.tool import Tool
from timu.types import Usage

DEFAULT_MAX_TOKENS = 64_000  # streamed, so a long reply does not time out
STOPS: dict[str, Stop] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "pause_turn": "end",  # server tools only, which timu does not send
    "tool_use": "tool_use",
    "max_tokens": "length",
    "refusal": "refused",
}
THINKING = ("thinking", "redacted_thinking")
BOUND = "bound to a different conversation"  # the 400 for an edited history
# error types worth another try when they arrive as an event in a 200 stream
RETRY_TYPES = ("overloaded_error", "api_error", "rate_limit_error", "timeout_error")


def from_settings(settings: ProviderSettings) -> AnthropicProvider:
    """options are request params, such as thinking or output_config, plus
    max_tokens (default 64000) and cache (default true)."""
    options = dict(settings.options)
    max_tokens = options.pop("max_tokens", DEFAULT_MAX_TOKENS)
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens < 1
    ):
        raise ValueError("provider.options.max_tokens must be a positive integer")
    cache = options.pop("cache", True)
    if not isinstance(cache, bool):
        raise ValueError("provider.options.cache must be true or false")  # noqa: TRY004 - the factory contract
    client = anthropic.Anthropic(
        api_key=settings.api_key or None,
        base_url=settings.base_url or None,
        timeout=settings.timeout,
        max_retries=0,
    )
    return AnthropicProvider(
        client,
        settings.model,
        max_tokens=max_tokens,
        cache=cache,
        request=options,
        extra_body=settings.extra_body,
    )


class AnthropicProvider:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        cache: bool = True,
        request: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        retries: int = 4,
        retry_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.cache = cache
        self.request = dict(request or {})
        self.extra_body = dict(extra_body or {})
        self.retries = retries
        self.retry_delay = retry_delay
        self._sleep = sleep

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        on_text: OnText | None = None,
    ) -> Reply:
        system, wire = to_wire(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": wire,
            **self.request,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        if self.cache:
            kwargs["cache_control"] = {"type": "ephemeral"}
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        def send() -> Reply:
            return retrying(
                lambda: self._attempt(kwargs, on_text),
                on_text,
                self.retries,
                self.retry_delay,
                self._sleep,
            )

        reply = send()
        if reply.stop == "error" and BOUND in reply.error and has_thinking(wire):
            kwargs["messages"] = without_thinking(wire)
            reply = send()
        return reply

    def _attempt(
        self, kwargs: Mapping[str, Any], on_text: OnText | None
    ) -> Reply | Failure:
        delivered = False
        try:
            with self.client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if event.type == "text" and event.text and on_text:
                        on_text(event.text)
                        delivered = True
                final = stream.get_final_message()
        except anthropic.APIStatusError as e:
            text = _body(e)
            if e.status_code in (200, 400) and is_context_error(text):
                return Failure(f"{CONTEXT_OVERFLOW}: {text[:500]}", retryable=False)
            if e.status_code < 400:  # an error event in a stream that began fine
                retry = any(t in text for t in RETRY_TYPES)
                return Failure(
                    f"stream error: {text[:500]}", retry, delivered=delivered
                )
            return Failure(
                f"http {e.status_code}: {text[:500]}",
                retryable_status(e.status_code),
                retry_after(e.response.headers),
                delivered,
            )
        except anthropic.APIConnectionError as e:
            return Failure(f"network error: {e}", True, delivered=delivered)
        except anthropic.AnthropicError as e:  # a malformed stream
            text = str(e)
            retry = any(t in text for t in RETRY_TYPES)
            return Failure(f"stream error: {text[:500]}", retry, delivered=delivered)
        return to_reply(final)


def to_wire(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """The system prompt, and the conversation as Messages API turns. Tool results
    and user text that follow each other join one user turn, so parallel tool
    results arrive together. An assistant turn with nothing in it is left out."""
    system = "\n\n".join(
        m.content for m in messages if m.role == "system" and m.content
    )
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "assistant":
            blocks = _assistant_blocks(m)
            if blocks:
                wire.append({"role": "assistant", "content": blocks})
            continue
        if m.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
            }
            if m.content:
                block["content"] = m.content
            if m.is_error:
                block["is_error"] = True
        elif m.content:
            block = {"type": "text", "text": m.content}
        else:
            continue
        if wire and wire[-1]["role"] == "user":
            wire[-1]["content"].append(block)
        else:
            wire.append({"role": "user", "content": [block]})
    return system, wire


def _assistant_blocks(m: Message) -> list[dict[str, Any]]:
    content = m.extra.get("content")
    if isinstance(content, list):  # the reply's own blocks, thinking included
        return [dict(b) for b in content if isinstance(b, dict)]
    blocks: list[dict[str, Any]] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for c in m.tool_calls:
        args = dict(c.arguments) if c.arguments is not None else {}
        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": args})
    return blocks


def has_thinking(wire: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        b.get("type") in THINKING
        for turn in wire
        if turn["role"] == "assistant"
        for b in turn["content"]
    )


def without_thinking(wire: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """wire with every thinking block removed, and turns left empty dropped."""
    out = []
    for turn in wire:
        content = [b for b in turn["content"] if b.get("type") not in THINKING]
        if content:
            out.append({**turn, "content": content})
    return out


def to_reply(message: Any) -> Reply:
    """A Reply from a final anthropic Message."""
    blocks = [b.model_dump(mode="json", exclude_none=True) for b in message.content]
    if message.stop_reason == "model_context_window_exceeded":
        return Reply("", stop="error", error=f"{CONTEXT_OVERFLOW}: the model's window")
    text = "".join(b["text"] for b in blocks if b.get("type") == "text")
    calls = tuple(
        ToolCall(
            b["id"],
            b["name"],
            b["input"] if isinstance(b.get("input"), dict) else None,
            json.dumps(b.get("input")),
        )
        for b in blocks
        if b.get("type") == "tool_use"
    )
    stop = STOPS.get(message.stop_reason or "", "tool_use" if calls else "end")
    u = message.usage
    read = u.cache_read_input_tokens or 0
    written = u.cache_creation_input_tokens or 0
    usage = Usage(
        input_tokens=u.input_tokens + read + written,  # Anthropic counts them apart
        output_tokens=u.output_tokens,
        cached_input_tokens=read,
        cache_write_input_tokens=written,
    )
    extra = (
        {"content": blocks} if any(b.get("type") in THINKING for b in blocks) else {}
    )
    return Reply(text, calls, stop, usage, extra=extra, cost_known=False)


def _body(e: anthropic.APIStatusError) -> str:
    body = e.body
    return json.dumps(body) if isinstance(body, dict) else str(e.message)
