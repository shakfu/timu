"""The anthropic and openai provider types, through their SDKs, against a local HTTP
server that replays recorded server-sent events."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from http_server import JSON, Resp, Server
from timu import Tool, ToolOutput, Usage
from timu.config import ConfigError, load_config
from timu.provider import factory
from timu.provider.anthropic_sdk import AnthropicProvider
from timu.provider.anthropic_sdk import from_settings as anthropic_provider
from timu.provider.anthropic_sdk import to_wire as anthropic_wire
from timu.provider.base import CONTEXT_OVERFLOW, Message, ProviderSettings, ToolCall
from timu.provider.openai_sdk import OpenAIResponsesProvider
from timu.provider.openai_sdk import from_settings as openai_provider
from timu.provider.openai_sdk import to_wire as openai_wire
from timu.provider.testing import reply_problems
from timu.types import Capability

READ = Tool(
    "read",
    "Read a file.",
    {"type": "object", "properties": {"path": {"type": "string"}}},
    frozenset({Capability.FS_READ}),
    lambda ctx, args: ToolOutput("x"),
)
CONVERSATION = [Message("system", "be brief"), Message("user", "read a.py")]


def sse(*events: dict[str, Any]) -> list[bytes]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]


def no_wait(_: float) -> None:
    pass


# ---- anthropic ----


def claude(server: Server, **options: Any) -> AnthropicProvider:
    p = anthropic_provider(
        ProviderSettings(
            "claude-test",
            server.url.removesuffix("/v1"),
            "sk-ant",
            timeout=10,
            options=options,
        )
    )
    p._sleep = no_wait
    return p


def message_stream(
    *blocks: tuple[dict[str, Any], list[dict[str, Any]]],
    stop: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> list[bytes]:
    """A Messages API stream: each block's start and its deltas."""
    start_usage = usage or {
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 3,
    }
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": start_usage,
            },
        }
    ]
    for i, (block, deltas) in enumerate(blocks):
        events.append(
            {"type": "content_block_start", "index": i, "content_block": block}
        )
        events += [
            {"type": "content_block_delta", "index": i, "delta": d} for d in deltas
        ]
        events.append({"type": "content_block_stop", "index": i})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 20},
        }
    )
    events.append({"type": "message_stop"})
    return sse(*events)


