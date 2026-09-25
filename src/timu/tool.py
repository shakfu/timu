"""Tools: a JSON-Schema interface for the model plus a handler that runs it."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from timu.sandbox import Sandbox
from timu.skills import Skill
from timu.types import Capability, Result


@dataclass(frozen=True)
class ToolOutput:
    """A tool's result. Failures are values, so the model sees them and can recover."""

    text: str
    is_error: bool = False
    untrusted: bool = False  # the text carries web content; the agent becomes untrusted


PROTECTED = ("timu.toml", ".timu")
"""Workdir entries that later runs read as config and skills; no agent may write them."""

Delegate = Callable[[str, str, str, tuple[str, ...]], Result]
"""delegate(role, goal, accept, input ids) -> the child's Result. Raises DelegateError."""


class DelegateError(Exception):
    """A refused delegation. The message goes to the model."""


@dataclass(frozen=True)
class Context:
    """What a tool handler may use. Handlers hold no other state.

    Paths are resolved (symlinks followed). write_files are single-file write roots.
    sandbox is set only for roles with exec; tmp is the run's private temp dir.
    """

    workdir: Path
    cancel: threading.Event
    max_output: int = 16 * 1024
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    write_files: tuple[Path, ...] = ()
    sandbox: Sandbox | None = None
    tmp: Path | None = None
    shell_timeout: float = 120
    skills: tuple[Skill, ...] = ()
    delegate: Delegate | None = None  # set by a workflow Run for roles that delegate


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Any]  # JSON Schema of the arguments object
    needs: frozenset[Capability]
    run: Callable[[Context, Mapping[str, Any]], ToolOutput]


def cap(text: str, limit: int) -> str:
    """text cut to about limit characters. Keeps the first fifth and the last four
    fifths, since errors and exit status usually come last."""
    if len(text) <= limit:
        return text
    head = limit // 5
    tail = limit - head
    dropped = len(text) - head - tail
    return f"{text[:head]}\n[... {dropped} characters omitted ...]\n{text[-tail:]}"
