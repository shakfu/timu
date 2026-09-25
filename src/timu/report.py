"""Report files: the path checks shared by both reviewer report modes (design 4.3)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from timu.tools.fs import atomic_write

MAX_REPORT = 256 * 1024  # bytes


class ReportError(ValueError):
    """A report path or report that fails a check."""


def file_root(workdir: Path, path: str) -> Path:
    """path as a single-file root: inside workdir, with an existing parent, not a
    symlink and not a directory. workdir must be resolved. Raises ReportError."""
    lexical = workdir / path
    try:
        parent = Path(os.path.realpath(lexical.parent, strict=True))
    except OSError:
        raise ReportError(f"parent of {path} does not exist") from None
    target = parent / lexical.name
    if not target.is_relative_to(workdir):
        raise ReportError(f"{path} is outside the workdir")
    if target.is_symlink():
        raise ReportError(f"{path} is a symlink")
    if target.is_dir():
        raise ReportError(f"{path} is a directory")
    return target


def write_report(workdir: Path, path: str, text: str) -> Path:
    """Write text to path atomically, after the file_root checks. Raises ReportError."""
    target = file_root(workdir, path)
    data = text.encode()
    if len(data) > MAX_REPORT:
        raise ReportError(f"report is {len(data)} bytes; the limit is {MAX_REPORT}")
    try:
        atomic_write(target, data)
    except OSError as e:
        raise ReportError(f"cannot write {path}: {e.strerror}") from None
    return target


def is_gitignored(workdir: Path, path: str) -> bool | None:
    """True or False in a git repo; None outside one or without git."""
    try:
        r = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "check-ignore", "-q", "--", path],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(r.returncode)
