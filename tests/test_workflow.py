"""Tests for the built-in roles, report writing, Run budgets, the fix-review pipeline
and the workflow registry."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from timu import Budget, Capability, Event, Origin, Role, Task, Usage
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.report import MAX_REPORT, ReportError, is_gitignored, write_report
from timu.roles import CODER, REVIEWER, REVIEWER_WRITE, with_skills
from timu.sandbox import NoSandbox, Policy
from timu.tools.skill import LOAD_SKILL, READ_SKILL_FILE
from timu.workflow import (
    WORKFLOWS,
    Options,
    Run,
    check_fix_review,
    commands,
    fix_review,
    verdict,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
R, W, X = Capability.FS_READ, Capability.FS_WRITE, Capability.EXEC

# ---- roles (design 4.2, 4.4 rule 6) ----

MATRIX = {
    "coder": ({"read", "list", "search", "write", "edit", "shell"}, {R, W, X}),
    "reviewer": ({"read", "list", "search", "shell"}, {R, X}),
}


@pytest.mark.parametrize("role", [CODER, REVIEWER])
def test_roles_match_the_matrix(role: Role) -> None:
    tools, grants = MATRIX[role.name]
    assert {t.name for t in role.tools} == tools
    assert role.grants == grants


def test_reviewer_modes_differ_only_where_allowed() -> None:
    """Rule 6: tools (write), grants (fs.write), write roots and the final instruction."""
    allowed = {"prompt", "tools", "grants", "write_files"}
    for f in dataclasses.fields(Role):
        if f.name not in allowed:
            assert getattr(REVIEWER, f.name) == getattr(REVIEWER_WRITE, f.name), f.name
    assert {t.name for t in REVIEWER_WRITE.tools} - {
        t.name for t in REVIEWER.tools
    } == {"write"}
    assert REVIEWER_WRITE.grants - REVIEWER.grants == {W}
    body = REVIEWER.prompt.rsplit("\n\n", 1)[0]
    assert REVIEWER_WRITE.prompt.rsplit("\n\n", 1)[0] == body
    assert REVIEWER.prompt != REVIEWER_WRITE.prompt


def test_prompts_are_loaded() -> None:
    assert CODER.prompt.startswith("You are the coder")
    assert (
        "\nVERDICT: APPROVE\nVERDICT: CHANGES\n" in REVIEWER.prompt
    )  # the bare format
    assert "Do not search the rest of the filesystem." in REVIEWER.prompt
    assert REVIEWER.prompt.endswith("Your final message is the report.")


def test_with_skills() -> None:
    assert with_skills(CODER, ()) is CODER
    role = with_skills(with_skills(CODER, ("a",)), ("b",))
    assert role.skills == ("b",)
    assert [t.name for t in role.tools][-2:] == ["load_skill", "read_skill_file"]
    assert sum(t is LOAD_SKILL or t is READ_SKILL_FILE for t in role.tools) == 2


# ---- verdict ----


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("VERDICT: APPROVE\nall good", "APPROVE"),
        ("verdict: changes\n- bug", "CHANGES"),
        ("# Review\n\n**VERDICT: APPROVE**\n", "APPROVE"),
        ("Here is my review.\nVERDICT: CHANGES", "CHANGES"),
        ("a\nb\nc\nVERDICT: APPROVE", None),  # not in the first three lines
        ("VERDICT: APPROVED-ish", None),
        ("", None),
    ],
)
def test_verdict(report: str, expected: str | None) -> None:
    assert verdict(report) == expected


# ---- report writing ----


def test_write_report(tmp_path: Path) -> None:
    work = Path(os.path.realpath(tmp_path))
    assert write_report(work, "REVIEW.md", "x") == work / "REVIEW.md"
    assert (work / "REVIEW.md").read_text() == "x"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing/R.md", "parent of missing/R.md does not exist"),
        ("../R.md", "outside the workdir"),
        ("link.md", "is a symlink"),
        ("sub", "is a directory"),
    ],
)
def test_write_report_refusals(tmp_path: Path, path: str, message: str) -> None:
    work = Path(os.path.realpath(tmp_path)) / "w"
    (work / "sub").mkdir(parents=True)
    (work / "code.py").write_text("keep\n")
    (work / "link.md").symlink_to(work / "code.py")
    with pytest.raises(ReportError, match=message):
        write_report(work, path, "x")
    assert (work / "code.py").read_text() == "keep\n"


def test_write_report_size_limit(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="limit"):
        write_report(Path(os.path.realpath(tmp_path)), "R.md", "x" * (MAX_REPORT + 1))


@needs_git
def test_is_gitignored(tmp_path: Path) -> None:
    assert is_gitignored(tmp_path, "REVIEW.md") is None
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert is_gitignored(tmp_path, "REVIEW.md") is False
    (tmp_path / ".gitignore").write_text("REVIEW.md\n")
    assert is_gitignored(tmp_path, "REVIEW.md") is True


# ---- Run budgets ----


def make_run(
    tmp_path: Path, replies: list[Reply], **kw: Any
) -> tuple[Run, FakeProvider, list[Event]]:
    provider = FakeProvider(replies)
    events: list[Event] = []
    kw.setdefault("sandbox", NoSandbox())
    run = Run(tmp_path, lambda role: provider, events.append, run_id="r1", **kw)
    return run, provider, events


PLAIN = Role("plain", "p")


def test_child_budget_is_capped_by_the_remainder(tmp_path: Path) -> None:
    replies = [calls(call("x", n=i)) for i in range(10)]
    run, provider, _ = make_run(tmp_path, replies, budget=Budget(turns=3))
    assert run.run_agent(PLAIN, Task("t")).status == "budget"
    assert run.used.turns == 3
    assert run.run_agent(PLAIN, Task("t")).summary == "workflow budget exhausted"
    assert len(provider.requests) == 3


def test_usage_is_charged_to_the_run(tmp_path: Path) -> None:
    run, _, events = make_run(tmp_path, [text("a", cost=0.25), text("b", cost=0.5)])
    run.run_agent(PLAIN, Task("t"))
    run.run_agent(PLAIN, Task("t"))
    assert run.used == Usage(turns=2, input_tokens=20, output_tokens=10, cost_usd=0.75)
    assert [e.agent_id for e in events if e.kind == "start"] == ["a1", "a2"]


def test_cost_budget(tmp_path: Path) -> None:
    run, _, _ = make_run(
        tmp_path,
        [text("a", cost=0.6), text("b", cost=0.6)],
        budget=Budget(cost_usd=1.0),
    )
    run.run_agent(PLAIN, Task("t"))
    assert run._child_budget(Budget()).cost_usd == pytest.approx(0.4)  # type: ignore[union-attr]
    run.run_agent(PLAIN, Task("t"))
    assert run._child_budget(Budget()) is None


def test_wall_budget(tmp_path: Path) -> None:
    now = [0.0]
    run, _, _ = make_run(
        tmp_path, [], budget=Budget(wall_seconds=10), clock=lambda: now[0]
    )
    assert run._child_budget(Budget(wall_seconds=100)).wall_seconds == 10  # type: ignore[union-attr]
    now[0] = 11
    assert run._child_budget(Budget()) is None


def test_runs_in_one_session_share_budget_ids_and_trace(tmp_path: Path) -> None:
    """Run.at binds the session to another workdir: one budget, id sequence and sink.
    Delegate inputs and each Run's own usage stay per workdir."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    replies = [text("x", cost=0.25), text("y", cost=0.5)]
    run, _, events = make_run(tmp_path / "a", replies, budget=Budget(cost_usd=1.0))
    other = run.at(tmp_path / "b")
    run.run_agent(PLAIN, Task("t"))
    other.run_agent(PLAIN, Task("t"))
    assert other.workdir == Path(os.path.realpath(tmp_path / "b"))
    assert other.run_id == run.run_id == "r1"
    assert (run.used.cost_usd, other.used.cost_usd) == (0.25, 0.5)
    assert run.session.used == Usage(
        turns=2, input_tokens=20, output_tokens=10, cost_usd=0.75
    )
    assert other._child_budget(Budget()).cost_usd == pytest.approx(0.25)  # type: ignore[union-attr]
    assert [e.agent_id for e in events if e.kind == "start"] == ["a1", "a2"]
    assert (list(run.results), list(other.results)) == (["a1"], ["a2"])


