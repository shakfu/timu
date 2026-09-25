"""Sandboxes for the shell tool (design 3). A backend rewrites a command so that it
runs under OS-enforced limits; the shell tool runs the result.

Every backend denies network access, writes outside the policy's write roots, and
writes inside any `.git` directory. Reads are allowed except under the user's home,
where only the policy's roots and the backend's extra_read paths are readable.
Metadata (stat, readlink) stays allowed under home, so symlinked toolchains and cd
work; this exposes whether a guessed path exists, but not contents or listings.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAC_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


@dataclass(frozen=True)
class Policy:
    """Resolved paths. Write roots are also readable. deny_write wins over them."""

    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    deny_write: tuple[Path, ...] = ()


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
class NoSandbox:
    """Runs commands unconfined. Only for an explicit --unsafe-no-sandbox."""

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        return list(argv)


def detect() -> Sandbox | None:
    """The backend for this platform, or None if there is none."""
    if sys.platform == "darwin" and os.access(MAC_SANDBOX_EXEC, os.X_OK):
        return MacSandbox()
    return None


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
        "(deny network*)",
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
