"""The shell tool: runs a command with /bin/sh inside the role's sandbox."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from timu.sandbox import Policy, Sandbox
from timu.tool import PROTECTED, Context, Tool, ToolOutput
from timu.types import Capability

SHELL_TIMEOUT_MAX = 600  # seconds
EXIT_GRACE = (
    0.3  # seconds to drain output after sh exits, if background jobs hold the pipe
)
TERM_GRACE = 1.0  # seconds between SIGTERM and SIGKILL


class _Output:
    """Keeps the first fifth and last four fifths of limit bytes, as tool.cap does,
    so memory stays bounded however much a command prints."""

    def __init__(self, limit: int) -> None:
        self.head_max = limit // 5
        self.tail_max = limit - self.head_max
        self.head, self.tail = bytearray(), bytearray()
        self.total = 0

    def add(self, data: bytes) -> None:
        self.total += len(data)
        room = self.head_max - len(self.head)
        if room > 0:
            self.head += data[:room]
            data = data[room:]
        self.tail += data
        del self.tail[: -self.tail_max or len(self.tail)]

    def text(self) -> str:
        dropped = self.total - len(self.head) - len(self.tail)
        head = self.head.decode(errors="replace")
        tail = self.tail.decode(errors="replace")
        return (
            f"{head}\n[... {dropped} bytes omitted ...]\n{tail}"
            if dropped
            else head + tail
        )


def scrubbed_env(tmp: Path, workspace_writable: bool = True) -> dict[str, str]:
    """A scrubbed environment: parent secrets such as API keys are not passed on.
    HOME is the private temp dir tmp, so tools find no user config."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp),
        "TMPDIR": f"{tmp}/",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }
    if not workspace_writable:
        # Read-only workspace (reviewer): keep Python caches out of it (design 4.3).
        env["PYTHONPYCACHEPREFIX"] = str(tmp / "pycache")
        env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    return env


@dataclass(frozen=True)
class Ran:
    """How a command ended. code is None if it was stopped or could not start."""

    text: str
    code: int | None
    stopped: str = ""  # why it was stopped: cancelled, or timed out after N s


def run_sh(
    cmd: str,
    *,
    cwd: Path,
    sandbox: Sandbox,
    policy: Policy,
    env: Mapping[str, str],
    timeout: float,
    cancel: threading.Event,
    max_output: int,
) -> Ran:
    """Run cmd with /bin/sh under sandbox and policy. stdout and stderr are merged
    and capped at max_output. Timeout and cancel kill the whole process group."""
    argv = sandbox.wrap(["/bin/sh", "-c", cmd], policy)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            process_group=0,
        )
    except OSError as e:
        return Ran(f"cannot run {argv[0]}: {e.strerror}", None)
    assert proc.stdout is not None
    out, fd = _Output(max_output), proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    exited_at: float | None = None
    stopped = ""
    try:
        while True:
            now = time.monotonic()
            if cancel.is_set():
                stopped = "cancelled"
                break
            if now >= deadline:
                stopped = f"timed out after {timeout:g}s"
                break
            if proc.poll() is not None:
                exited_at = exited_at or now
                if now - exited_at > EXIT_GRACE:
                    break
            if select.select([fd], [], [], min(0.1, deadline - now))[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break  # EOF: every writer has exited
                out.add(chunk)
    finally:
        proc.stdout.close()
        _kill_group(proc, gently=bool(stopped))
    if stopped:
        return Ran(out.text(), None, stopped)
    code = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
    return Ran(out.text(), code)


def _kill_group(proc: subprocess.Popen[bytes], gently: bool) -> None:
    """Kill sh's process group, including background jobs and grandchildren."""
    if gently:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(TERM_GRACE)
    with suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _timeout(ctx: Context, args: Mapping[str, Any]) -> float:
    want = args.get("timeout", ctx.shell_timeout)
    if not isinstance(want, int | float) or isinstance(want, bool) or not want >= 1:
        want = ctx.shell_timeout
    return float(min(want, SHELL_TIMEOUT_MAX))


def _shell(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return ToolOutput("command must be a non-empty string", is_error=True)
    if ctx.sandbox is None or ctx.tmp is None:
        return ToolOutput(
            "shell is not available: no sandbox for this role", is_error=True
        )
    timeout = _timeout(ctx, args)
    skill_dirs = tuple(s.path for s in ctx.skills)  # so skill scripts can run
    policy = Policy(
        (*ctx.read_roots, *skill_dirs),
        (*ctx.write_roots, ctx.tmp),  # never write_files
        tuple(ctx.workdir / p for p in PROTECTED),
    )
    writable = any(ctx.workdir.is_relative_to(r) for r in ctx.write_roots)
    ran = run_sh(
        cmd,
        cwd=ctx.workdir,
        sandbox=ctx.sandbox,
        policy=policy,
        env=scrubbed_env(ctx.tmp, writable),
        timeout=timeout,
        cancel=ctx.cancel,
        max_output=ctx.max_output,
    )
    text = ran.text
    if ran.code is None and not ran.stopped:
        return ToolOutput(text, is_error=True)  # could not start
    nl = "\n" if text and not text.endswith("\n") else ""
    if ran.stopped:
        return ToolOutput(f"{text}{nl}[{ran.stopped}; killed]", is_error=True)
    return ToolOutput(f"{text}{nl}[exit {ran.code}]", is_error=ran.code != 0)


SHELL = Tool(
    "shell",
    "Run a command with /bin/sh in the working directory, inside a sandbox with no "
    "network access. Returns combined stdout and stderr and the exit status. stdin is "
    f"/dev/null. The command and its children are killed after `timeout` seconds "
    f"(max {SHELL_TIMEOUT_MAX}). Use $TMPDIR for scratch files.",
    {
        "type": "object",
        "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["command"],
        "additionalProperties": False,
    },
    frozenset({Capability.EXEC}),
    _shell,
)