SCRIPTS = {
    "lead": [
        calls(
            call("delegate", id="d1", role="researcher", goal="look"),
            call("delegate", id="d2", role="coder", goal="fix"),
            call("delegate", id="d3", role="reviewer", goal="review"),
        ),
        text("done"),
    ],
    "researcher": [text("facts")],
    "coder": [text("fixed")],
    "reviewer": [text("VERDICT: APPROVE")],
}


@pytest.mark.parametrize("name", list(WORKFLOWS))
def test_workflow_roles_cover_the_roles_it_starts(tmp_path: Path, name: str) -> None:
    """The CLI checks providers for Workflow.roles before the run; a role missing
    there would fail halfway instead."""
    started: list[str] = []

    def provider_for(role: Role) -> FakeProvider:
        started.append(role.name)
        return FakeProvider(list(SCRIPTS[role.name]))

    run = Run(tmp_path, provider_for, lambda e: None, sandbox=NoSandbox())
    by_name: dict[str, dict[str, Any]] = {
        "commands": {"steps": ["true"]},
        # fails once, so the fix loop runs, then passes
        "check-fix-review": {"check": [FAIL_ONCE], "max_rounds": 1},
        "lead": {},
    }
    params = by_name.get(name, {"max_rounds": 1})
    result = WORKFLOWS[name].run(run, "objective", Options({}, params=params))
    assert result.status == "done", result.summary
    assert set(started) == set(WORKFLOWS[name].roles)


