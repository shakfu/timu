"""Tests for the built-in roles, report writing, Run budgets and the fix-review pipeline."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from timu import Budget, Capability, Event, Role, Task, Usage
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.report import MAX_REPORT, ReportError, is_gitignored, write_report
from timu.roles import CODER, REVIEWER, REVIEWER_WRITE, with_skills
from timu.sandbox import NoSandbox
from timu.tools.skill import LOAD_SKILL, READ_SKILL_FILE
from timu.workflow import Run, fix_review, verdict

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
