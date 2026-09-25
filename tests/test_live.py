"""Phase 4 exit: a coder completes a small task against a real model.

Billed, so opt-in: TIMU_LIVE=1 make test. The model and API come from timu.toml
or TIMU_MODEL / TIMU_BASE_URL (see timu.config).
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from scenarios import SCENARIOS, redact
from timu import Agent, Capability, Event, Role, Task, Tool
from timu.cli import main
from timu.config import load_config
from timu.roles import researcher
from timu.sandbox import MacSandbox, detect
from timu.tools.fs import EDIT, LIST, READ, SEARCH, WRITE
from timu.tools.shell import SHELL
from timu.tools.skill import LOAD_SKILL, READ_SKILL_FILE
from timu.trace import load, save

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("TIMU_LIVE") != "1",
        reason="set TIMU_LIVE=1 to call a real model",
    ),
]

PROMPT = (
    "You are a coding agent working in the current directory. Use the tools to inspect "
    "and change files, and run commands to verify your work. Be concise."
)


def test_coder_completes_a_small_task(tmp_path: Path) -> None:
    provider = load_config().provider("coder")
    tools: tuple[Tool, ...] = (READ, LIST, SEARCH, WRITE, EDIT)
    grants = {Capability.FS_READ, Capability.FS_WRITE}
    sandbox = None
    if isinstance(detect(), MacSandbox):
        tools += (SHELL,)
        grants.add(Capability.EXEC)
        prefixes = {Path(os.path.realpath(p)) for p in (sys.prefix, sys.base_prefix)}
        sandbox = MacSandbox(extra_read=tuple(prefixes))
    role = Role("coder", PROMPT, tools, frozenset(grants), write_roots=(".",))
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    events: list[Event] = []

    result = Agent(role, provider, events.append, tmp_path, sandbox=sandbox).run(
        Task(
            "calc.py has a bug in add(). Fix it.",
            accept="add(2, 3) returns 5. No other files change.",
        )
    )

    assert result.status == "done", result.summary
    namespace: dict[str, object] = {}
    exec((tmp_path / "calc.py").read_text(), namespace)  # noqa: S102 - our own test fixture
    assert namespace["add"](2, 3) == 5  # type: ignore[operator]
    print(f"\n{result.usage}")


def test_agent_loads_a_matching_skill(tmp_path: Path) -> None:
    """Phase 5 exit (live half): the model loads a skill whose description matches the
    task. The file's content is only knowable from the skill body."""
    root = tmp_path / "skills" / "release-notes"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: release-notes\n"
        "description: Use when asked to write release notes for this project.\n---\n"
        "Write NOTES.md whose first line is exactly: Release notes (timu skill 7f3a)\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    role = Role(
        "coder",
        PROMPT,
        (LOAD_SKILL, READ_SKILL_FILE, READ, WRITE),
        frozenset({Capability.FS_READ, Capability.FS_WRITE}),
        write_roots=(".",),
        skills=("release-notes",),
    )
    events: list[Event] = []
    agent = Agent(
        role,
        load_config().provider("coder"),
        events.append,
        work,
        skill_roots=[tmp_path / "skills"],
    )
    result = agent.run(Task("Write release notes for version 0.2."))

    assert result.status == "done", result.summary
    assert any(e.kind == "tool_call" and e.data["name"] == "load_skill" for e in events)
    first = (work / "NOTES.md").read_text().splitlines()[0]
    assert first == "Release notes (timu skill 7f3a)"


def run_scenario(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[int, str, Path]:
    """Run a scenario through the CLI with the configured model. With TIMU_KEEP_TRACES
    set to a directory, the trace is saved there as <name>.jsonl, with local paths and
    the username replaced by placeholders, and <name>.json records the scenario and
    model, for the replay tests."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    scenario = SCENARIOS[name]
    repo = scenario.build(tmp_path / "repo")
    base = load_config()
    prefixes = sorted({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix)})
    toml = tmp_path / "timu.toml"
    toml.write_text(
        f"[provider]\nbase_url = {json.dumps(base.base_url)}\n"
        f"api_key_env = {json.dumps(base.api_key_env)}\nmodel = {json.dumps(base.model)}\n"
        f"[sandbox]\nextra_read = {json.dumps(prefixes)}\n"
    )
    err = io.StringIO()
    argv = [
        "run",
        "--config",
        str(toml),
        "-C",
        str(repo),
        "--workflow",
        scenario.workflow,
    ]
    code = main([*argv, *extra, scenario.objective], err=err)
    if keep := os.environ.get("TIMU_KEEP_TRACES"):
        (trace,) = (tmp_path / "state" / "timu" / "runs").glob("*.jsonl")
        dest = Path(keep)
        dest.mkdir(parents=True, exist_ok=True)
        save(redact(load(trace), tmp_path), dest / f"{name}.jsonl")
        meta = {"scenario": name, "model": base.model, "run": trace.stem}
        (dest / f"{name}.json").write_text(json.dumps(meta, indent=2) + "\n")
    return code, err.getvalue(), repo


def test_timu_run_fixes_a_seeded_bug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 6 exit (live): coder and reviewer, with real models, fix a seeded bug."""
    code, err, repo = run_scenario("fix-review-calc", tmp_path, monkeypatch)
    assert code == 0, err
    assert "a + b" in (repo / "calc.py").read_text()
    assert (
        (repo / "REVIEW.md")
        .read_text()
        .lstrip()
        .upper()
        .startswith(("VERDICT", "**VERDICT"))
    )


@pytest.mark.skipif(not os.environ.get("BRAVE_API_KEY"), reason="needs BRAVE_API_KEY")
def test_researcher_cites_sources(tmp_path: Path) -> None:
    """Phase 7 (live): the researcher searches, fetches and cites what it used."""
    config = load_config()
    role = researcher(config.search_tool())
    events: list[Event] = []
    result = Agent(role, config.provider("researcher"), events.append, tmp_path).run(
        Task("Which Python version added the tomllib module to the standard library?")
    )
    assert result.status == "done", result.summary
    assert result.untrusted
    assert "3.11" in result.summary
    assert "Sources:" in result.summary
    assert {e.data["name"] for e in events if e.kind == "tool_call"} <= {
        "web_search",
        "web_fetch",
    }


def test_lead_needs_research_and_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 8 exit (live): the lead meets an objective that needs a web fact and a
    code change, using only delegation."""
    # Unattended, so no terminal to approve on; the gate is tested offline.
    code, err, repo = run_scenario(
        "lead-tomllib", tmp_path, monkeypatch, "--no-approve-untrusted"
    )
    assert code == 0, err
    namespace: dict[str, object] = {}
    exec((repo / "facts.py").read_text(), namespace)  # noqa: S102 - our own test fixture
    assert namespace["tomllib_added"]() == "3.11"  # type: ignore[operator]
    assert "[lead a1] delegate researcher:" in err
