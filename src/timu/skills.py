"""Agent Skills (https://agentskills.io/specification): a directory holding a SKILL.md
with YAML frontmatter and a Markdown body.

Skills are found by name: the spec requires the name to equal the directory name, so
only the skills a role uses are parsed. The frontmatter parser handles the YAML
subset skills use (scalars, quoted strings, `|` and `>` blocks, multi-line plain
scalars, one level of mapping). Anything else is an error naming the file and line.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
KEY = re.compile(r"([A-Za-z0-9_-]+):(?:\s+(.*))?$")


class SkillError(ValueError):
    """An invalid or missing skill. The message names the file and, if known, the line."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the resolved skill directory
    license: str = ""
    compatibility: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()  # parsed, not enforced (plan D6)
    shadows: tuple[Path, ...] = ()  # same-name skill directories in later roots

    def body(self) -> str:
        """The Markdown after the frontmatter, read now so edits are picked up."""
        return _split(self.path / "SKILL.md")[1]


def default_roots(env: Mapping[str, str] = os.environ) -> tuple[Path, ...]:
    """./.timu/skills, then $XDG_CONFIG_HOME/timu/skills (default ~/.config/...)."""
    roots = [Path(".timu/skills")]
    base = env.get("XDG_CONFIG_HOME") or (
        str(Path(env["HOME"]) / ".config") if env.get("HOME") else ""
    )
    if base:
        roots.append(Path(base) / "timu" / "skills")
    return tuple(roots)


def find(names: Iterable[str], roots: Sequence[Path]) -> tuple[Skill, ...]:
    """The named skills, each from the first root that has it. Raises SkillError."""
    found = []
    for name in names:
        if not NAME.fullmatch(name):
            raise SkillError(f"invalid skill name: {name!r}")
        dirs = [r / name for r in roots if (r / name / "SKILL.md").is_file()]
        if not dirs:
            where = ", ".join(str(r) for r in roots) or "no skill roots"
            raise SkillError(f"skill {name} not found in {where}")
        found.append(replace(parse(dirs[0]), shadows=tuple(dirs[1:])))
    return tuple(found)


def parse(directory: Path) -> Skill:
    """Read and validate directory/SKILL.md. Raises SkillError."""
    path = Path(os.path.realpath(directory)) / "SKILL.md"
    front = frontmatter(path)
    src = str(path)

    def text(key: str, limit: int, required: bool = False) -> str:
        value = front.get(key, "")
        if not isinstance(value, str):
            raise SkillError(f"{src}: {key} must be a string")
        if required and not value:
            raise SkillError(f"{src}: {key} is required")
        if len(value) > limit:
            raise SkillError(f"{src}: {key} is longer than {limit} characters")
        return value

    name = text("name", 64, required=True)
    if not NAME.fullmatch(name):
        raise SkillError(
            f"{src}: name {name!r} must be lowercase letters, digits and single hyphens, "
            "not starting or ending with a hyphen"
        )
    if name != path.parent.name:
        raise SkillError(
            f"{src}: name {name!r} does not match directory {path.parent.name!r}"
        )
    if "compatibility" in front and not text("compatibility", 500):
        raise SkillError(f"{src}: compatibility is empty")
    metadata = front.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SkillError(f"{src}: metadata must be a mapping")
    return Skill(
        name=name,
        description=text("description", 1024, required=True),
        path=path.parent,
        license=text("license", 1024),
        compatibility=text("compatibility", 500),
        metadata=metadata,
        allowed_tools=tuple(text("allowed-tools", 4096).split()),
    )


def prompt_section(skills: Sequence[Skill]) -> str:
    """The system-prompt addition listing each skill's name and description."""
    if not skills:
        return ""
    items = "\n".join(
        f"<skill><name>{s.name}</name><description>{html.escape(s.description)}"
        "</description></skill>"
        for s in skills
    )
    return (
        "\n\n<available_skills>\n"
        f"{items}\n"
        "</available_skills>\n"
        "When a task matches a skill's description, call load_skill with its name "
        "before starting, and follow its instructions."
    )


# ---- frontmatter ----

Value = str | dict[str, str]


