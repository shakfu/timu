"""Events agents emit instead of printing, and sinks that receive them."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self

EventSink = Callable[["Event"], None]


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
        self._file: IO[str] = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed by close()

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
