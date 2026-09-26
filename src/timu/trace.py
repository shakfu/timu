"""Traces (design 10): load a JSONL trace, build its agent tree, and replay it.

Replay feeds the recorded model replies to a FakeProvider, and the recorded results
of tools with outside inputs (the web) to stand-in tools. Local tools run for real.
compare then checks that timu made the same decisions: the same events, tool calls,
error flags and statuses. Tool output text is not compared, since shell output holds
temp paths and timings that differ between runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from timu.events import Event, as_dict
from timu.provider.base import Reply, ToolCall
from timu.provider.fake import FakeProvider
from timu.tool import Context, Tool, ToolOutput
from timu.types import Capability, Usage

IGNORED = frozenset({"model_delta", "warning", "config"})  # chunks; environment notices


class TraceError(ValueError):
    """An unreadable trace. The message names the file and line."""


def load(path: str | Path) -> list[Event]:
    """The events in a JSONL trace. Raises TraceError naming the bad line."""
    events = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise TraceError(f"{path}: {e.strerror}") from None
    for n, line in enumerate(lines, 1):
        try:
            d = json.loads(line)
        except ValueError:
            raise TraceError(f"{path}:{n}: not a JSON object") from None
        fields = (
            ("kind", str),
            ("agent_id", str),
            ("parent_id", str),
            ("role", str),
            ("data", dict),
        )
        if not isinstance(d, dict) or not all(
            isinstance(d.get(k), t) for k, t in fields
        ):
            raise TraceError(f"{path}:{n}: not an event")
        ts = d.get("ts")
        if not isinstance(ts, int | float) or isinstance(ts, bool):
            raise TraceError(f"{path}:{n}: not an event")
        node = d.get("node", "")
        if not isinstance(node, str):
            raise TraceError(f"{path}:{n}: not an event")
        events.append(
            Event(
                d["kind"],
                d["agent_id"],
                d["parent_id"],
                d["role"],
                float(ts),
                d["data"],
                node,
            )
        )
    return events


def substitute(
    events: Sequence[Event], pairs: Sequence[tuple[str, str]]
) -> list[Event]:
    """events with each (old, new) replaced in every string, in order. An old that
    starts with a word character and ends with one matches whole words only, so a short
    username does not change other words. Used to keep local paths and names out of
    recorded traces, and to put a replay's own paths back in."""
    rules = []
    for old, new in pairs:
        if not old:
            continue
        pattern = re.escape(old)
        if re.match(r"\w", old) and re.search(r"\w$", old):
            pattern = rf"\b{pattern}\b"
        rules.append((re.compile(pattern), new.replace("\\", "\\\\")))  # a literal

    def fix(v: Any) -> Any:
        if isinstance(v, str):
            for rx, new in rules:
                v = rx.sub(new, v)
            return v
        if isinstance(v, list):
            return [fix(x) for x in v]
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v

    return [replace(e, data=fix(dict(e.data))) for e in events]


def save(events: Sequence[Event], path: str | Path) -> None:
    Path(path).write_text(
        "".join(json.dumps(as_dict(e)) + "\n" for e in events), encoding="utf-8"
    )


# ---- the agent tree ----


@dataclass
class Node:
    agent_id: str
    role: str
    goal: str
    status: str = "running"  # until a result event
    summary: str = ""
    usage: Usage = field(default_factory=Usage)
    untrusted: bool = False
    children: list[Node] = field(default_factory=list)
    graph_node: str = ""


def tree(events: Sequence[Event]) -> list[Node]:
    """Agents as a forest: each agent under the agent that delegated to it."""
    nodes: dict[str, Node] = {}
    roots: list[Node] = []
    for e in events:
        if e.kind == "start":
            node = Node(e.agent_id, e.role, str(e.data.get("goal", "")))
            node.graph_node = e.node
            nodes[e.agent_id] = node
            parent = nodes.get(e.parent_id)
            (parent.children if parent else roots).append(node)
        elif e.kind == "result" and e.agent_id in nodes:
            node = nodes[e.agent_id]
            node.status = str(e.data.get("status", "?"))
            node.summary = str(e.data.get("summary", ""))
            node.untrusted = e.data.get("untrusted") is True
            node.usage = _usage(e.data.get("usage"))
    return roots


def render(events: Sequence[Event]) -> str:
    """The agent tree, one agent per line, under a line for the run. In a graph run,
    each top-level agent is prefixed with its node."""
    roots = tree(events)
    total = Usage()
    stack = list(roots)
    while stack:
        node = stack.pop()
        total += node.usage
        stack.extend(node.children)
    outcome = next((e for e in reversed(events) if e.kind == "workflow_result"), None)
    status = outcome.data.get("status", "?") if outcome else "unfinished"
    run = f"run {outcome.agent_id}" if outcome else "run"
    lines = [f"{run} {status}: {_cost(total)}"]

    def walk(node: Node, depth: int) -> None:
        mark = " untrusted" if node.untrusted else ""
        where = f"[{node.graph_node}] " if depth == 1 and node.graph_node else ""
        lines.append(
            f"{'  ' * depth}{where}{node.agent_id} {node.role} {node.status}: "
            f"{_cost(node.usage)}{mark}"
        )
        for child in node.children:
            walk(child, depth + 1)

    for root in roots:
        walk(root, 1)
    if outcome and outcome.data.get("summary"):
        lines.append(f"summary: {outcome.data['summary']}")
    return "\n".join(lines)


