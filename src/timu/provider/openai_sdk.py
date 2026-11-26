"""OpenAI's Responses API through the openai SDK: `[provider] type = "openai"`.

Needs the extra: `uv tool install 'timu[extras]'`. The SDK's own retries are off, so
timu's rule applies (provider.retry). For OpenRouter and other servers that speak
chat completions, use the built-in type instead.

Requests set store = false and ask for encrypted reasoning, so nothing is kept on
OpenAI's side and the model's reasoning still carries across turns: each reply's
output items, reasoning included, come back in Reply.extra and are sent back as the
next request's input, as the Responses API's stateless mode expects.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import openai

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

TERMINAL = ("response.completed", "response.incomplete", "response.failed")
INCOMPLETE: dict[str, Stop] = {
    "max_output_tokens": "length",
    "content_filter": "refused",
}
DEFAULTS: Mapping[str, Any] = {
    "store": False,
    "include": ["reasoning.encrypted_content"],
}


def from_settings(settings: ProviderSettings) -> OpenAIResponsesProvider:
    """options are request params, such as reasoning or max_output_tokens; they
    replace the defaults store = false and include = [reasoning.encrypted_content]."""
    client = openai.OpenAI(
        api_key=settings.api_key or None,
        base_url=settings.base_url or None,
        timeout=settings.timeout,
        max_retries=0,
    )
    return OpenAIResponsesProvider(
        client, settings.model, request=settings.options, extra_body=settings.extra_body
    )


class OpenAIResponsesProvider:
    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        *,
        request: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        retries: int = 4,
        retry_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.model = model
        self.request = {**DEFAULTS, **(request or {})}
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
        instructions, items = to_wire(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": items,
            **self.request,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": False,  # timu's schemas are not written for strict mode
                }
                for t in tools
            ]
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        return retrying(
            lambda: self._attempt(kwargs, on_text),
            on_text,
            self.retries,
            self.retry_delay,
            self._sleep,
        )

    def _attempt(
        self, kwargs: Mapping[str, Any], on_text: OnText | None
    ) -> Reply | Failure:
        delivered = False
        final: Any = None
        error = ""
        try:
            with self.client.responses.create(**kwargs, stream=True) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta" and event.delta:
                        if on_text:
                            on_text(event.delta)
                            delivered = True
                    elif event.type in TERMINAL:
                        final = event.response
                    elif event.type == "error":
                        error = f"{event.code}: {event.message}"
        except openai.APIStatusError as e:
            text = _body(e)
            if is_context_error(text):
                return Failure(f"{CONTEXT_OVERFLOW}: {text[:500]}", retryable=False)
            quota = "insufficient_quota" in text  # waiting does not refill it
            return Failure(
                f"http {e.status_code}: {text[:500]}",
                retryable_status(e.status_code) and not quota,
                retry_after(e.response.headers),
                delivered,
            )
        except openai.APIConnectionError as e:
            return Failure(f"network error: {e}", True, delivered=delivered)
        except openai.OpenAIError as e:  # a malformed stream
            return Failure(f"stream error: {str(e)[:500]}", True, delivered=delivered)
        if final is None:
            why = error or "the stream ended before the response did"
            if is_context_error(why):
                return Failure(f"{CONTEXT_OVERFLOW}: {why[:500]}", retryable=False)
            return Failure(f"stream error: {why[:500]}", True, delivered=delivered)
        return to_reply(final)


def to_wire(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """The instructions, and the conversation as Responses input items."""
    instructions = "\n\n".join(
        m.content for m in messages if m.role == "system" and m.content
    )
    items: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "user":
            items.append({"role": "user", "content": m.content})
        elif m.role == "tool":
            output = f"error: {m.content}" if m.is_error else m.content
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.tool_call_id,
                    "output": output,
                }
            )
        elif isinstance(output_items := m.extra.get("output"), list):
            items.extend(dict(i) for i in output_items if isinstance(i, dict))
        else:
            if m.content:
                items.append({"role": "assistant", "content": m.content})
            for c in m.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": c.id,
                        "name": c.name,
                        "arguments": c.raw or json.dumps(c.arguments or {}),
                    }
                )
    return instructions, items


def to_reply(response: Any) -> Reply:
    """A Reply from a terminal Response: completed, incomplete or failed."""
    if response.status == "failed":
        err = response.error
        why = f"{err.code}: {err.message}" if err else "the response failed"
        prefix = f"{CONTEXT_OVERFLOW}: " if is_context_error(why) else ""
        return Reply("", stop="error", error=f"{prefix}{why[:500]}")
    output = [
        i.model_dump(mode="json", exclude_none=True) for i in response.output or ()
    ]
    text, refusal = [], []
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content") or ():
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    refusal.append(part.get("refusal", ""))
    calls = tuple(
        ToolCall(
            i["call_id"],
            i["name"],
            _args(i.get("arguments", "")),
            i.get("arguments", ""),
        )
        for i in output
        if i.get("type") == "function_call"
    )
    stop: Stop = "tool_use" if calls else "end"
    if refusal and not text:
        stop = "refused"
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", "") or ""
        stop = INCOMPLETE.get(reason, "length")
    u = response.usage
    details = getattr(u, "input_tokens_details", None) if u else None
    prompt = u.input_tokens if u else 0
    cached = min(getattr(details, "cached_tokens", 0) or 0, prompt)
    written = min(getattr(details, "cache_write_tokens", 0) or 0, prompt - cached)
    usage = Usage(
        input_tokens=prompt,  # OpenAI counts cached tokens inside input_tokens
        output_tokens=u.output_tokens if u else 0,
        cached_input_tokens=cached,
        cache_write_input_tokens=written,
    )
    return Reply(
        "".join(text) or "".join(refusal),
        calls,
        stop,
        usage,
        extra={"output": output} if output else {},
        cost_known=False,
    )


def _args(raw: str) -> dict[str, Any] | None:
    if not raw.strip():
        return {}
    try:
        args = json.loads(raw)
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def _body(e: openai.APIStatusError) -> str:
    body = e.body
    return json.dumps(body) if isinstance(body, dict) else str(e.message)
