"""OpenAI-compatible chat completions over urllib, streamed (plan D2).

Works with OpenRouter, OpenAI, llama-server, vLLM and Ollama. The response is
untrusted input: every field is type-checked, and anything malformed becomes a
Reply with stop="error" rather than an exception.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from timu.provider.base import Message, OnText, Reply, Stop, ToolCall
from timu.tool import Tool
from timu.types import Usage

MAX_RESPONSE = 64 * 1024 * 1024  # bytes
MAX_RETRY_AFTER = 60.0  # seconds; a longer Retry-After falls back to backoff
STOPS: dict[str, Stop] = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "length",
    "content_filter": "refused",
}
CONTEXT_HINTS = (
    "context size",
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "too many tokens",
)


@dataclass(frozen=True)
class _Failure:
    error: str
    retryable: bool
    delay: float | None = None  # from Retry-After


class OpenAIProvider:
    """extra_body is merged into every request body, e.g. OpenRouter's
    {"cache_control": {"type": "ephemeral"}} or {"reasoning": {...}}."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        extra_body: Mapping[str, Any] | None = None,
        timeout: float = 600,
        retries: int = 4,
        retry_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self._key = api_key
        self.extra_body = dict(extra_body or {})
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._sleep = sleep

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        on_text: OnText | None = None,
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [to_wire(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.extra_body,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        data = json.dumps(body).encode()
        for attempt in range(self.retries + 1):
            outcome = self._attempt(data, on_text)
            if isinstance(outcome, Reply):
                return outcome
            if not outcome.retryable or attempt == self.retries:
                return Reply("", stop="error", error=outcome.error)
            self._sleep(
                outcome.delay
                if outcome.delay is not None
                else self.retry_delay * 2**attempt
            )
        raise AssertionError("unreachable")  # the loop always returns

    def _attempt(self, data: bytes, on_text: OnText | None) -> Reply | _Failure:
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(self.url, data, headers, method="POST")
        stream = _Stream(on_text)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if "text/event-stream" not in r.headers.get("content-type", ""):
                    raw = r.read(MAX_RESPONSE + 1)  # the server ignored "stream"
                    if len(raw) > MAX_RESPONSE:
                        return _Failure("response too large", retryable=False)
                    return stream.whole(raw)
                received = 0
                while line := r.readline(MAX_RESPONSE + 1 - received):
                    received += len(line)
                    if received > MAX_RESPONSE:
                        return _Failure("response too large", retryable=False)
                    stream.feed(line)
        except urllib.error.HTTPError as e:
            with e:
                text = _clean(e.read(64 * 1024))
            if _is_context_error(text):
                return _Failure(
                    f"context length exceeded: {text[:500]}", retryable=False
                )
            retryable = e.code == 429 or e.code >= 500
            return _Failure(
                f"http {e.code}: {text[:500]}", retryable, _retry_after(e.headers)
            )
        except urllib.error.URLError as e:
            refused = isinstance(
                e.reason, ConnectionRefusedError
            )  # retrying cannot help
            return _Failure(
                f"cannot reach {self.url}: {e.reason}", retryable=not refused
            )
        except (OSError, http.client.HTTPException) as e:
            return _Failure(
                f"network error: {e or type(e).__name__}", not stream.delivered
            )
        return stream.result()


def to_wire(m: Message) -> dict[str, Any]:
    """A timu Message as a chat-completions message."""
    if m.role == "tool":
        content = (
            f"error: {m.content}" if m.is_error else m.content
        )  # no is_error field
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": content}
    if m.role != "assistant":
        return {"role": m.role, "content": m.content}
    wire: dict[str, Any] = {"role": "assistant", "content": m.content, **m.extra}
    if m.tool_calls:
        wire["content"] = (
            m.content or None
        )  # null content is only valid with tool_calls
        wire["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": c.raw or json.dumps(c.arguments),
                },
            }
            for c in m.tool_calls
        ]
    return wire