def _split(path: Path) -> tuple[list[str], str]:
    """(frontmatter lines, body) of a SKILL.md."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SkillError(f"{path}: {e}") from None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        raise SkillError(f"{path}:1: SKILL.md must start with '---'")
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip() == "---":
            return [x.rstrip("\r\n") for x in lines[1:i]], "".join(
                lines[i + 1 :]
            ).lstrip("\n")
    raise SkillError(f"{path}: frontmatter has no closing '---'")


def frontmatter(path: Path) -> dict[str, Value]:
    """The parsed frontmatter of path. Raises SkillError."""
    lines, _ = _split(path)
    out: dict[str, Value] = {}
    i = 0
    while i < len(lines):
        line, at = lines[i], i + 2  # +1 for the opening ---, +1 for 1-based
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            raise _error(path, at, "unexpected indentation")
        m = KEY.match(line)
        if not m:
            raise _error(path, at, "expected 'key: value'")
        key, raw = m.group(1), (m.group(2) or "").strip()
        if key in out:
            raise _error(path, at, f"duplicate key {key}")
        block, i = _indented(lines, i)
        if raw[:1] in ("|", ">"):
            if not re.fullmatch(r"[|>]-?", raw):
                raise _error(path, at, f"unsupported block indicator {raw!r}")
            out[key] = _block(block, raw)
        elif raw:
            if block and raw[0] in "'\"":
                raise _error(path, at, "multi-line quoted strings are not supported")
            continued = [
                b.strip() for b in block if b.strip()
            ]  # a multi-line plain scalar
            if any(KEY.match(c) for c in continued):
                raise _error(
                    path,
                    at + 1,
                    "unexpected indentation: nested keys are not supported",
                )
            out[key] = " ".join([_scalar(raw, path, at), *continued])
        elif block:
            out[key] = _mapping(block, path, at + 1)
        else:
            out[key] = ""
    return out


def _error(path: Path, line: int, msg: str) -> SkillError:
    return SkillError(f"{path}:{line}: {msg}")


def _indented(lines: list[str], i: int) -> tuple[list[str], int]:
    """The run of indented or blank lines from i, and the index after it."""
    j = i
    while j < len(lines) and (not lines[j].strip() or lines[j][0].isspace()):
        j += 1
    while j > i and not lines[j - 1].strip():  # trailing blanks belong to no block
        j -= 1
    return lines[i:j], j


def _scalar(raw: str, path: Path, at: int) -> str:
    """A plain, single-quoted or double-quoted scalar; a comment may follow."""
    if raw[0] == '"':
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except ValueError:
            value, end = None, 0
        if not isinstance(value, str):
            raise _error(path, at, "invalid double-quoted string")
        return _no_trailer(value, raw[end:], path, at)
    if raw[0] == "'":
        m = re.match(r"'((?:[^']|'')*)'", raw)
        if not m:
            raise _error(path, at, "unterminated single-quoted string")
        return _no_trailer(m.group(1).replace("''", "'"), raw[m.end() :], path, at)
    if raw[0] in "[{&*!|>%@`-":
        raise _error(path, at, f"unsupported YAML: a value starting with {raw[0]!r}")
    return raw.split(" #", 1)[0].rstrip()


def _no_trailer(value: str, rest: str, path: Path, at: int) -> str:
    if rest.strip() and not re.match(r"\s+#", rest):
        raise _error(
            path, at, f"unexpected text after a quoted string: {rest.strip()!r}"
        )
    return value


def _block(block: list[str], indicator: str) -> str:
    """A | (literal) or > (folded) block. "-" strips the final newline."""
    indent = min((len(b) - len(b.lstrip()) for b in block if b.strip()), default=0)
    rows = [b[indent:] if b.strip() else "" for b in block]
    if indicator[0] == "|":
        text = "\n".join(rows)
    else:
        paragraphs, current = [], []
        for r in rows:
            if r:
                current.append(r)
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    text = text.rstrip("\n")
    return text if indicator.endswith("-") else text + "\n"


def _mapping(block: list[str], path: Path, first: int) -> dict[str, str]:
    """One level of key: value lines, all at the same indentation."""
    out: dict[str, str] = {}
    indent = len(block[0]) - len(block[0].lstrip())
    for at, line in enumerate(block, first):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) != indent:
            raise _error(path, at, "nested mappings are not supported")
        m = KEY.match(line.strip())
        if not m or not m.group(2):
            raise _error(path, at, "expected 'key: value' in mapping")
        if m.group(1) in out:
            raise _error(path, at, f"duplicate key {m.group(1)}")
        out[m.group(1)] = _scalar(m.group(2).strip(), path, at)
    return out