THINK = (
    {"type": "thinking", "thinking": "", "signature": ""},
    [
        {"type": "thinking_delta", "thinking": "look at a.py"},
        {"type": "signature_delta", "signature": "sig-1"},
    ],
)
TEXT = (
    {"type": "text", "text": ""},
    [{"type": "text_delta", "text": "Hel"}, {"type": "text_delta", "text": "lo"}],
)
USE = (
    {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
    [
        {"type": "input_json_delta", "partial_json": '{"path": '},
        {"type": "input_json_delta", "partial_json": '"a.py"}'},
    ],
)


def test_anthropic_streams_text_thinking_and_tool_calls(server: Server) -> None:
    server.responses.append(
        Resp(chunks=message_stream(THINK, TEXT, USE, stop="tool_use"))
    )
    streamed: list[str] = []
    reply = claude(server).complete(CONVERSATION, [READ], streamed.append)
    assert (reply.text, streamed, reply.stop) == ("Hello", ["Hel", "lo"], "tool_use")
    assert reply.tool_calls == (
        ToolCall("toolu_1", "read", {"path": "a.py"}, '{"path": "a.py"}'),
    )
    assert reply.usage == Usage(
        input_tokens=18,
        output_tokens=20,
        cached_input_tokens=5,
        cache_write_input_tokens=3,
    )
    assert not reply.cost_known and reply_problems(reply) == []
    thinking = reply.extra["content"][0]
    assert (thinking["type"], thinking["signature"]) == ("thinking", "sig-1")
    request = server.requests[0]
    assert request["path"] == "/v1/messages"
    sent = request["headers"]  # the SDK picks the header: x-api-key or a bearer token
    assert "sk-ant" in sent.get("x-api-key", "") + sent.get("Authorization", "")
    body = request["body"]
    assert (body["system"], body["max_tokens"], body["stream"]) == (
        "be brief",
        64000,
        True,
    )
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["input_schema"] == READ.parameters


def test_anthropic_wire_joins_user_turns_and_replays_blocks() -> None:
    blocks = [{"type": "thinking", "thinking": "t", "signature": "s"}]
    system, wire = anthropic_wire(
        [
            *CONVERSATION,
            Message(
                "assistant",
                "two reads",
                (ToolCall("t1", "read", {"path": "a"}), ToolCall("t2", "read", None)),
            ),
            Message("tool", "A", tool_call_id="t1"),
            Message("tool", "bad args", tool_call_id="t2", is_error=True),
            Message("user", "now b"),
            Message("assistant", "", extra={"content": blocks}),
            Message("assistant", ""),  # nothing to send
        ]
    )
    assert system == "be brief"
    assert [t["role"] for t in wire] == ["user", "assistant", "user", "assistant"]
    assert wire[1]["content"][2] == {
        "type": "tool_use",
        "id": "t2",
        "name": "read",
        "input": {},
    }
    assert wire[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
        {
            "type": "tool_result",
            "tool_use_id": "t2",
            "content": "bad args",
            "is_error": True,
        },
        {"type": "text", "text": "now b"},
    ]
    assert wire[3]["content"] == blocks


def test_anthropic_retries_overload_and_stops_on_a_client_error(server: Server) -> None:
    overloaded = json.dumps({"type": "error", "error": {"type": "overloaded_error"}})
    server.responses += [
        Resp(529, dict(JSON), [overloaded.encode()]),
        Resp(chunks=message_stream(TEXT)),
        Resp(400, dict(JSON), [b'{"type": "error", "error": {"message": "bad"}}']),
    ]
    p = claude(server)
    assert p.complete(CONVERSATION, []).text == "Hello"
    reply = p.complete(CONVERSATION, [])
    assert reply.stop == "error" and reply.error.startswith("http 400")
    assert len(server.requests) == 3


def test_anthropic_error_mid_stream_retries_and_marks_the_restart(
    server: Server,
) -> None:
    cut = message_stream(TEXT)[:4]  # message_start, the text block start, two deltas
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    server.responses += [
        Resp(chunks=[*cut, *sse(error)]),
        Resp(chunks=message_stream(TEXT)),
    ]
    streamed: list[str] = []
    reply = claude(server).complete(CONVERSATION, [], streamed.append)
    assert reply.text == "Hello"
    assert streamed[:2] == ["Hel", "lo"] and streamed[-2:] == ["Hel", "lo"]
    assert "retrying the reply" in streamed[2]


def test_anthropic_long_prompt_is_a_context_overflow(server: Server) -> None:
    error = {"type": "error", "error": {"message": "prompt is too long: 9 > 8"}}
    server.responses.append(Resp(400, dict(JSON), [json.dumps(error).encode()]))
    reply = claude(server).complete(CONVERSATION, [])
    assert reply.error.startswith(CONTEXT_OVERFLOW)
    server.responses.append(
        Resp(chunks=message_stream(stop="model_context_window_exceeded"))
    )
    assert claude(server).complete(CONVERSATION, []).error.startswith(CONTEXT_OVERFLOW)


def test_anthropic_drops_thinking_from_an_edited_history(server: Server) -> None:
    """A trimmed history invalidates later thinking blocks; one retry without them."""
    bound = {
        "type": "error",
        "error": {
            "message": "messages.1.content.0: Invalid `signature` in `thinking` block. "
            "The block is bound to a different conversation."
        },
    }
    server.responses += [
        Resp(400, dict(JSON), [json.dumps(bound).encode()]),
        Resp(chunks=message_stream(TEXT)),
    ]
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "text", "text": "hi"},
    ]
    history = [*CONVERSATION, Message("assistant", "hi", extra={"content": blocks})]
    reply = claude(server).complete([*history, Message("user", "go on")], [])
    assert reply.text == "Hello"
    first, second = (r["body"]["messages"][1]["content"] for r in server.requests)
    assert [b["type"] for b in first] == ["thinking", "text"]
    assert [b["type"] for b in second] == ["text"]


@pytest.mark.parametrize(
    ("stop", "mapped"), [("max_tokens", "length"), ("refusal", "refused")]
)
def test_anthropic_stop_reasons(server: Server, stop: str, mapped: str) -> None:
    server.responses.append(Resp(chunks=message_stream(TEXT, stop=stop)))
    assert claude(server).complete(CONVERSATION, []).stop == mapped


def test_anthropic_options(server: Server) -> None:
    server.responses.append(Resp(chunks=message_stream(TEXT)))
    p = claude(server, max_tokens=100, cache=False, thinking={"type": "adaptive"})
    p.complete(CONVERSATION, [])
    body = server.requests[0]["body"]
    assert (body["max_tokens"], body["thinking"]) == (100, {"type": "adaptive"})
    assert "cache_control" not in body
    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        claude(server, max_tokens=0)


# ---- openai ----


def gpt(server: Server, **options: Any) -> OpenAIResponsesProvider:
    p = openai_provider(
        ProviderSettings("gpt-test", server.url, "sk-oa", timeout=10, options=options)
    )
    p._sleep = no_wait
    return p


OUTPUT: list[dict[str, Any]] = [
    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
    {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Hi", "annotations": []}],
    },
    {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read",
        "arguments": '{"path": "a.py"}',
        "status": "completed",
    },
]


def response_stream(
    output: list[dict[str, Any]], status: str = "completed", **extra: Any
) -> list[bytes]:
    response = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-test",
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        **extra,
    }
    return sse(
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "output": [], "status": "in_progress"},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_1",
            "output_index": 1,
            "content_index": 0,
            "delta": "Hi",
            "logprobs": [],
        },
        {"type": f"response.{status}", "sequence_number": 2, "response": response},
    )


