"""The git commands the engine runs itself, unsandboxed, on copies and checkouts.
Each call turns off what an agent-written tree could make git run: fsmonitor, hooks,
and filter and diff drivers. Agents never run these; they cannot write .git."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

COMMITTER = (  # the user's identity, signing and hooks play no part in a copy
    *("-c", "user.name=timu", "-c", "user.email=timu@localhost"),
    *("-c", "commit.gpgsign=false"),
)
DRIVERS = r"^(filter|diff)\..*\.(clean|smudge|process|textconv|command)$"
TIMEOUT = 120  # seconds


class GitError(ValueError):
    """A git command failed, timed out or could not start."""


def run(cwd: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    """git args in cwd, and its output. Raises GitError unless it exits in ok."""
    try:
        r = subprocess.run(
            [
                *(
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                ),
                *no_drivers(cwd),
                *args,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",  # a diff may hold any bytes
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]} in {cwd}: {e}") from None
    if r.returncode not in ok:
        raise GitError(f"git {args[0]} in {cwd}: {r.stderr.strip() or r.returncode}")
    return r.stdout


def no_drivers(cwd: Path) -> list[str]:
    """-c options that blank each configured filter and diff driver. The engine runs
    unsandboxed, and .gitattributes in an agent-written tree selects the driver, which
    may run a script from that tree. Standard git-lfs filters stay."""
    r = subprocess.run(
        ["git", "config", "--get-regexp", DRIVERS],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    blank = []
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if not (key.startswith("filter.lfs.") and value.startswith("git-lfs ")):
            blank += ["-c", f"{key}="]
    return blank


def files(base: Path) -> list[Path] | None:
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


def is_ignored(workdir: Path, path: str) -> bool | None:
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


def provision(src: Path, dest: Path, ref: str, branch: str) -> tuple[str, bool]:
    """Clone src to dest with `git clone --local`, on a new branch at ref. Returns the
    base commit, and whether src has uncommitted changes (they are not copied).
    src may be an upstream node's copy. Raises GitError."""
    try:
        base = run(src, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except GitError:
        raise GitError(f"{src}: ref {ref!r} is not a commit") from None
    dirty = bool(run(src, "status", "--porcelain").strip())
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        dest.parent,
        "clone",
        "--local",
        "--no-checkout",
        "--quiet",
        "--",
        str(src),
        str(dest),
    )
    run(dest, "checkout", "--quiet", "-b", branch, base)
    return base, dirty


def commit(ws: Path, message: str, exclude: Sequence[str] = ()) -> str:
    """Commit every change in ws that is not ignored or excluded, as timu. Returns
    HEAD, unchanged if there was nothing to commit. Only the engine calls this."""
    # git add fails on a pathspec naming an ignored path, even an exclude; add --all
    # skips ignored paths anyway
    skip = [f":(exclude,literal){p}" for p in exclude if not is_ignored(ws, p)]
    run(ws, "add", "--all", "--", ".", *skip)
    if run(ws, "diff", "--cached", "--name-only"):
        run(ws, *COMMITTER, "commit", "--quiet", "--no-verify", "-m", message)
    return head(ws)


def head(ws: Path) -> str:
    return run(ws, "rev-parse", "HEAD").strip()


def diff(ws: Path, base: str) -> str:
    """The commits on ws's branch since base, as a patch."""
    return run(ws, "diff", "--binary", "--no-textconv", "--no-ext-diff", base, "HEAD")


def take_branch(workdir: Path, copy: Path, branch: str, base: str) -> None:
    """Make workdir's files match branch in copy, which started at base; HEAD and the
    index stay. Removed files go first, while workdir still holds base, so no path
    the copy turned into a symlink is followed. Raises GitError or OSError."""
    run(workdir, "fetch", "--quiet", "--no-tags", str(copy), branch)
    gone = run(
        workdir, "diff", "--name-only", "--no-renames", "--diff-filter=D",
        base, "FETCH_HEAD",
    )  # fmt: skip
    for rel in gone.splitlines():
        path = workdir / rel
        if not Path(os.path.realpath(path.parent)).is_relative_to(workdir):
            raise OSError(f"{rel} is outside the workdir")
        path.unlink(missing_ok=True)
    run(workdir, "checkout", "FETCH_HEAD", "--", ".")
    run(workdir, "reset", "--quiet")  # the index back to HEAD