FAIL_ONCE = "test -e .ran || { touch .ran; exit 1; }"


# ---- command steps ----


class Recorder:
    """A sandbox that runs commands unconfined and records each policy."""

    def __init__(self) -> None:
        self.policies: list[Policy] = []

    def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
        self.policies.append(policy)
        return list(argv)


def test_commands_run_in_order_and_stop_at_a_failure(tmp_path: Path) -> None:
    run, provider, events = make_run(tmp_path, [])
    steps = ["echo one", "false", "echo never"]
    r = commands(run, "build", steps=steps)
    assert (r.status, r.summary) == ("failed", "`false` failed: exit 1")
    (log,) = r.artifacts
    assert log.content == "$ echo one\none\n[exit 0]\n$ false\n\n[exit 1]\n"
    assert (log.origin, log.untrusted, r.untrusted) == (Origin.AGENT, False, False)
    assert provider.requests == []  # no model
    assert [e.data["command"] for e in events if e.kind == "command"] == steps[:2]
    assert commands(run, "b", steps=["true", "true"]).summary == "2 commands passed"


def test_command_policy(tmp_path: Path) -> None:
    box = Recorder()
    files = tmp_path / "files" / "lib"
    files.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    run = Run(tmp_path, lambda r: FakeProvider([]), lambda e: None, sandbox=box)
    run = run.session.at(ws, reads=(files,))
    r = commands(run, "b", steps=["true"], network=True)
    (policy,) = box.policies
    w = Path(os.path.realpath(ws))
    assert policy.network and policy.read_roots == (w, files)
    assert policy.write_roots[0] == w and len(policy.write_roots) == 2  # + a temp home
    assert policy.deny_write == (w / "timu.toml", w / ".timu")
    (log,) = r.artifacts
    assert (log.origin, log.untrusted, r.untrusted) == (Origin.NET, True, True)


def test_commands_get_a_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    run, _, _ = make_run(tmp_path, [])
    r = commands(run, "b", steps=["env"])
    env = r.artifacts[0].content
    assert "sk-secret" not in env
    home = next(line for line in env.splitlines() if line.startswith("HOME="))
    assert "/timu-" in home  # a private temp dir, not the user's home