class _Stream:
    """One streamed reply, reassembled from its server-sent events."""

    def __init__(self, on_text: OnText | None) -> None:
        self.on_text = on_text
        self.text: list[str] = []
        self.calls: list[dict[str, Any]] = []  # {"id", "name", "args": [str]}, by index
        self.details: list[dict[str, Any]] = []  # reasoning_details, merged by index
        self.usage: dict[str, Any] = {}
        self.finish = ""
        self.error: str | None = None
        self.events = 0
        self.done = False  # the server sent [DONE]
        self.delivered = False  # text reached on_text, so a retry would repeat it

    def feed(self, line: bytes) -> None:
        line = line.rstrip(b"\r\n")
        if not line.startswith(b"data:"):
            return  # comments (":") and other SSE fields
        data = line[5:].strip()
        if data == b"[DONE]":
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except ValueError:
            return
        if isinstance(chunk, dict):
            self.absorb(chunk)

    def whole(self, raw: bytes) -> Reply | _Failure:
        """A non-streamed response, absorbed as a single event."""
        try:
            resp = json.loads(raw)
        except ValueError:
            return _Failure(
                f"response is not JSON: {_clean(raw[:200])}", retryable=False
            )
        if not isinstance(resp, dict):
            return _Failure("response is not a JSON object", retryable=False)
        choice = _first_choice(resp)
        msg = choice.get("message")
        delta = dict(msg) if isinstance(msg, dict) else {}
        if isinstance(calls := delta.get("tool_calls"), list):
            delta["tool_calls"] = [
                {**c, "index": i} for i, c in enumerate(calls) if isinstance(c, dict)
            ]
        self.absorb({**resp, "choices": [{**choice, "delta": delta}]})
        self.done = True
        return self.result()

    def absorb(self, chunk: dict[str, Any]) -> None:
        self.events += 1
        if "error" in chunk:
            self.error = json.dumps(chunk["error"])
            return
        if isinstance(usage := chunk.get("usage"), dict):
            self.usage = usage
        choice = _first_choice(chunk)
        if isinstance(finish := choice.get("finish_reason"), str):
            self.finish = finish
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        if isinstance(text := delta.get("content"), str) and text:
            self.text.append(text)
            if self.on_text:
                self.on_text(text)
                self.delivered = True
        if isinstance(calls := delta.get("tool_calls"), list):
            self._merge_calls(calls)
        if isinstance(details := delta.get("reasoning_details"), list):
            self._merge_details(details)

    def _merge_calls(self, deltas: list[Any]) -> None:
        """Pieces keyed by index: arguments are appended, id and name are set."""
        for d in deltas:
            idx = d.get("index") if isinstance(d, dict) else None
            if (
                not isinstance(d, dict)
                or not isinstance(idx, int)
                or not 0 <= idx < 256
            ):
                continue
            while len(self.calls) <= idx:
                self.calls.append({"id": "", "name": "", "args": []})
            call = self.calls[idx]
            fn = d.get("function")
            if not isinstance(fn, dict):
                fn = {}
            if isinstance(id_ := d.get("id"), str) and id_:
                call["id"] = id_
            if isinstance(name := fn.get("name"), str) and name:
                call["name"] = name
            if isinstance(args := fn.get("arguments"), str):
                call["args"].append(args)

    def _merge_details(self, deltas: list[Any]) -> None:
        """reasoning_details pieces: string fields are appended, others set."""
        for d in deltas:
            if not isinstance(d, dict):
                continue
            idx = d.get("index")
            into = next(
                (e for e in reversed(self.details) if e.get("index") == idx), None
            )
            if idx is None or into is None:
                self.details.append(dict(d))
                continue
            for k, v in d.items():
                old = into.get(k)
                both_str = isinstance(v, str) and isinstance(old, str)
                into[k] = old + v if both_str and k != "type" else v

    def result(self) -> Reply | _Failure:
        if self.error is not None:
            if _is_context_error(self.error):
                return _Failure(
                    f"context length exceeded: {self.error[:500]}", retryable=False
                )
            return _Failure(f"stream error: {self.error[:500]}", not self.delivered)
        if not self.events:
            return _Failure("empty response", retryable=True)
        if not self.finish and not self.done:
            return _Failure("the stream ended early", not self.delivered)
        calls = tuple(
            ToolCall(
                c["id"], c["name"], _parse_args("".join(c["args"])), "".join(c["args"])
            )
            for c in self.calls
            if c["name"]  # a call without a name cannot be dispatched or echoed back
        )
        stop = STOPS.get(self.finish, "tool_use" if calls else "end")
        if stop == "end" and calls:  # some servers say "stop" with tool calls
            stop = "tool_use"
        return Reply(
            "".join(self.text),
            calls,
            stop,
            Usage(
                input_tokens=_int(self.usage.get("prompt_tokens")),
                output_tokens=_int(self.usage.get("completion_tokens")),
                cost_usd=_num(self.usage.get("cost")),  # OpenRouter reports it
            ),
            extra={"reasoning_details": self.details} if self.details else {},
        )


def _first_choice(obj: Mapping[str, Any]) -> dict[str, Any]:
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def _parse_args(raw: str) -> dict[str, Any] | None:
    if not raw.strip():
        return {}  # some servers send nothing for a call without arguments
    try:
        args = json.loads(raw)
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _num(v: Any) -> float:
    ok = isinstance(v, int | float) and not isinstance(v, bool) and v >= 0
    return float(v) if ok else 0.0


def _clean(b: bytes) -> str:
    return b.decode("utf-8", "replace").replace("\0", "\ufffd")


def _is_context_error(body: str) -> bool:
    """OpenAI's code and llama-server's type first; wording, which varies, after."""
    if "context_length_exceeded" in body or "exceed_context_size_error" in body:
        return True
    lower = body.lower()
    return any(h in lower for h in CONTEXT_HINTS)


def _retry_after(headers: Any) -> float | None:
    """Retry-After in seconds, if given as a number and not too long."""
    try:
        secs = float(headers.get("retry-after", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return secs if 0 <= secs <= MAX_RETRY_AFTER else None
