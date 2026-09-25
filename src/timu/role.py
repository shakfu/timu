"""Roles: the prompt, tools, grants, roots and budget that specialise an agent."""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from timu.report import ReportError, file_root
from timu.sandbox import Sandbox, detect
from timu.skills import SkillError, default_roots, find
from timu.tool import Context, Tool
from timu.types import Budget, Capability


class RoleError(ValueError):
    """A role that breaks an invariant in design 4.4. Raised when an agent is built."""


@dataclass(frozen=True)
class Role:
    """Roots are paths relative to the workdir, or absolute. write_files are
    single-file write roots, such as a reviewer's report."""

    name: str
    prompt: str
    tools: tuple[Tool, ...] = ()
    grants: frozenset[Capability] = frozenset()
    budget: Budget = field(default_factory=Budget)
    read_roots: tuple[str, ...] = (".",)
    write_roots: tuple[str, ...] = ()
    write_files: tuple[str, ...] = ()
    max_output: int = 16 * 1024
    shell_timeout: float = 120  # seconds, when the model gives none
    skills: tuple[str, ...] = ()  # names; each role must also have load_skill
    delegates: tuple[str, ...] = ()  # roles this one may delegate to (design 7)


def validate(role: Role) -> None:
    """Check the invariants that do not depend on the workdir. Raises RoleError."""
    names = [t.name for t in role.tools]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        raise RoleError(f"{role.name}: duplicate tools: {', '.join(dupes)}")
    for tool in role.tools:
        if missing := tool.needs - role.grants:
            raise RoleError(
                f"{role.name}: tool {tool.name} needs {', '.join(sorted(missing))}, "
                "which the role does not grant"
            )
    risky = role.grants & {Capability.FS_WRITE, Capability.EXEC}
    if Capability.NET in role.grants and risky:
        raise RoleError(
            f"{role.name}: net cannot be granted with {', '.join(sorted(risky))} (design 6)"
        )
    if (
        role.write_roots or role.write_files
    ) and Capability.FS_WRITE not in role.grants:
        raise RoleError(f"{role.name}: write roots without the fs.write grant")
    if bool(role.delegates) != ("delegate" in names):
        raise RoleError(
            f"{role.name}: the delegate tool and a delegates list go together"
        )
    if role.skills and "load_skill" not in names:
        raise RoleError(f"{role.name}: has skills but not the load_skill tool")


def make_context(
    role: Role,
    workdir: Path,
    cancel: threading.Event,
    sandbox: Sandbox | None = None,
    skill_roots: Sequence[Path] | None = None,
) -> Context:
    """Validate role and resolve its roots and skills against workdir. A role with exec
    gets sandbox, or the platform's backend if sandbox is None. skill_roots defaults
    to skills.default_roots(), relative to workdir. Raises RoleError."""
    validate(role)
    roots = [
        workdir / r for r in (default_roots() if skill_roots is None else skill_roots)
    ]
    try:
        skills = find(role.skills, roots)
    except SkillError as e:
        raise RoleError(f"{role.name}: {e}") from None
    if Capability.EXEC not in role.grants:
        sandbox = None
    elif sandbox is None and (sandbox := detect()) is None:
        raise RoleError(
            f"{role.name}: exec needs a sandbox, and this platform has none; "
            "pass NoSandbox() to run commands unconfined"
        )
    work = Path(os.path.realpath(workdir))

    def resolve(p: str) -> Path:
        return Path(os.path.realpath(work / p))

    files = tuple(_file_root(role.name, work, p) for p in role.write_files)
    return Context(
        workdir=work,
        cancel=cancel,
        max_output=role.max_output,
        read_roots=tuple(map(resolve, role.read_roots))
        if Capability.FS_READ in role.grants
        else (),
        write_roots=tuple(map(resolve, role.write_roots)),
        write_files=files,
        sandbox=sandbox,
        shell_timeout=role.shell_timeout,
        skills=skills,
    )


def _file_root(role: str, work: Path, p: str) -> Path:
    try:
        return file_root(work, p)
    except ReportError as e:
        raise RoleError(f"{role}: write file {e}") from None
