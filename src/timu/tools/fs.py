"""Filesystem tools: read, list, search, write, edit (design 4.1).

Every path is resolved with symlinks followed, then checked against the Context's
roots, so a symlink cannot lead a tool outside them.
"""

from __future__ import annotations

import fnmatch
import itertools
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from timu.tool import Context, Tool, ToolOutput
from timu.types import Capability

MAX_FILE = (
    16 * 1024 * 1024
)  # bytes; larger files are refused by edit and skipped by search
SNIFF = 8192  # bytes checked for NUL to detect binary files
READ_LINES = 2000
MAX_ENTRIES = 1000
MAX_MATCHES = 500
MAX_LINE = 300  # characters of a matching line that search shows


class FsError(Exception):
    """A refused or failed operation. The message goes to the model."""


Handler = Callable[[Context, Mapping[str, Any]], ToolOutput]


def _guard(fn: Handler) -> Handler:
    """Turn FsError and OSError into error results."""

    def run(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
        try:
            return fn(ctx, args)
        except FsError as e:
            return ToolOutput(str(e), is_error=True)
        except OSError as e:
            where = f": {shown(ctx, Path(e.filename))}" if e.filename else ""
            return ToolOutput(f"{e.strerror or e}{where}", is_error=True)

    return run


# ---- arguments ----


def _str(args: Mapping[str, Any], key: str, default: str | None = None) -> str:
    v = args.get(key, default)
    if not isinstance(v, str):
        raise FsError(f"{key} must be a string")
    return v


def _int(args: Mapping[str, Any], key: str, default: int) -> int:
    v = args.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise FsError(f"{key} must be a positive integer")
    return v


# ---- paths ----


def shown(ctx: Context, p: Path) -> str:
    """p relative to the workdir when inside it, else absolute."""
    return str(p.relative_to(ctx.workdir)) if p.is_relative_to(ctx.workdir) else str(p)


def _lexical(ctx: Context, raw: str) -> Path:
    if not raw or "\0" in raw:
        raise FsError("path must be a non-empty string without NUL")
    return Path(os.path.abspath(ctx.workdir / raw))  # an absolute raw replaces workdir


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


def _within(p: Path, roots: tuple[Path, ...]) -> bool:
    return any(p.is_relative_to(r) for r in roots)


def readable(ctx: Context, raw: str) -> Path:
    real = _real(_lexical(ctx, raw))
    if not _within(real, ctx.read_roots):
        raise FsError(f"{raw}: outside the readable roots")
    return real


def writable(ctx: Context, raw: str) -> Path:
    lexical = _lexical(ctx, raw)
    if lexical in ctx.write_files and lexical.is_symlink():
        raise FsError(f"{raw}: is a symlink; refusing to write through it")
    real = _real(lexical)
    if ".git" in real.parts:  # hooks and config there can run commands later
        raise FsError(f"{raw}: writes inside .git are not allowed")
    if real in ctx.write_files or _within(real, ctx.write_roots):
        return real
    raise FsError(f"{raw}: outside the writable roots")


def _is_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\0" in f.read(SNIFF)


def atomic_write(path: Path, data: bytes) -> None:
    """Replace an existing file via a temp file and rename, so a failure leaves the
    old contents. A new file is created directly, with the umask's mode."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        try:
            with open(path, "xb") as f:
                f.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".timu-tmp", dir=path.parent
    )
    tmp = Path(name)
    try:
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _git_files(base: Path) -> list[Path] | None:
    """Tracked and untracked files under base, without ignored ones. None if base is
    not in a git repo. fsmonitor is off because it runs a configured command."""
    try:
        out = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
            ],
            cwd=base,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [base / os.fsdecode(n) for n in sorted(out.stdout.split(b"\0")) if n]


def _files(ctx: Context, base: Path) -> Iterator[Path]:
    """Regular files under base inside the read roots, skipping .git and gitignored
    files. Yields resolved paths."""
    if base.is_file():
        yield base
        return
    listed = _git_files(base)
    if listed is None:
        listed = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != ".git")
            listed.extend(Path(dirpath) / f for f in sorted(filenames))
    for p in listed:
        real = _real(p)
        if real.is_file() and _within(real, ctx.read_roots):
            yield real


def _matches(ctx: Context, p: Path, pattern: str) -> bool:
    """A pattern without "/" matches the file name at any depth; one with "/" matches
    the path relative to the workdir, where "*" also matches "/"."""
    pattern = pattern.removeprefix("**/")
    if "/" not in pattern:
        return fnmatch.fnmatch(p.name, pattern)
    return fnmatch.fnmatch(shown(ctx, p), pattern)


# ---- tools ----


@_guard
def _read(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    raw = _str(args, "path")
    offset, limit = _int(args, "offset", 1), _int(args, "limit", READ_LINES)
    path = readable(ctx, raw)
    st = path.stat()
    if stat.S_ISDIR(st.st_mode):
        raise FsError(f"{raw}: is a directory; use list")
    if not stat.S_ISREG(st.st_mode):
        raise FsError(f"{raw}: not a regular file")
    if _is_binary(path):
        raise FsError(f"{raw}: is binary")
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        lines = list(itertools.islice(f, offset - 1, offset - 1 + limit))
        total = offset - 1 + len(lines) + sum(1 for _ in f)
    text = "".join(lines)
    if not lines and total:
        return ToolOutput(f"[{raw} has {total} lines; offset {offset} is past the end]")
    if len(lines) < total:
        nl = "" if text.endswith("\n") else "\n"
        text += f"{nl}[lines {offset}-{offset + len(lines) - 1} of {total}]"
    return ToolOutput(text)


@_guard
def _list(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    raw = _str(args, "path", ".")
    base = readable(ctx, raw)
    if not base.is_dir():
        raise FsError(f"{raw}: not a directory")
    if "pattern" in args:
        pattern = _str(args, "pattern")
        entries = [
            shown(ctx, p) for p in _files(ctx, base) if _matches(ctx, p, pattern)
        ]
    else:
        entries = [
            shown(ctx, p) + ("/" if p.is_dir() else "")
            for p in sorted(base.iterdir())
            if p.name != ".git"
        ]
    if not entries:
        return ToolOutput("no entries")
    more = len(entries) - MAX_ENTRIES
    note = f"\n[{more} more not shown]" if more > 0 else ""
    return ToolOutput("\n".join(entries[:MAX_ENTRIES]) + note)


@_guard
def _search(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    raw_pattern = _str(args, "pattern")
    base = readable(ctx, _str(args, "path", "."))
    glob = _str(args, "glob") if "glob" in args else None
    flags = re.IGNORECASE if args.get("ignore_case") is True else 0
    try:
        rx = re.compile(raw_pattern, flags)
    except re.error as e:
        raise FsError(f"invalid regex: {e}") from None
    hits: list[str] = []
    for path in _files(ctx, base):
        if ctx.cancel.is_set() or len(hits) > MAX_MATCHES:
            break
        if glob and not _matches(ctx, path, glob):
            continue
        if path.stat().st_size > MAX_FILE or _is_binary(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if rx.search(line):
                    hits.append(f"{shown(ctx, path)}:{n}:{line.rstrip()[:MAX_LINE]}")
                    if len(hits) > MAX_MATCHES:
                        break
    if not hits:
        return ToolOutput("no matches")
    note = f"\n[stopped after {MAX_MATCHES} matches]" if len(hits) > MAX_MATCHES else ""
    return ToolOutput("\n".join(hits[:MAX_MATCHES]) + note)


@_guard
def _write(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    raw, content = _str(args, "path"), _str(args, "content")
    path = writable(ctx, raw)
    if path.is_dir():
        raise FsError(f"{raw}: is a directory")
    data = content.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, data)
    return ToolOutput(f"wrote {shown(ctx, path)} ({len(data)} bytes)")


@_guard
def _edit(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    raw = _str(args, "path")
    old, new = _str(args, "old_string"), _str(args, "new_string")
    if not old:
        raise FsError("old_string is empty")
    path = writable(ctx, raw)
    if not path.is_file():
        raise FsError(f"{raw}: no such file")
    if path.stat().st_size > MAX_FILE:
        raise FsError(f"{raw}: larger than {MAX_FILE // (1024 * 1024)} MB")
    data = path.read_bytes()
    if b"\0" in data:
        raise FsError(f"{raw}: is binary")
    old_b = old.encode()
    count = data.count(old_b)
    if count == 0:
        raise FsError(f"old_string not found in {raw}")
    if count > 1:
        raise FsError(f"old_string occurs {count} times in {raw}; it must occur once")
    atomic_write(path, data.replace(old_b, new.encode(), 1))
    return ToolOutput(f"edited {shown(ctx, path)}")


def _schema(required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_PATH = {
    "type": "string",
    "description": "relative to the working directory, or absolute",
}

READ = Tool(
    "read",
    "Read a text file. Returns up to `limit` lines starting at line `offset` (1-based).",
    _schema(
        ["path"],
        path=_PATH,
        offset={"type": "integer", "minimum": 1},
        limit={"type": "integer", "minimum": 1},
    ),
    frozenset({Capability.FS_READ}),
    _read,
)
LIST = Tool(
    "list",
    "List a directory. With `pattern`, list files below it that match a glob instead: "
    "`*.py` matches file names at any depth, `src/*.py` matches paths. "
    "Gitignored files and .git are skipped.",
    _schema([], path=_PATH, pattern={"type": "string"}),
    frozenset({Capability.FS_READ}),
    _list,
)
SEARCH = Tool(
    "search",
    "Search files for a Python regex. Returns path:line:text for each matching line. "
    "`glob` filters files as in list. Gitignored and binary files are skipped.",
    _schema(
        ["pattern"],
        pattern={"type": "string"},
        path=_PATH,
        glob={"type": "string"},
        ignore_case={"type": "boolean"},
    ),
    frozenset({Capability.FS_READ}),
    _search,
)
WRITE = Tool(
    "write",
    "Create or replace a file with `content`. Missing parent directories are created.",
    _schema(["path", "content"], path=_PATH, content={"type": "string"}),
    frozenset({Capability.FS_WRITE}),
    _write,
)
EDIT = Tool(
    "edit",
    "Replace `old_string` with `new_string` in a file. `old_string` must occur exactly once.",
    _schema(
        ["path", "old_string", "new_string"],
        path=_PATH,
        old_string={"type": "string"},
        new_string={"type": "string"},
    ),
    frozenset({Capability.FS_WRITE}),
    _edit,
)
