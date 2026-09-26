"""Sandboxes for the shell tool (design 3). A backend rewrites a command so that it
runs under OS-enforced limits; the shell tool runs the result.

Every backend denies network access unless the policy allows it, writes outside the
policy's write roots, and writes inside `.git`. Reads are allowed except under the
user's home, where only the policy's roots and the backend's extra_read paths are
readable.

- macOS (sandbox-exec): metadata (stat, readlink) stays allowed under home, so
  symlinked toolchains and cd work. It denies writes to any path named `.git` and to
  deny_write paths, even ones that do not exist yet.
- Linux (bwrap): home is an empty tmpfs with the readable paths bound into it. It
  protects `.git` at each write root and deny_write paths that exist when the command
  starts; bwrap mounts only existing paths, so a command can create a new one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

MAC_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


@dataclass(frozen=True)
class Policy:
    """Resolved paths. Write roots are also readable. deny_write wins over them."""

    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    deny_write: tuple[Path, ...] = ()
    network: bool = False


class Sandbox(Protocol):
    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        """argv rewritten to run under policy."""
        ...


@dataclass(frozen=True)
class MacSandbox:
    """sandbox-exec with a generated profile. extra_read lists paths under home that
    tools need, such as a Python install in ~/.local/share/uv."""

    home: Path | None = None
    extra_read: tuple[Path, ...] = ()

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        home = self.home if self.home is not None else _home()
        return [
            MAC_SANDBOX_EXEC,
            "-p",
            mac_profile(policy, home, self.extra_read),
            *argv,
        ]


@dataclass(frozen=True)
class BwrapSandbox:
    """bubblewrap (https://github.com/containers/bubblewrap) in new user, pid, ipc
    and uts namespaces, and a new network namespace unless the policy allows it.
    extra_read is as for MacSandbox."""

    home: Path | None = None
    extra_read: tuple[Path, ...] = ()
    bwrap: str = "bwrap"

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        home = self.home if self.home is not None else _home()
        return [self.bwrap, *bwrap_args(policy, home, self.extra_read), "--", *argv]


@dataclass(frozen=True)
class NoSandbox:
    """Runs commands unconfined. Only for an explicit --unsafe-no-sandbox."""

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        return list(argv)


def detect(extra_read: tuple[Path, ...] = ()) -> Sandbox | None:
    """The backend for this platform, or None if there is none (see why_none)."""
    if sys.platform == "darwin" and os.access(MAC_SANDBOX_EXEC, os.X_OK):
        return MacSandbox(extra_read=extra_read)
    if sys.platform.startswith("linux") and (path := shutil.which("bwrap")):
        if _bwrap_problem(path) is None:
            return BwrapSandbox(extra_read=extra_read, bwrap=path)
    return None


def why_none() -> str:
    """Why detect() returns None on this host."""
    if sys.platform.startswith("linux"):
        path = shutil.which("bwrap")
        if path is None:
            return "bwrap is not installed"
        if problem := _bwrap_problem(path):
            return f"bwrap cannot create a sandbox here: {problem}"
    return f"no sandbox backend for {sys.platform}"


@cache
def _bwrap_problem(path: str) -> str | None:
    """None if bwrap can create the namespaces BwrapSandbox uses, else its error.
    Ubuntu 24.04 and later block this until an AppArmor profile allows it."""
    probe = [path, "--ro-bind", "/", "/", "--unshare-user", "--unshare-net"]
    try:
        r = subprocess.run(
            [*probe, "--", "/bin/true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return None if r.returncode == 0 else (r.stderr.strip() or f"exit {r.returncode}")


def _home() -> Path | None:
    home = os.environ.get("HOME")
    return Path(os.path.realpath(home)) if home else None


def _q(p: Path) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac_profile(
    policy: Policy, home: Path | None, extra_read: tuple[Path, ...] = ()
) -> str:
    """An SBPL profile for policy. In SBPL the last matching rule wins, so each
    deny is followed by the allows that carve exceptions out of it."""
    dev = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper") '
    dev += '(regex #"^/dev/tty") (regex #"^/dev/fd/")'
    # Xcode's /usr/bin shims (make, python3, git) cache here and print an error if they
    # cannot; only files with that prefix are allowed in the per-user temp dir.
    dev += ' (regex #"^/private/var/folders/[^/]+/[^/]+/T/xcrun_db")'
    # /bin/sh is bash 3.2, which ignores TMPDIR and writes here-documents to /tmp/sh-thd*.
    dev += ' (regex #"^/private/tmp/sh-thd")'
    writes = " ".join(f"(subpath {_q(p)})" for p in policy.write_roots)
    lines = [
        "(version 1)",
        "(allow default)",
        *([] if policy.network else ["(deny network*)"]),
        "(deny file-write*)",
        f"(allow file-write* {writes} {dev})",
        '(deny file-write* (regex #"/\\.git(/|$)"))',
    ]
    if policy.deny_write:  # subpath matches regardless of case on case-insensitive APFS
        denied = " ".join(f"(subpath {_q(p)})" for p in policy.deny_write)
        lines.append(f"(deny file-write* {denied})")
    if home is not None:
        readable = (*policy.read_roots, *policy.write_roots, *extra_read)
        lines.append(f"(deny file-read* (subpath {_q(home)}))")
        lines.append(f"(allow file-read-metadata (subpath {_q(home)}))")
        if readable:
            lines.append(
                f"(allow file-read* {' '.join(f'(subpath {_q(p)})' for p in readable)})"
            )
    return "\n".join(lines) + "\n"


def bwrap_args(
    policy: Policy, home: Path | None, extra_read: tuple[Path, ...] = ()
) -> list[str]:
    """bwrap options for policy. Later mounts cover earlier ones, so the order is:
    everything read-only, home emptied, readable paths, writable paths, then the
    read-only exceptions inside them."""
    args = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    args += ["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    args += ["--unshare-cgroup-try", "--new-session", "--die-with-parent"]
    if not policy.network:
        args.append("--unshare-net")
    if home is not None and home != Path("/"):
        args += ["--tmpfs", str(home)]
    for p in (*policy.read_roots, *extra_read):
        if p.exists():
            args += ["--ro-bind", str(p), str(p)]
    for p in policy.write_roots:
        args += ["--bind", str(p), str(p)]
    frozen = [r / ".git" for r in policy.write_roots] + list(policy.deny_write)
    for p in frozen:
        if p.exists():
            args += ["--ro-bind", str(p), str(p)]
    return args
