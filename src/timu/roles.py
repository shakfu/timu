"""The built-in roles (design 4.2, 4.3). Prompts live in timu/prompts/."""

from __future__ import annotations

from dataclasses import replace
from importlib.resources import files

from timu.role import Role
from timu.tool import Tool
from timu.tools.delegate import DELEGATE
from timu.tools.fs import EDIT, LIST, READ, SEARCH, WRITE
from timu.tools.shell import SHELL
from timu.tools.skill import LOAD_SKILL, READ_SKILL_FILE
from timu.tools.web import WEB_FETCH
from timu.types import Capability

R, W, X, N = Capability.FS_READ, Capability.FS_WRITE, Capability.EXEC, Capability.NET


def prompt(name: str) -> str:
    return (
        (files("timu") / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()
    )


CODER = Role(
    "coder",
    prompt("coder"),
    (READ, LIST, SEARCH, WRITE, EDIT, SHELL),
    frozenset({R, W, X}),
    write_roots=(".",),
)

# Report mode B (default): the report is the final message; the workflow writes it.
REVIEWER = Role(
    "reviewer",
    prompt("reviewer") + "\n\n" + prompt("reviewer_return"),
    (READ, LIST, SEARCH, SHELL),
    frozenset({R, X}),
    max_output=32 * 1024,
    shell_timeout=300,
)

# Report mode A: the reviewer writes the report itself. The workflow sets write_files
# to the report path; only the write tool reaches it (design 4.3).
REVIEWER_WRITE = replace(
    REVIEWER,
    prompt=prompt("reviewer") + "\n\n" + prompt("reviewer_write"),
    tools=(*REVIEWER.tools, WRITE),
    grants=REVIEWER.grants | {W},
)


LEAD = Role(
    "lead",
    prompt("lead"),
    (READ, LIST, SEARCH, DELEGATE),
    frozenset({R}),
    delegates=("researcher", "coder", "reviewer"),
)


def researcher(search: Tool | None = None, fetch: Tool = WEB_FETCH) -> Role:
    """The researcher: web tools only, no files (design 4.2). search needs an API key,
    so it is built from config; without it the role can fetch but not search."""
    tools = (fetch,) if search is None else (search, fetch)
    return Role(
        "researcher", prompt("researcher"), tools, frozenset({N}), read_roots=()
    )


def with_skills(role: Role, names: tuple[str, ...]) -> Role:
    """role with the named skills and the tools that load them."""
    if not names:
        return role
    tools = tuple(t for t in role.tools if t not in (LOAD_SKILL, READ_SKILL_FILE))
    return replace(role, tools=(*tools, LOAD_SKILL, READ_SKILL_FILE), skills=names)
