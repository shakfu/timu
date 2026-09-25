"""Values passed between agents and workflows: capabilities, budgets, tasks, results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Capability(StrEnum):
    """A permission a tool needs and a role grants."""

    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    EXEC = "exec"
    NET = "net"


class Origin(StrEnum):
    """Where an artifact's content came from."""

    USER = "user"
    FS = "fs"
    NET = "net"
    AGENT = "agent"


@dataclass(frozen=True)
class Budget:
    """Limits on one agent run. Reaching any of them stops the run."""

    turns: int = 50
    tool_calls: int = 100
    tokens: int = 500_000
    cost_usd: float | None = None
    wall_seconds: float = 1800


@dataclass(frozen=True)
class Usage:
    """Resources an agent run has used."""

    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.turns + other.turns,
            self.tool_calls + other.tool_calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost_usd + other.cost_usd,
        )

    def exceeds(self, budget: Budget, elapsed: float) -> str | None:
        """The name of the first limit reached, or None."""
        if self.turns >= budget.turns:
            return "turns"
        if self.tool_calls >= budget.tool_calls:
            return "tool_calls"
        if self.tokens >= budget.tokens:
            return "tokens"
        if budget.cost_usd is not None and self.cost_usd >= budget.cost_usd:
            return "cost_usd"
        if elapsed >= budget.wall_seconds:
            return "wall_seconds"
        return None


@dataclass(frozen=True)
class Artifact:
    """A named output with provenance. source is an agent id, a path or a URL.

    origin NET means web content first-hand. untrusted also covers anything derived
    from it; such content reaches a model only inside the untrusted wrapper (design 6).
    """

    name: str
    content: str
    kind: str = "text"
    origin: Origin = Origin.USER
    source: str = ""
    untrusted: bool = False

    @property
    def tainted(self) -> bool:
        return self.untrusted or self.origin is Origin.NET


@dataclass(frozen=True)
class Task:
    """What an agent is asked to do."""

    goal: str
    inputs: tuple[Artifact, ...] = ()
    accept: str = ""
    budget: Budget | None = None


Status = Literal["done", "failed", "budget", "refused", "cancelled"]


@dataclass(frozen=True)
class Result:
    """What an agent returns. trace_id identifies the agent's events in the trace."""

    status: Status
    summary: str
    artifacts: tuple[Artifact, ...]
    usage: Usage
    trace_id: str
    untrusted: bool = False  # the agent read net content, or an untrusted input
