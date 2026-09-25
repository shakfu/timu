"""Tests for the OpenAI-compatible provider, against a local HTTP server."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from timu import Agent, Role, Task, Tool, ToolOutput, Usage
from timu.provider import openai as oa
from timu.provider.base import Message, ToolCall
from timu.provider.openai import OpenAIProvider, to_wire

SSE = {"content-type": "text/event-stream"}
JSON = {"content-type": "application/json"}


@dataclass
class Resp:
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: dict(SSE))
    chunks: list[bytes] = field(default_factory=list)


@dataclass
class Server:
    url: str = ""
    responses: list[Resp] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def server() -> Iterator[Server]:
    srv = Server()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            srv.requests.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )
            r = (
                srv.responses.pop(0)
                if srv.responses
                else Resp(599, dict(JSON), [b"unscripted"])
            )
            self.send_response(r.status)
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in r.chunks:  # separate writes, so lines can arrive split
                self.wfile.write(chunk)
                self.wfile.flush()

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    srv.url = f"http://127.0.0.1:{httpd.server_port}/v1"
    yield srv
    httpd.shutdown()
    httpd.server_close()


def event(
    delta: dict[str, Any] | None = None, finish: str | None = None, **top: Any
) -> bytes:
    choice: dict[str, Any] = {"index": 0, "delta": delta or {}}
    if finish:
        choice["finish_reason"] = finish
    return f"data: {json.dumps({'choices': [choice], **top})}\n\n".encode()


DONE = b"data: [DONE]\n\n"
USAGE = b'data: {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.002}}\n\n'
ECHO = Tool(
    "echo", "Echo text.", {"type": "object"}, frozenset(), lambda c, a: ToolOutput("")
)


def make(server: Server, **kw: Any) -> tuple[OpenAIProvider, list[float]]:
    delays: list[float] = []
    kw.setdefault("sleep", delays.append)
    return OpenAIProvider(server.url, "test-model", "sk-test", **kw), delays


# ---- success ----


def test_streamed_text(server: Server) -> None:
    server.responses.append(
        Resp(
            chunks=[
                b": keep-alive comment\n\n",
                event({"role": "assistant", "content": "Hel"})[:20],  # split mid-line
                event({"role": "assistant", "content": "Hel"})[20:],
                event({"content": "lo"}, finish="stop"),
                USAGE,
                DONE,
            ]
        )
    )
    provider, _ = make(server, extra_body={"cache_control": {"type": "ephemeral"}})
    pieces: list[str] = []
    reply = provider.complete(
        [Message("system", "sys"), Message("user", "hi")], [ECHO], pieces.append
    )

    assert reply.stop == "end"
    assert reply.text == "Hello"
    assert pieces == ["Hel", "lo"]
    assert reply.usage == Usage(input_tokens=12, output_tokens=3, cost_usd=0.002)
    req = server.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    assert req["body"]["model"] == "test-model"
    assert req["body"]["stream"] is True
    assert req["body"]["cache_control"] == {"type": "ephemeral"}
    assert req["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert req["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text.",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_no_key_no_auth_header_and_no_tools(server: Server) -> None:
    server.responses.append(Resp(chunks=[event({"content": "x"}, finish="stop"), DONE]))
    OpenAIProvider(server.url, "m").complete([Message("user", "hi")], [])
    req = server.requests[0]
    assert "Authorization" not in req["headers"]
    assert "tools" not in req["body"]


def test_tool_calls_merged_by_index(server: Server) -> None:
    def call_delta(index: int, **kw: Any) -> dict[str, Any]:
        return {"tool_calls": [{"index": index, **kw}]}

    server.responses.append(
        Resp(
            chunks=[
                event(
                    call_delta(
                        0,
                        id="c1",
                        type="function",
                        function={"name": "echo", "arguments": ""},
                    )
                ),
                event(call_delta(0, function={"arguments": '{"te'})),
                event(call_delta(1, id="c2", function={"name": "noargs"})),
                event(call_delta(0, function={"arguments": 'xt": "a"}'})),
                event(
                    call_delta(
                        2, id="c3", function={"name": "bad", "arguments": "{nope"}
                    )
                ),
                event(finish="tool_calls"),
                DONE,
            ]
        )
    )
    reply = make(server)[0].complete([Message("user", "hi")], [ECHO])
    assert reply.stop == "tool_use"
    assert reply.tool_calls == (
        ToolCall("c1", "echo", {"text": "a"}, '{"text": "a"}'),
        ToolCall("c2", "noargs", {}, ""),
        ToolCall("c3", "bad", None, "{nope"),
    )


def test_stop_with_tool_calls_is_tool_use(server: Server) -> None:
    delta = {
        "tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "echo", "arguments": "{}"}}
        ]
    }
    server.responses.append(Resp(chunks=[event(delta, finish="stop"), DONE]))
    assert make(server)[0].complete([], []).stop == "tool_use"


@pytest.mark.parametrize(
    ("finish", "stop"), [("length", "length"), ("content_filter", "refused")]
)
def test_finish_reasons(server: Server, finish: str, stop: str) -> None:
    server.responses.append(Resp(chunks=[event({"content": "x"}, finish=finish), DONE]))
    assert make(server)[0].complete([], []).stop == stop


def test_finish_without_done_is_complete(server: Server) -> None:
    server.responses.append(Resp(chunks=[event({"content": "x"}, finish="stop")]))
    reply = make(server)[0].complete([], [])
    assert (reply.stop, reply.text) == ("end", "x")


def test_reasoning_details_round_trip(server: Server) -> None:
    server.responses.append(
        Resp(
            chunks=[
                event(
                    {
                        "reasoning_details": [
                            {"index": 0, "type": "reasoning.text", "text": "thi"}
                        ]
                    }
                ),
                event(
                    {
                        "reasoning_details": [
                            {
                                "index": 0,
                                "type": "reasoning.text",
                                "text": "nk",
                                "signature": "s",
                            }
                        ]
                    }
                ),
                event({"content": "ok"}, finish="stop"),
                DONE,
            ]
        )
    )
    reply = make(server)[0].complete([], [])
    details = [
        {"index": 0, "type": "reasoning.text", "text": "think", "signature": "s"}
    ]
    assert reply.extra == {"reasoning_details": details}
    wire = to_wire(Message("assistant", "ok", extra=reply.extra))
    assert wire["reasoning_details"] == details


def test_non_streamed_json(server: Server) -> None:
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "calling",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text": "x"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    server.responses.append(
        Resp(headers=dict(JSON), chunks=[json.dumps(body).encode()])
    )
    pieces: list[str] = []
    reply = make(server)[0].complete([], [], pieces.append)
    assert reply.stop == "tool_use"
    assert reply.text == "calling"
    assert pieces == ["calling"]
    assert reply.tool_calls == (ToolCall("c1", "echo", {"text": "x"}, '{"text": "x"}'),)
    assert reply.usage == Usage(input_tokens=5, output_tokens=2)


# ---- failures and retries ----


def test_retries_429_and_5xx_then_succeeds(server: Server) -> None:
    server.responses += [
        Resp(429, {**JSON, "retry-after": "3"}, [b'{"error": "slow down"}']),
        Resp(503, dict(JSON), [b"unavailable"]),
        Resp(chunks=[event({"content": "ok"}, finish="stop"), DONE]),
    ]
    provider, delays = make(server)
    reply = provider.complete([], [])
    assert (reply.stop, reply.text) == ("end", "ok")
    assert delays == [3.0, 2.0]  # Retry-After, then backoff for attempt 1


def test_retries_give_up(server: Server) -> None:
    server.responses += [Resp(500, dict(JSON), [b"boom"]) for _ in range(3)]
    provider, delays = make(server, retries=2)
    reply = provider.complete([], [])
    assert reply.stop == "error"
    assert reply.error == "http 500: boom"
    assert delays == [1.0, 2.0]
    assert len(server.requests) == 3


def test_client_error_is_not_retried(server: Server) -> None:
    server.responses.append(Resp(401, dict(JSON), [b'{"error": "bad key"}']))
    reply = make(server)[0].complete([], [])
    assert reply.error == 'http 401: {"error": "bad key"}'
    assert len(server.requests) == 1


def test_context_error_is_not_retried(server: Server) -> None:
    body = b'{"error": {"code": "context_length_exceeded", "message": "too long"}}'
    server.responses.append(Resp(400, dict(JSON), [body]))
    reply = make(server)[0].complete([], [])
    assert reply.error.startswith("context length exceeded")
    assert len(server.requests) == 1


def test_truncated_stream_is_retried(server: Server) -> None:
    server.responses += [
        Resp(chunks=[event({"role": "assistant"})]),  # no finish, no [DONE]
        Resp(chunks=[event({"content": "ok"}, finish="stop"), DONE]),
    ]
    reply = make(server)[0].complete([], [])
    assert reply.text == "ok"
    assert len(server.requests) == 2


def test_no_retry_after_text_was_delivered(server: Server) -> None:
    server.responses += [
        Resp(chunks=[event({"content": "partial"})]),
        Resp(chunks=[event({"content": "again"}, finish="stop"), DONE]),
    ]
    pieces: list[str] = []
    reply = make(server)[0].complete([], [], pieces.append)
    assert reply.error == "the stream ended early"
    assert pieces == ["partial"]
    assert len(server.requests) == 1


def test_error_mid_stream(server: Server) -> None:
    server.responses += [
        Resp(chunks=[b'data: {"error": {"message": "overloaded"}}\n\n']),
        Resp(chunks=[b'data: {"error": {"message": "prompt is too long"}}\n\n']),
    ]
    reply = make(server)[0].complete([], [])
    assert reply.error.startswith("context length exceeded")  # the retry hit this
    assert len(server.requests) == 2


def test_connection_refused_is_not_retried() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the request
    delays: list[float] = []
    reply = OpenAIProvider(
        f"http://127.0.0.1:{port}/v1", "m", sleep=delays.append
    ).complete([], [])
    assert reply.stop == "error"
    assert "cannot reach" in reply.error
    assert delays == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        b'{"choices": "x"}',
        b'{"choices": [{"message": {"content": 5, "tool_calls": "x"}}], "usage": {"prompt_tokens": "9"}}',
        b'{"choices": [{"message": {"tool_calls": [{"id": 1, "function": {"name": 2}}]}}]}',
    ],
)
def test_malformed_responses_do_not_raise(server: Server, body: bytes) -> None:
    server.responses.append(Resp(headers=dict(JSON), chunks=[body]))
    reply = make(server, retries=0)[0].complete([], [])
    assert reply.stop in ("end", "error")
    assert reply.usage.tokens == 0


def test_malformed_stream_events_are_skipped(server: Server) -> None:
    server.responses.append(
        Resp(
            chunks=[
                b"data: {not json\n\n",
                b"data: [1]\n\n",
                event({"content": 7}),
                event({"tool_calls": [{"index": -1}, {"index": "0"}, "x"]}),
                event({"content": "ok"}, finish="stop"),
                DONE,
            ]
        )
    )
    reply = make(server)[0].complete([], [])
    assert (reply.stop, reply.text, reply.tool_calls) == ("end", "ok", ())


def test_response_size_cap(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oa, "MAX_RESPONSE", 100)
    server.responses.append(
        Resp(chunks=[event({"content": "x" * 200}, finish="stop"), DONE])
    )
    assert make(server)[0].complete([], []).error == "response too large"


def test_agent_turn_through_the_provider(server: Server, tmp_path: Any) -> None:
    """A tool call and its result go back to the server in chat-completions form."""
    delta = {
        "tool_calls": [
            {
                "index": 0,
                "id": "c1",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
        ]
    }
    server.responses += [
        Resp(chunks=[event(delta, finish="tool_calls"), USAGE, DONE]),
        Resp(chunks=[event({"content": "done"}, finish="stop"), USAGE, DONE]),
    ]
    echo = Tool(
        "echo", "", {"type": "object"}, frozenset(), lambda c, a: ToolOutput(a["text"])
    )
    events: list[Any] = []
    result = Agent(
        Role("r", "sys", (echo,)), make(server)[0], events.append, tmp_path
    ).run(Task("go"))

    assert (result.status, result.summary) == ("done", "done")
    assert result.usage == Usage(
        turns=2, tool_calls=1, input_tokens=24, output_tokens=6, cost_usd=0.004
    )
    assert server.requests[1]["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "hi"},
    ]
    assert [e.data["text"] for e in events if e.kind == "model_delta"] == ["done"]


# ---- wire format ----


def test_to_wire() -> None:
    call = ToolCall("c1", "echo", {"text": "a"}, "")
    assert to_wire(Message("assistant", "", (call,))) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "a"}'},
            }
        ],
    }
    assert to_wire(Message("assistant", "done")) == {
        "role": "assistant",
        "content": "done",
    }
    assert to_wire(Message("tool", "boom", tool_call_id="c1", is_error=True)) == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "error: boom",
    }