def test_command_timeout_and_budget(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, [])
    r = commands(run, "b", steps=["sleep 5"], timeout=1)
    assert (r.status, r.summary) == ("failed", "`sleep 5` failed: timed out after 1s")
    now = [0.0]
    run, _, _ = make_run(
        tmp_path, [], budget=Budget(wall_seconds=10), clock=lambda: now[0]
    )
    now[0] = 11
    r = commands(run, "b", steps=["true"])
    assert (r.status, r.summary) == (
        "budget",
        "workflow budget exhausted before `true`",
    )


def test_commands_need_a_sandbox(tmp_path: Path) -> None:
    run = Run(tmp_path, lambda r: FakeProvider([]), lambda e: None)
    r = commands(run, "b", steps=["true"])
    assert (r.status, r.summary) == ("failed", "command steps need a sandbox")


def test_check_fix_review_skips_the_model_when_checks_pass(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [])
    r = check_fix_review(run, "o", check=["true"])
    assert (r.status, [a.name for a in r.artifacts]) == ("done", ["log"])
    assert provider.requests == []


def test_check_fix_review_fixes_then_checks_again(tmp_path: Path) -> None:
    replies = [
        calls(call("write", path="fixed.txt", content="ok")),
        text("fixed"),
        text("VERDICT: APPROVE"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = check_fix_review(run, "make the check pass", check=["test -f fixed.txt"])
    assert (r.status, r.summary) == ("done", "1 commands passed")
    assert [a.name for a in r.artifacts] == ["log", "review"]
    coder_task = provider.requests[0].messages[1].content
    assert "The checks failed" in coder_task and "[exit 1]" in coder_task


def test_check_fix_review_gives_up_after_max_rounds(tmp_path: Path) -> None:
    replies = [text("tried"), text("VERDICT: APPROVE")]
    run, _, _ = make_run(tmp_path, replies)
    r = check_fix_review(run, "o", check=["false"], max_rounds=1)
    assert r.status == "failed"
    assert r.summary == "checks still fail after 1 fix rounds: `false` failed: exit 1"


# ---- fix_review ----

APPROVE = "VERDICT: APPROVE\nThe fix is correct; tests pass."
CHANGES = "VERDICT: CHANGES\n- calc.py:2: still subtracts."


def coder_turn(summary: str = "fixed calc.py") -> list[Reply]:
    return [calls(call("write", path="calc.py", content="x = 1\n")), text(summary)]


def test_approved_in_round_one(tmp_path: Path) -> None:
    run, provider, events = make_run(tmp_path, [*coder_turn(), text(APPROVE)])
    result = fix_review(run, "fix the bug")

    assert (result.status, result.summary) == ("done", "approved in round 1")
    assert result.trace_id == "r1"
    assert (tmp_path / "REVIEW.md").read_text() == APPROVE
    assert result.artifacts[0].kind == "report"
    assert result.artifacts[0].content == APPROVE
    assert result.usage.turns == 3
    review_task = provider.requests[2].messages[1].content
    assert review_task.startswith(
        "Review the coder's work on this goal:\n\nfix the bug"
    )
    assert 'name="coder-summary"' in review_task
    assert "fixed calc.py" in review_task
    rounds = [
        (e.data["stage"], e.data.get("verdict")) for e in events if e.kind == "round"
    ]
    assert rounds == [("code", None), ("review", None), ("verdict", "APPROVE")]


def test_findings_reach_the_next_coder(tmp_path: Path) -> None:
    run, provider, _ = make_run(
        tmp_path, [*coder_turn(), text(CHANGES), *coder_turn(), text(APPROVE)]
    )
    result = fix_review(run, "fix the bug")
    assert result.summary == "approved in round 2"
    second_coder_task = provider.requests[3].messages[1].content
    assert "The reviewer asked for changes" in second_coder_task
    assert 'name="review" kind="report" origin="agent"' in second_coder_task
    assert "still subtracts" in second_coder_task


def test_stops_at_max_rounds(tmp_path: Path) -> None:
    replies = [*coder_turn(), text(CHANGES)] * 3
    run, provider, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", max_rounds=2)
    assert (result.status, result.summary) == ("failed", "not approved after 2 rounds")
    assert len(provider.requests) == 6
    assert result.artifacts[0].content == CHANGES


def test_missing_verdict_fails(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, [*coder_turn(), text("Looks fine to me.")])
    result = fix_review(run, "fix")
    assert (result.status, result.summary) == (
        "failed",
        "the review in round 1 has no VERDICT line",
    )


def test_coder_failure_stops_the_pipeline(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [Reply("", stop="refused")])
    result = fix_review(run, "fix")
    assert result.status == "refused"
    assert result.summary.startswith("coder stopped in round 1")
    assert len(provider.requests) == 1


def test_shared_budget_stops_the_pipeline(tmp_path: Path) -> None:
    run, _, _ = make_run(
        tmp_path, [*coder_turn(), *coder_turn()], budget=Budget(turns=2)
    )
    result = fix_review(run, "fix")
    assert result.status == "budget"
    assert result.summary.startswith(
        "reviewer stopped in round 1: workflow budget exhausted"
    )


def test_oversized_report_fails(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, [*coder_turn(), text(APPROVE + "x" * MAX_REPORT)])
    result = fix_review(run, "fix")
    assert result.status == "failed"
    assert "cannot write the report" in result.summary
    assert not (tmp_path / "REVIEW.md").exists()


def test_report_path_symlink_is_refused(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("keep\n")
    (tmp_path / "REVIEW.md").symlink_to(tmp_path / "code.py")
    run, provider, _ = make_run(tmp_path, [])
    with pytest.raises(ReportError, match="symlink"):
        fix_review(run, "fix")
    assert provider.requests == []


@needs_git
def test_gitignore_warning(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    run, _, events = make_run(tmp_path, [*coder_turn(), text(APPROVE)])
    fix_review(run, "fix")
    assert [e.data for e in events if e.kind == "warning" and e.role == "workflow"] == [
        {"message": "REVIEW.md is not gitignored"}
    ]
    (tmp_path / ".gitignore").write_text("REVIEW.md\n")
    run, _, events = make_run(tmp_path, [*coder_turn(), text(APPROVE)])
    fix_review(run, "fix")
    assert not [e for e in events if e.kind == "warning" and e.role == "workflow"]


# ---- mode A: the reviewer writes the report ----


def test_write_mode(tmp_path: Path) -> None:
    run, provider, _ = make_run(
        tmp_path,
        [
            *coder_turn(),
            calls(call("write", path="REVIEW.md", content="draft")),
            calls(call("write", path="REVIEW.md", content=APPROVE)),
            calls(call("write", path="calc.py", content="sabotage")),
            text("VERDICT: APPROVE"),
        ],
    )
    result = fix_review(run, "fix", report_mode="write")
    assert (result.status, result.artifacts[0].content) == ("done", APPROVE)
    assert (tmp_path / "REVIEW.md").read_text() == APPROVE
    assert (
        tmp_path / "calc.py"
    ).read_text() == "x = 1\n"  # the reviewer cannot touch code
    review_task = provider.requests[2].messages[1].content
    assert "Report path: REVIEW.md" in review_task
    assert "write" in provider.requests[2].tools


def test_write_mode_requires_a_new_report(tmp_path: Path) -> None:
    (tmp_path / "REVIEW.md").write_text(APPROVE)  # stale, from an earlier run
    run, _, _ = make_run(tmp_path, [*coder_turn(), text("VERDICT: APPROVE")])
    result = fix_review(run, "fix", report_mode="write")
    assert (result.status, result.summary) == (
        "failed",
        "reviewer did not write REVIEW.md in round 1",
    )


def test_skills_from_config(tmp_path: Path) -> None:
    skills = tmp_path / ".timu" / "skills" / "house-style"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: house-style\ndescription: Style rules.\n---\nUse tabs.\n"
    )
    run, provider, _ = make_run(tmp_path, [*coder_turn(), text(APPROVE)])
    fix_review(run, "fix", skills={"reviewer": ("house-style",)})
    assert "load_skill" not in provider.requests[0].tools  # the coder has no skills
    assert "load_skill" in provider.requests[2].tools
    assert "<name>house-style</name>" in provider.requests[2].messages[0].content
