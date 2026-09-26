"""Tests for SKILL.md parsing, lookup, the skill tools and the prompt section."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from timu import Agent, Capability, Context, Role, RoleError, Task
from timu.provider.fake import FakeProvider, call, calls, text
from timu.role import make_context
from timu.skills import SkillError, default_roots, find, parse, prompt_section
from timu.tools.fs import READ, WRITE
from timu.tools.skill import LOAD_SKILL, READ_SKILL_FILE

GOOD = """---
name: pdf-tools
description: >
  Extract text from PDFs.
  Use when a task mentions PDFs.
license: 'Apache-2.0'
compatibility: "Requires python3"
metadata:
  author: example-org
  version: "1.0"  # a comment
allowed-tools: Read Bash(git:*)
---

# PDF tools

Run scripts/extract.py.
"""


def skill(root: Path, name: str, text: str = "", **files: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        text or f"---\nname: {name}\ndescription: Does {name}.\n---\nBody of {name}.\n"
    )
    for rel, content in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(content)
    return d


# ---- parsing ----


def test_parse_full(tmp_path: Path) -> None:
    s = parse(skill(tmp_path, "pdf-tools", GOOD))
    assert s.name == "pdf-tools"
    assert s.description == "Extract text from PDFs. Use when a task mentions PDFs.\n"
    assert s.license == "Apache-2.0"
    assert s.compatibility == "Requires python3"
    assert s.metadata == {"author": "example-org", "version": "1.0"}
    assert s.allowed_tools == ("Read", "Bash(git:*)")
    assert s.body() == "# PDF tools\n\nRun scripts/extract.py.\n"


@pytest.mark.parametrize(
    ("front", "expected"),
    [
        ("description: plain text  # comment", "plain text"),
        ("description: first line\n  continues here", "first line continues here"),
        ("description: |\n  line one\n  line two", "line one\nline two\n"),
        ("description: |-\n  kept\n\n  para", "kept\n\npara"),
        ("description: >-\n  folded\n  lines\n\n  new para", "folded lines\nnew para"),
        ('description: "esc \\"quoted\\" \\u00e9"', 'esc "quoted" \u00e9'),
        ("description: 'it''s'", "it's"),
    ],
)
def test_scalar_forms(tmp_path: Path, front: str, expected: str) -> None:
    s = parse(skill(tmp_path, "x", f"---\nname: x\n{front}\n---\nbody\n"))
    assert s.description == expected


@pytest.mark.parametrize(
    ("front", "message"),
    [
        ("description: d", ":1: SKILL.md must start with '---'"),  # no opening line
        ("---\nname: x\ndescription: d\n", "no closing '---'"),
        ("---\nname: x\n---\n", "description is required"),
        ("---\ndescription: d\n---\n", "name is required"),
        ("---\nname: x\nname: x\ndescription: d\n---\n", ":3: duplicate key name"),
        ("---\nname: x\ndescription: [a, b]\n---\n", ":3: unsupported YAML"),
        ("---\nname: x\ndescription: - a\n---\n", ":3: unsupported YAML"),
        (
            "---\nname: x\n  indented: y\ndescription: d\n---\n",
            "unexpected indentation",
        ),
        ("---\nname: x\nnot a key\n---\n", ":3: expected 'key: value'"),
        (
            "---\nname: x\ndescription: d\nmetadata:\n  a:\n    b: c\n---\n",
            "key: value' in mapping",
        ),
        (
            "---\nname: x\ndescription: d\nmetadata:\n  a: 1\n    b: c\n---\n",
            ":6: nested mappings",
        ),
        (
            "---\nname: x\ndescription: d\nmetadata: flat\n---\n",
            "metadata must be a mapping",
        ),
        (
            "---\nname: x\ndescription: d\ncompatibility: ''\n---\n",
            "compatibility is empty",
        ),
        ('---\nname: x\ndescription: "unterminated\n---\n', "invalid double-quoted"),
        ("---\nname: x\ndescription: |+\n  a\n---\n", "unsupported block indicator"),
        ("---\nname: x\ndescription: 'a' b\n---\n", "unexpected text after a quoted"),
    ],
)
def test_invalid_frontmatter(tmp_path: Path, front: str, message: str) -> None:
    d = tmp_path / "x"
    d.mkdir()
    (d / "SKILL.md").write_text(front)
    with pytest.raises(SkillError, match=message.replace("[", r"\[")):
        parse(d)


@pytest.mark.parametrize(
    "name", ["PDF", "-pdf", "pdf-", "pdf--tools", "pdf_tools", "a" * 65]
)
def test_invalid_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(SkillError, match="name"):
        parse(skill(tmp_path, "d", f"---\nname: {name}\ndescription: d\n---\n"))


def test_name_must_match_directory(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="does not match directory 'other'"):
        parse(skill(tmp_path, "other", "---\nname: pdf\ndescription: d\n---\n"))


def test_description_limit(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="longer than 1024"):
        parse(skill(tmp_path, "x", f"---\nname: x\ndescription: {'d' * 1025}\n---\n"))


# ---- lookup ----


def test_find_first_root_wins(tmp_path: Path) -> None:
    project, user = tmp_path / "project", tmp_path / "user"
    skill(project, "shared", "---\nname: shared\ndescription: project copy\n---\n")
    skill(user, "shared", "---\nname: shared\ndescription: user copy\n---\n")
    skill(user, "only-user")
    found = find(["shared", "only-user"], [project, user])
    assert [(s.name, s.description) for s in found] == [
        ("shared", "project copy"),
        ("only-user", "Does only-user."),
    ]
    assert found[0].shadows == (user / "shared",)
    assert found[1].shadows == ()


def test_agent_warns_when_a_skill_is_shadowed(tmp_path: Path) -> None:
    project, user = tmp_path / "project", tmp_path / "user"
    skill(project, "alpha")
    skill(user, "alpha")
    role = Role("r", "p", (LOAD_SKILL,), skills=("alpha",))
    events: list[Any] = []
    Agent(
        role,
        FakeProvider([text("ok")]),
        events.append,
        tmp_path,
        skill_roots=[project, user],
    ).run(Task("x"))
    warnings = [e.data["message"] for e in events if e.kind == "warning"]
    assert warnings == [f"skill alpha: {project / 'alpha'} shadows {user / 'alpha'}"]


def test_find_errors(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="skill missing not found"):
        find(["missing"], [tmp_path])
    with pytest.raises(SkillError, match="invalid skill name"):
        find(["../escape"], [tmp_path])


def test_default_roots() -> None:
    assert default_roots({"HOME": "/h"}) == (
        Path("/h/.config/timu/skills"),
        Path(".timu/skills"),
    )
    assert default_roots({"XDG_CONFIG_HOME": "/x"})[0] == Path("/x/timu/skills")
    assert default_roots({}) == (Path(".timu/skills"),)


def test_workspace_skill_cannot_replace_a_user_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo may add skills, but not change the instructions of one the user named."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ws, user = tmp_path / "ws", tmp_path / "xdg" / "timu" / "skills"
    skill(
        ws / ".timu" / "skills", "alpha", "---\nname: alpha\ndescription: repo\n---\n"
    )
    skill(user, "alpha", "---\nname: alpha\ndescription: user\n---\n")
    skill(ws / ".timu" / "skills", "beta")
    role = Role("r", "p", (LOAD_SKILL,), skills=("alpha", "beta"))
    ctx = make_context(role, ws, threading.Event())
    assert [(s.name, s.description) for s in ctx.skills] == [
        ("alpha", "user"),
        ("beta", "Does beta."),
    ]


# ---- tools ----


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    root = tmp_path / "skills"
    skill(root, "alpha", **{"references/REF.md": "reference text\n", "bin.dat": "a\0b"})
    (tmp_path / "secret.txt").write_text("secret\n")
    return Context(tmp_path, threading.Event(), skills=find(["alpha"], [root]))


def test_load_skill(ctx: Context) -> None:
    out = LOAD_SKILL.run(ctx, {"name": "alpha"})
    assert not out.is_error
    assert out.text.startswith("Body of alpha.\n")
    assert f"[skill directory: {ctx.skills[0].path};" in out.text


def test_load_unknown_skill(ctx: Context) -> None:
    out = LOAD_SKILL.run(ctx, {"name": "beta"})
    assert (out.is_error, out.text) == (True, "unknown skill: beta; available: alpha")


def test_read_skill_file(ctx: Context) -> None:
    assert (
        READ_SKILL_FILE.run(ctx, {"name": "alpha", "path": "references/REF.md"}).text
        == "reference text\n"
    )


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../../secret.txt", "outside the skill directory"),
        ("/etc/hosts", "outside the skill directory"),
        ("missing.md", "no such file"),
        ("references", "no such file"),
        ("bin.dat", "is binary"),
        ("", "non-empty string"),
    ],
)
def test_read_skill_file_refusals(ctx: Context, path: str, message: str) -> None:
    out = READ_SKILL_FILE.run(ctx, {"name": "alpha", "path": path})
    assert out.is_error
    assert message in out.text


def test_read_skill_file_symlink_escape(ctx: Context, tmp_path: Path) -> None:
    (ctx.skills[0].path / "leak.md").symlink_to(tmp_path / "secret.txt")
    assert (
        "outside" in READ_SKILL_FILE.run(ctx, {"name": "alpha", "path": "leak.md"}).text
    )


# ---- roles and the agent ----


def test_prompt_section_escapes(tmp_path: Path) -> None:
    s = parse(skill(tmp_path, "x", "---\nname: x\ndescription: use <b> & more\n---\n"))
    section = prompt_section([s])
    assert (
        "<skill><name>x</name><description>use &lt;b&gt; &amp; more</description></skill>"
        in section
    )
    assert prompt_section([]) == ""


def test_role_with_skills_needs_load_skill(tmp_path: Path) -> None:
    role = Role("r", "p", (READ,), frozenset({Capability.FS_READ}), skills=("alpha",))
    with pytest.raises(RoleError, match="not the load_skill tool"):
        Agent(role, FakeProvider([]), lambda e: None, tmp_path, skill_roots=[])


def test_missing_skill_fails_at_construction(tmp_path: Path) -> None:
    role = Role("r", "p", (LOAD_SKILL,), skills=("alpha",))
    with pytest.raises(RoleError, match="r: skill alpha not found"):
        Agent(role, FakeProvider([]), lambda e: None, tmp_path, skill_roots=[tmp_path])


def test_default_skill_root_is_relative_to_workdir(tmp_path: Path) -> None:
    skill(tmp_path / ".timu" / "skills", "alpha")
    role = Role("r", "p", (LOAD_SKILL,), skills=("alpha",))
    agent = Agent(role, FakeProvider([]), lambda e: None, tmp_path)
    assert [s.name for s in agent.context.skills] == ["alpha"]


def test_agent_lists_only_its_skills_and_loads_one(tmp_path: Path) -> None:
    """Phase 5 exit (scripted half): the prompt lists the role's skills, and the body
    reaches the model through load_skill."""
    root = tmp_path / "skills"
    skill(
        root,
        "greeting",
        "---\nname: greeting\ndescription: Use when asked to greet.\n---\n"
        "Write GREETING.txt containing exactly: hello from the skill\n",
    )
    skill(root, "unused")
    role = Role(
        "coder",
        "You are a coder.",
        (LOAD_SKILL, READ_SKILL_FILE, WRITE),
        frozenset({Capability.FS_WRITE}),
        write_roots=(".",),
        skills=("greeting",),
    )
    provider = FakeProvider(
        [
            calls(call("load_skill", name="greeting")),
            calls(call("write", path="GREETING.txt", content="hello from the skill")),
            text("done"),
        ]
    )
    agent = Agent(role, provider, lambda e: None, tmp_path, skill_roots=[root])
    assert agent.run(Task("greet the user")).status == "done"

    system = provider.requests[0].messages[0].content
    assert system.startswith("You are a coder.\n\n<available_skills>\n")
    assert "<name>greeting</name>" in system
    assert "unused" not in system
    loaded = provider.requests[1].messages[3]
    assert "hello from the skill" in loaded.content
    assert (tmp_path / "GREETING.txt").read_text() == "hello from the skill"