def _cost(u: Usage) -> str:
    cost = f", ${u.cost_usd:.4f}" if u.cost_usd else ""
    turns = "turn" if u.turns == 1 else "turns"
    tool_calls = "tool call" if u.tool_calls == 1 else "tool calls"
    tokens = "token" if u.tokens == 1 else "tokens"
    return f"{u.turns} {turns}, {u.tool_calls} {tool_calls}, {u.tokens} {tokens}{cost}"


def _usage(d: Any) -> Usage:
    if not isinstance(d, dict):
        return Usage()
    names = ("turns", "tool_calls", "input_tokens", "output_tokens", "cost_usd")
    return Usage(**{k: d[k] for k in names if isinstance(d.get(k), int | float)})


# ---- replay ----


def replies(events: Sequence[Event]) -> list[Reply]:
    """The recorded model replies, in the order the provider returned them."""
    out = []
    for e in events:
        if e.kind != "model_reply":
            continue
        calls = tuple(
            ToolCall(c["id"], c["name"], c["arguments"], c.get("raw", ""))
            for c in e.data.get("tool_calls", [])
        )
        usage = _reply_usage(_usage(e.data.get("usage")))
        out.append(
            Reply(
                e.data.get("text", ""),
                calls,
                e.data.get("stop", "end"),
                usage,
                e.data.get("error", ""),
            )
        )
    return out


def _reply_usage(u: Usage) -> Usage:
    """A Reply's usage leaves turns and tool calls to the agent."""
    return Usage(
        input_tokens=u.input_tokens, output_tokens=u.output_tokens, cost_usd=u.cost_usd
    )


def replay_provider(events: Sequence[Event]) -> FakeProvider:
    return FakeProvider(replies(events))


def recorded_tool(
    events: Sequence[Event], name: str, needs: frozenset[Capability]
) -> Tool:
    """A stand-in for tool name that returns its recorded results in order. Calls the
    agent skipped (budget, cancel, repeats) are left out, since replay skips them too."""
    calls: dict[tuple[str, str], bool] = {}
    queue: list[ToolOutput] = []
    for e in events:
        if e.kind == "tool_call" and e.data.get("name") == name:
            calls[(e.agent_id, e.data["id"])] = True
        elif e.kind == "tool_result" and calls.pop(
            (e.agent_id, e.data.get("id", "")), False
        ):
            text, err = str(e.data.get("text", "")), e.data.get("is_error") is True
            if not (err and text.startswith("skipped: ")):
                queue.append(ToolOutput(text, err, untrusted=Capability.NET in needs))

    def run(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
        if not queue:
            return ToolOutput(
                f"replay: no recorded result left for {name}", is_error=True
            )
        return queue.pop(0)

    return Tool(name, f"recorded {name}", {"type": "object"}, needs, run)


def _key(e: Event) -> tuple[Any, ...]:
    """What replay must reproduce for one event."""
    d = e.data
    who = (
        ("run", "", "workflow")
        if e.role == "workflow"
        else (e.agent_id, e.parent_id, e.role)
    )
    detail: Any
    match e.kind:
        case "start":
            detail = d.get("goal")
        case "model_reply":
            calls = [
                (c["name"], json.dumps(c["arguments"], sort_keys=True))
                for c in d.get("tool_calls", [])
            ]
            detail = (d.get("stop"), calls)
        case "tool_call":
            detail = (d.get("name"), json.dumps(d.get("arguments"), sort_keys=True))
        case "tool_result":
            detail = d.get("is_error")
        case "result" | "workflow_result":
            detail = (d.get("status"), d.get("untrusted"))
        case _:
            detail = json.dumps(d, sort_keys=True)
    return (e.kind, *who, detail)


def compare(recorded: Sequence[Event], replayed: Sequence[Event]) -> str | None:
    """None if replayed reproduces recorded, else where they first differ. Trace line
    numbers refer to the recorded trace."""
    a = [(n, _key(e)) for n, e in enumerate(recorded, 1) if e.kind not in IGNORED]
    b = [_key(e) for e in replayed if e.kind not in IGNORED]
    for i, (line, key) in enumerate(a):
        if i >= len(b):
            return f"trace line {line}: replay ended early; expected {key}"
        if b[i] != key:
            return f"trace line {line}: expected {key}, replay gave {b[i]}"
    if len(b) > len(a):
        return f"replay has {len(b) - len(a)} events more than the trace; next: {b[len(a)]}"
    return None