def test_openai_streams_text_reasoning_and_calls(server: Server) -> None:
    server.responses.append(Resp(chunks=response_stream(OUTPUT)))
    streamed: list[str] = []
    reply = gpt(server).complete(CONVERSATION, [READ], streamed.append)
    assert (reply.text, streamed, reply.stop) == ("Hi", ["Hi"], "tool_use")
    assert reply.tool_calls == (
        ToolCall("call_1", "read", {"path": "a.py"}, '{"path": "a.py"}'),
    )
    assert reply.usage == Usage(input_tokens=10, output_tokens=5, cached_input_tokens=4)
    assert not reply.cost_known and reply_problems(reply) == []
    request = server.requests[0]
    assert request["path"] == "/v1/responses"
    assert request["headers"]["authorization"] == "Bearer sk-oa"
    body = request["body"]
    assert (body["store"], body["include"]) == (False, ["reasoning.encrypted_content"])
    assert body["instructions"] == "be brief"
    assert body["input"] == [{"role": "user", "content": "read a.py"}]
    assert body["tools"][0]["name"] == "read" and body["tools"][0]["strict"] is False
    # the next turn sends the output back, reasoning included
    _, items = openai_wire(
        [
            *CONVERSATION,
            Message("assistant", reply.text, reply.tool_calls, extra=reply.extra),
            Message("tool", "x = 1", tool_call_id="call_1"),
        ]
    )
    assert [i.get("type") for i in items[1:4]] == [
        "reasoning",
        "message",
        "function_call",
    ]
    assert items[1]["encrypted_content"] == "enc"
    assert items[4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "x = 1",
    }


def test_openai_wire_without_recorded_output() -> None:
    _, items = openai_wire(
        [
            Message("user", "u"),
            Message("assistant", "a", (ToolCall("c1", "read", {"path": "a"}),)),
            Message("tool", "nope", tool_call_id="c1", is_error=True),
        ]
    )
    assert items[1:] == [
        {"role": "assistant", "content": "a"},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "read",
            "arguments": '{"path": "a"}',
        },
        {"type": "function_call_output", "call_id": "c1", "output": "error: nope"},
    ]


def test_openai_incomplete_and_failed_responses(server: Server) -> None:
    server.responses += [
        Resp(
            chunks=response_stream(
                OUTPUT[1:2],
                "incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            )
        ),
        Resp(
            chunks=response_stream(
                [],
                "failed",
                error={"code": "context_length_exceeded", "message": "too long"},
            )
        ),
    ]
    p = gpt(server)
    assert p.complete(CONVERSATION, []).stop == "length"
    reply = p.complete(CONVERSATION, [])
    assert reply.stop == "error" and reply.error.startswith(CONTEXT_OVERFLOW)


def test_openai_retries_server_errors_but_not_an_empty_quota(server: Server) -> None:
    quota = {"error": {"code": "insufficient_quota", "message": "no credit"}}
    server.responses += [
        Resp(500, dict(JSON), [b'{"error": {"message": "oops"}}']),
        Resp(chunks=response_stream(OUTPUT[1:2])),
        Resp(429, dict(JSON), [json.dumps(quota).encode()]),
    ]
    p = gpt(server)
    assert p.complete(CONVERSATION, []).text == "Hi"
    assert p.complete(CONVERSATION, []).error.startswith("http 429")
    assert len(server.requests) == 3


def test_openai_options_replace_the_defaults(server: Server) -> None:
    server.responses.append(Resp(chunks=response_stream(OUTPUT[1:2])))
    gpt(server, include=[], reasoning={"effort": "low"}).complete(CONVERSATION, [])
    body = server.requests[0]["body"]
    assert (body["include"], body["reasoning"], body["store"]) == (
        [],
        {"effort": "low"},
        False,
    )


# ---- choosing the type ----


def test_the_sdk_types_are_built_in() -> None:
    assert factory("anthropic") is anthropic_provider
    assert factory("openai") is openai_provider


def test_a_missing_sdk_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    factory.cache_clear()
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import fails as if absent
    monkeypatch.delitem(sys.modules, "timu.provider.anthropic_sdk")
    try:
        with pytest.raises(LookupError, match=r"uv tool install 'timu\[extras\]'"):
            factory("anthropic")
    finally:
        factory.cache_clear()


@pytest.mark.parametrize(
    ("kind", "env"), [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")]
)
def test_each_sdk_type_checks_its_own_key(tmp_path: Path, kind: str, env: str) -> None:
    config_file = tmp_path / "timu.toml"
    config_file.write_text(f'[provider]\ntype = "{kind}"\nmodel = "m"\n')
    config = load_config(config_file, env={})
    assert (config.provider_type, config.api_key_env, config.base_url) == (
        kind,
        env,
        "",
    )
    with pytest.raises(ConfigError, match=f"{env} is not set"):
        config.provider("coder", env={})
    assert getattr(config.provider("coder", env={env: "sk"}), "model", "") == "m"
