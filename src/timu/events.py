"""Events agents emit instead of printing, and sinks that receive them.

The event kinds and their data keys are public API: a UI reads them, and the trace
stores them. SCHEMA lists every kind; a key in brackets below is optional. An agent's
events carry its agent_id, parent_id (the delegating agent, or "") and role. A
workflow or graph event carries the run id as agent_id and role "workflow". node is
the graph node, or "" outside a graph. Usage values are dicts of types.Usage.

    config          source                     the config file and env overrides read
    run             workflow, workdir          a workflow starts
    resume          run, nodes, again          nodes reused from run; again: node -> why
    warning         message
    start           goal, model                an agent starts
    model_delta     text                       streamed model text; display only
    model_reply     text, stop, tool_calls, usage, error
    tool_call       id, name, arguments, raw   arguments is None if raw is not JSON
    tool_result     id, text, is_error
    trim            characters, reason         old tool output removed from context
    result          status, summary, usage, untrusted      an agent ends
    approval        artifact, role, approved   the untrusted-content gate answered
    round           n, stage, [verdict]        a workflow step: code, review, verdict...
    command         command, network           a commands step starts
    command_result  command, result
    workflow_result status, summary            a workflow ends
    node            repo, project, workflow    a graph node starts
    node_result     node, status, summary, branch, base, head, workdir, files, spec,
                    reused, outputs            a graph node ends; enough to resume it

New kinds and new optional keys may be added in a minor release. A UI should ignore
kinds it does not know.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self

EventSink = Callable[["Event"], None]

# kind -> (required data keys, optional data keys); see the module docstring
SCHEMA: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    kind: (frozenset(req.split()), frozenset(opt.split()))
    for kind, req, opt in (
        ("config", "source", ""),
        ("run", "workflow workdir", ""),
        ("resume", "run nodes again", ""),
        ("warning", "message", ""),
        ("start", "goal model", ""),
        ("model_delta", "text", ""),
        ("model_reply", "text stop tool_calls usage error", ""),
        ("tool_call", "id name arguments raw", ""),
        ("tool_result", "id text is_error", ""),
        ("trim", "characters reason", ""),
        ("result", "status summary usage untrusted", ""),
        ("approval", "artifact role approved", ""),
        ("round", "n stage", "verdict"),
        ("command", "command network", ""),
        ("command_result", "command result", ""),
        ("workflow_result", "status summary", ""),
        ("node", "repo project workflow", ""),
        (
            "node_result",
            "node status summary branch base head workdir files spec reused outputs",
            "",
        ),
    )
}


def schema_problem(event: Event) -> str | None:
    """Why event does not match SCHEMA, or None."""
    if event.kind not in SCHEMA:
        return f"unknown event kind {event.kind!r}"
    required, optional = SCHEMA[event.kind]
    keys = set(event.data)
    if missing := required - keys:
        return f"{event.kind}: missing {', '.join(sorted(missing))}"
    if extra := keys - required - optional:
        return f"{event.kind}: undocumented {', '.join(sorted(extra))}"
    return None


@dataclass(frozen=True)
class Event:
    """One step of a run. data must be JSON-serialisable."""

    kind: str
    agent_id: str
    parent_id: str
    role: str
    ts: float
    data: Mapping[str, Any] = field(default_factory=dict)
    node: str = ""  # the graph node whose Run emitted it; "" outside a graph


def as_dict(event: Event) -> dict[str, Any]:
    """event as a trace line; node only in a graph run, so other traces keep their
    format."""
    d = asdict(event)
    if not d["node"]:
        del d["node"]
    return d


class JsonlSink:
    """Appends each event to a file as one JSON line, flushed so a crash keeps it."""

    def __init__(self, path: str | Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)  # traces may hold workspace secrets
        self._file: IO[str] = os.fdopen(fd, "a", encoding="utf-8")

    def __call__(self, event: Event) -> None:
        self._file.write(json.dumps(as_dict(event), separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
