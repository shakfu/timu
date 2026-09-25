"""load_skill and read_skill_file: progressive loading of a role's skills (design 11).

Neither needs a capability. Skill directories are trusted and read-only, and each
tool reaches only the skills the role was built with.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from timu.skills import Skill, SkillError
from timu.tool import Context, Tool, ToolOutput

MAX_FILE = 4 * 1024 * 1024  # bytes


def _skill(ctx: Context, args: Mapping[str, Any]) -> Skill | ToolOutput:
    name = args.get("name")
    for s in ctx.skills:
        if s.name == name:
            return s
    known = ", ".join(s.name for s in ctx.skills) or "none"
    return ToolOutput(f"unknown skill: {name}; available: {known}", is_error=True)


def _load(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    skill = _skill(ctx, args)
    if isinstance(skill, ToolOutput):
        return skill
    try:
        body = skill.body()
    except SkillError as e:
        return ToolOutput(str(e), is_error=True)
    return ToolOutput(
        f"{body}\n\n[skill directory: {skill.path}; read its files with read_skill_file]"
    )


def _read_file(ctx: Context, args: Mapping[str, Any]) -> ToolOutput:
    skill = _skill(ctx, args)
    if isinstance(skill, ToolOutput):
        return skill
    rel = args.get("path")
    if not isinstance(rel, str) or not rel or "\0" in rel:
        return ToolOutput("path must be a non-empty string", is_error=True)
    path = Path(os.path.realpath(skill.path / rel))
    if not path.is_relative_to(skill.path):
        return ToolOutput(f"{rel}: outside the skill directory", is_error=True)
    if not path.is_file():
        return ToolOutput(f"{rel}: no such file in skill {skill.name}", is_error=True)
    if path.stat().st_size > MAX_FILE:
        return ToolOutput(
            f"{rel}: larger than {MAX_FILE // (1024 * 1024)} MB", is_error=True
        )
    data = path.read_bytes()
    if b"\0" in data:
        return ToolOutput(f"{rel}: is binary", is_error=True)
    return ToolOutput(data.decode("utf-8", "replace"))


_NAME = {"type": "string", "description": "a name from <available_skills>"}

LOAD_SKILL = Tool(
    "load_skill",
    "Load a skill's instructions. Call it when a task matches the skill's description.",
    {
        "type": "object",
        "properties": {"name": _NAME},
        "required": ["name"],
        "additionalProperties": False,
    },
    frozenset(),
    _load,
)
READ_SKILL_FILE = Tool(
    "read_skill_file",
    "Read a file from a skill's directory, such as references/REFERENCE.md.",
    {
        "type": "object",
        "properties": {
            "name": _NAME,
            "path": {
                "type": "string",
                "description": "relative to the skill directory",
            },
        },
        "required": ["name", "path"],
        "additionalProperties": False,
    },
    frozenset(),
    _read_file,
)
