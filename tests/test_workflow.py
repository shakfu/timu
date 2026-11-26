"""Tests for the built-in roles, report writing, Run budgets, the fix-review pipeline
and the workflow registry."""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from timu import Artifact, Budget, Capability, Event, Origin, Role, Task, Usage
from timu.git import is_ignored as is_gitignored
from timu.git import take_branch
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.report import MAX_REPORT, ReportError, write_report
from timu.roles import (
    CODER,
    FINDER,
    REVIEWER,
    REVIEWER_WRITE,
    TESTER,
    VALIDATOR,
    VERIFIER,
    with_skills,
)
from timu.sandbox import NoSandbox, Policy
from timu.tools.skill import LOAD_SKILL, READ_SKILL_FILE
from timu.workflow import (
    WORKFLOWS,
    Options,
    Run,
    _same_log,
    best_of_n,
    check_files,
    check_fix_review,
    commands,
    fix_review,
    lead,
    reproduce_fix_review,
    research_fix_review,
    review_validate_fix,
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
        ("**VERDICT:** APPROVE", "APPROVE"),
        ("**Verdict**: changes", "CHANGES"),
        ("VERDICT: **APPROVE**", "APPROVE"),
        ("`VERDICT`: `CHANGES`", "CHANGES"),
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
        (".git/config", "writes inside .git are not allowed"),
        ("sub/.git/config", "writes inside .git are not allowed"),
        ("timu.toml", "agents may not write it"),
        (".timu/skills/s/SKILL.md", "agents may not write it"),
    ],
)
def test_write_report_refusals(tmp_path: Path, path: str, message: str) -> None:
    work = Path(os.path.realpath(tmp_path)) / "w"
    (work / "sub" / ".git").mkdir(parents=True)
    (work / ".git").mkdir()
    (work / ".timu" / "skills" / "s").mkdir(parents=True)
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


F1 = {
    "id": "F1",
    "severity": "high",
    "location": "calc.py:2",
    "claim": "add subtracts",
    "evidence": "test_add fails",
}


def block(**data: Any) -> str:
    return "```json\n" + json.dumps(data) + "\n```"


def verdict_for(fid: str, status: str) -> dict[str, str]:
    return {"id": fid, "status": status, "evidence": f"{fid} is {status}"}


SCRIPTS = {
    "lead": [
        calls(
            call("delegate", id="d1", role="researcher", goal="look"),
            call("delegate", id="d2", role="coder", goal="fix"),  # then a review
        ),
        text("done"),
    ],
    "researcher": [text("facts")],
    "coder": [text("fixed")],
    "tester": [calls(call("write", path="tests/test_new.py", content="x")), text("ok")],
    "selector": [text(block(choice=[{"id": "c1", "reason": "smaller"}]))],
    # a verdict for fix-review and a finding for review-validate-fix
    "reviewer": [text("VERDICT: APPROVE\n" + block(findings=[F1]))],
    "validator": [text(block(verdicts=[verdict_for("F1", "confirmed")]))],
    "verifier": [text(block(verdicts=[verdict_for("F1", "fixed")]))],
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
        "check-fix-review": {"check": [FAIL_ONCE]},
        # passes on the base, fails with the tester's test, then passes
        "reproduce-fix-review": {"check": [FAIL_SECOND]},
        # two candidates pass, so the selector runs
        "best-of-n": {"check": ["true"], "n": 2},
    }
    if name == "best-of-n":
        git_repo(tmp_path)
    params = by_name.get(name, {})
    result = WORKFLOWS[name].run(run, "objective", Options({}, params=params))
    assert result.status == "done", result.summary
    assert set(started) == set(WORKFLOWS[name].roles)
    checks = [r for i, r in enumerate(started[1:]) if started[i] == "coder"]
    assert len(checks) == started.count("coder")  # no coder run is the last
    if name == "best-of-n":  # candidates not chosen are dropped; the chosen is reviewed
        assert started[-1] == "reviewer"
        return
    assert set(checks) <= {"reviewer", "verifier"}  # every fix is checked


FAIL_ONCE = "test -e .ran || { touch .ran; exit 1; }"
FAIL_SECOND = 'n=$(cat .n 2>/dev/null || echo 0); echo $((n + 1)) > .n; test "$n" != 1'


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


def test_commands_may_not_create_git_or_timu_files(tmp_path: Path) -> None:
    run, _, events = make_run(tmp_path, [])
    steps = ['git init -q "$TMPDIR/r"', "git init -q sub && mkdir .timu", "echo never"]
    r = commands(run, "b", steps=steps)
    assert r.status == "failed"
    assert "created .timu, sub/.git; moved to " in r.summary
    assert not (tmp_path / ".timu").exists()
    assert not (tmp_path / "sub" / ".git").exists() and (tmp_path / "sub").is_dir()
    assert "echo never" not in r.artifacts[0].content
    assert any("sub/.git" in e.data["message"] for e in events if e.kind == "warning")


def test_commands_need_a_sandbox(tmp_path: Path) -> None:
    run = Run(tmp_path, lambda r: FakeProvider([]), lambda e: None)
    r = commands(run, "b", steps=["true"])
    assert (r.status, r.summary) == ("failed", "command steps need a sandbox")


def test_check_fix_review_skips_the_model_when_checks_pass(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [])
    r = check_fix_review(run, "o", check=["true"])
    assert (r.status, [a.name for a in r.artifacts]) == ("done", ["log"])
    assert provider.requests == []


def test_a_passing_check_keeps_the_taint_of_its_inputs(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, [])
    web = Artifact("research", "x", "research", Origin.NET, "a1", True)
    assert check_fix_review(run, "o", check=["true"], inputs=(web,)).untrusted


def test_check_fix_review_fixes_then_checks_again(tmp_path: Path) -> None:
    replies = [
        calls(call("write", path="fixed.txt", content="ok")),
        text("fixed"),
        text("VERDICT: APPROVE"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = check_fix_review(run, "make the check pass", check=["test -f fixed.txt"])
    assert (r.status, r.summary) == ("done", "approved in round 1")
    assert [a.name for a in r.artifacts] == ["log", "review"]
    assert r.artifacts[0].content.endswith("[exit 0]\n")  # the passing re-check
    coder_task = provider.requests[0].messages[1].content
    assert "The checks failed" in coder_task and "[exit 1]" in coder_task


def test_check_files() -> None:
    ws = Path("/nonexistent")
    assert check_files(["make -C sub test"], ws) == (
        "Makefile",
        "GNUmakefile",
        "makefile",
        "*.mk",
    )
    assert "conftest.py" in check_files(["uv run python -m pytest -q"], ws)
    assert check_files(["ninja test", "echo pytest-free"], ws) == ()


def test_check_fix_review_refuses_a_fix_that_edits_the_check(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\ttest -f fixed.txt\n")
    replies = [
        calls(call("write", path="Makefile", content="check:\n\ttrue\n")),
        text("fixed"),
        text("VERDICT: APPROVE"),
    ]
    run, _, _ = make_run(tmp_path, replies)
    r = check_fix_review(run, "o", check=["make -s check"])
    assert (r.status, r.summary) == (
        "failed",
        (
            "round 1 changed Makefile, which the check runs; review the change, "
            "or set allow_check_edits = true"
        ),
    )
    run, _, _ = make_run(tmp_path, replies)
    (tmp_path / "Makefile").write_text("check:\n\ttest -f fixed.txt\n")
    r = check_fix_review(run, "o", check=["make -s check"], allow_check_edits=True)
    assert r.status == "done", r.summary


def test_check_fix_review_watches_a_script_the_check_names(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check.sh").write_text("test -f fixed.txt\n")
    (tmp_path / "conftest.py").write_text("")  # not a pytest check: not watched
    replies = [
        calls(call("write", path="conftest.py", content="# changed\n")),
        calls(call("write", id="w2", path="scripts/check.sh", content="true\n")),
        text("fixed"),
        text("VERDICT: APPROVE"),
    ]
    run, _, _ = make_run(tmp_path, replies)
    r = check_fix_review(run, "o", check=["sh scripts/check.sh"])
    assert r.summary.startswith("round 1 changed scripts/check.sh, which")


def test_check_fix_review_checks_the_coders_code_offline(tmp_path: Path) -> None:
    box = Recorder()
    replies = [
        calls(call("write", path="fixed.txt", content="ok")),
        text("fixed"),
        text("VERDICT: APPROVE"),
    ]
    run, _, _ = make_run(tmp_path, replies)
    run.session.sandbox = box
    r = check_fix_review(run, "o", check=["test -f fixed.txt"], network=True)
    assert r.status == "done"
    assert [p.network for p in box.policies] == [True, False]


def test_a_networked_first_check_taints_the_result(tmp_path: Path) -> None:
    """The coder read output from a run with network on. With no review, only the
    offline re-check's log reaches the result."""
    run, _, _ = make_run(tmp_path, [text("tried")])
    r = check_fix_review(run, "o", check=["false"], network=True)
    assert r.status == "failed"
    assert [(a.name, a.tainted) for a in r.artifacts] == [("log", False)]
    assert r.untrusted


def test_check_fix_review_does_not_loop_unless_asked(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [text("tried")])
    r = check_fix_review(run, "o", check=["false"])
    assert r.status == "failed"
    assert r.summary == "check in round 1: `false` failed: exit 1"
    assert len(provider.requests) == 1  # a failing re-check skips the review


def test_check_to_fix_loops_only_the_check_edge(tmp_path: Path) -> None:
    """Each edge has its own limit (E2): a failing re-check goes back to the coder,
    not to a review."""
    replies = [
        calls(call("write", path="n.txt", content="1")),
        text("tried"),
        calls(call("write", path="n.txt", content="2")),
        text("tried again"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    cmd = "cat n.txt 2>/dev/null; false"  # a new output each round
    r = check_fix_review(run, "o", check=[cmd], check_to_fix=1)
    assert r.summary == f"check in round 2: `{cmd}` failed: exit 1"
    assert len(provider.requests) == 4


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
    result = fix_review(run, "fix the bug", review_to_fix=1)
    assert result.summary == "approved in round 2"
    second_coder_task = provider.requests[3].messages[1].content
    assert "The reviewer asked for changes" in second_coder_task
    assert 'name="review" kind="report" origin="agent"' in second_coder_task
    assert "still subtracts" in second_coder_task


def test_review_to_fix_bounds_the_loop(tmp_path: Path) -> None:
    replies = [*coder_turn(), text(CHANGES)] * 3
    run, provider, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", review_to_fix=1)
    assert (result.status, result.summary) == ("failed", "not approved after 2 rounds")
    assert len(provider.requests) == 6
    assert result.artifacts[0].content == CHANGES


# ---- fix_review with a check (H2 in REVIEW.md) ----


def test_a_passing_check_reaches_the_reviewer(tmp_path: Path) -> None:
    run, provider, events = make_run(tmp_path, [*coder_turn(), text(APPROVE)])
    result = fix_review(run, "fix", check=["test -f calc.py"])
    assert (result.status, result.summary) == ("done", "approved in round 1")
    review_task = provider.requests[2].messages[1].content
    assert 'name="log" kind="log"' in review_task
    assert "$ test -f calc.py" in review_task
    rounds = [e.data["stage"] for e in events if e.kind == "round"]
    assert rounds == ["code", "check", "review", "verdict"]


def test_a_failing_check_skips_the_review(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, coder_turn())
    result = fix_review(run, "fix", check=["false"])
    assert (result.status, result.summary) == (
        "failed",
        "check in round 1: `false` failed: exit 1",
    )
    assert len(provider.requests) == 2  # the coder's two turns, no reviewer


def test_check_to_fix_sends_the_log_back(tmp_path: Path) -> None:
    """The check edge has its own limit; review_to_fix stays 0."""
    replies = [text("tried"), *coder_turn(), text(APPROVE)]
    run, provider, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", check=["test -f calc.py"], check_to_fix=1)
    assert (result.status, result.summary) == ("done", "approved in round 2")
    second_coder_task = provider.requests[1].messages[1].content
    assert "The checks failed." in second_coder_task
    assert "[exit 1]" in second_coder_task


def test_a_check_failure_after_changes_keeps_the_review(tmp_path: Path) -> None:
    """A failing check does not replace a review the coder has not yet answered."""
    replies = [
        calls(call("write", path="calc.py", content="good\n")),
        text("fixed"),
        text(CHANGES),
        calls(call("write", path="calc.py", content="bad\n")),
        text("changed"),
        calls(call("write", path="calc.py", content="good\n")),
        text("fixed again"),
        text(APPROVE),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    result = fix_review(
        run, "fix", check=["grep -q good calc.py"], check_to_fix=1, review_to_fix=1
    )
    assert (result.status, result.summary) == ("done", "approved in round 3")
    third_coder_task = provider.requests[5].messages[1].content
    assert "The checks failed." in third_coder_task
    assert "[exit 1]" in third_coder_task
    assert "The reviewer asked for changes" in third_coder_task
    assert "still subtracts" in third_coder_task
    review_task = provider.requests[7].messages[1].content
    assert "[exit 1]" not in review_task


@needs_git
def test_a_round_that_changes_nothing_stops_the_run(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    replies = [*coder_turn(), text(CHANGES), text("nothing to do")]
    run, provider, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", review_to_fix=2)
    assert (result.status, result.summary) == ("failed", "round 2 made no change")
    assert len(provider.requests) == 4  # no second review


def test_the_same_check_failure_twice_stops_the_run(tmp_path: Path) -> None:
    replies = [
        calls(call("write", path="a.txt", content="a")),
        text("tried"),
        calls(call("write", path="b.txt", content="b")),
        text("tried again"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", check=["echo took 0.5s; false"], check_to_fix=3)
    assert result.summary == "check in round 2 failed with the same output as before"
    assert len(provider.requests) == 4


def test_same_log_ignores_timings_and_the_workdir() -> None:
    def log(text: str) -> Artifact:
        return Artifact("log", text, "log")

    w = Path("/w/copy")
    a = log("/w/copy/t.py::x FAILED\n1 failed in 0.52s\n")
    assert _same_log(a, log("/w/copy/t.py::x FAILED\n1 failed in 1.07s\n"), w)
    assert not _same_log(a, log("/w/copy/t.py::y FAILED\n1 failed in 0.52s\n"), w)


def test_a_round_the_budget_cannot_cover_is_not_started(tmp_path: Path) -> None:
    replies = [*coder_turn(), text(CHANGES)]  # round 1: 3 turns
    run, provider, _ = make_run(tmp_path, replies, budget=Budget(turns=5))
    result = fix_review(run, "fix", review_to_fix=1)
    assert (result.status, result.summary) == (
        "budget",
        "round 2 not started: less budget is left than a round used",
    )
    assert len(provider.requests) == 3


def test_check_to_fix_bounds_the_check_edge(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [text("tried")] * 3)
    result = fix_review(run, "fix", check=["false"], check_to_fix=1)
    assert result.summary == "check in round 2: `false` failed: exit 1"
    assert len(provider.requests) == 2


def test_a_fix_may_not_edit_what_the_check_runs(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\ttest -f fixed.txt\n")
    replies = [
        calls(call("write", path="Makefile", content="check:\n\ttrue\n")),
        text("fixed"),
        text(APPROVE),
    ]
    run, _, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", check=["make -s check"])
    assert result.status == "failed"
    assert result.summary.startswith("round 1 changed Makefile, which the check runs")
    (tmp_path / "Makefile").write_text("check:\n\ttest -f fixed.txt\n")
    run, _, _ = make_run(tmp_path, replies)
    result = fix_review(run, "fix", check=["make -s check"], allow_check_edits=True)
    assert result.status == "done", result.summary


def test_coder_summary_can_be_withheld_from_the_reviewer(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [*coder_turn(), text(APPROVE)])
    fix_review(run, "fix", check=["true"], coder_summary=False)
    review_task = provider.requests[2].messages[1].content
    assert "coder-summary" not in review_task
    assert "fixed calc.py" not in review_task
    assert 'name="log"' in review_task


# the registry fills these from Options, and workflows from their own steps
SUPPLIED = {"skills", "inputs", "search", "fetch"}
SUPPLIED |= {"failed_check", "review_inputs", "first"}
# fix_review params that a forwarding workflow keeps from graph files, and why
NOT_FORWARDED = {
    # an ablation setting for the sim's fix-review arms (H2)
    "check-fix-review": {"coder_summary"},
}
FUNCTIONS = {
    "fix-review": fix_review,
    "research-fix-review": research_fix_review,
    "reproduce-fix-review": reproduce_fix_review,
    "best-of-n": best_of_n,
    "review-validate-fix": review_validate_fix,
    "commands": commands,
    "check-fix-review": check_fix_review,
    "lead": lead,
}


def keywords(f: Any) -> tuple[set[str], bool]:
    """f's keyword-only params, and whether it forwards **kwargs to fix_review."""
    ps = inspect.signature(f).parameters.values()
    own = {p.name for p in ps if p.kind is p.KEYWORD_ONLY} - SUPPLIED
    return own, any(p.kind is p.VAR_KEYWORD for p in ps)


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_params_match_the_function(name: str) -> None:
    """A param in the function but not in Workflow.params is unreachable from the CLI
    and graph files, unless NOT_FORWARDED names it; one in params but not in the
    function fails at run time."""
    declared = set(WORKFLOWS[name].params)
    own, forwards = keywords(FUNCTIONS[name])
    if forwards:
        own |= keywords(fix_review)[0] - NOT_FORWARDED.get(name, set())
    assert declared == own


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


# ---- review_validate_fix ----


def finding(fid: str, severity: str) -> dict[str, str]:
    return {**F1, "id": fid, "severity": severity, "claim": f"claim {fid}"}


def verdicts(**status: str) -> Reply:
    return text(
        "Done.\n" + block(verdicts=[verdict_for(k, v) for k, v in status.items()])
    )


THREE = text(
    "Report.\n"
    + block(
        findings=[
            finding("F1", "low"),
            finding("F2", "critical"),
            finding("F3", "medium"),
        ]
    )
)


def test_review_validate_fix_roles_judge_but_do_not_write() -> None:
    assert FINDER.name == "reviewer"  # [roles.reviewer] model applies
    for role in (FINDER, VALIDATOR, VERIFIER):
        assert (role.tools, role.grants) == (REVIEWER.tools, REVIEWER.grants)
    assert (VALIDATOR.name, VERIFIER.name) == ("validator", "verifier")


def test_review_validate_fix_fixes_confirmed_findings_most_severe_first(
    tmp_path: Path,
) -> None:
    replies = [
        THREE,
        verdicts(F1="confirmed", F2="confirmed", F3="rejected"),
        *coder_turn(),
        verdicts(F1="fixed", F2="fixed"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(run, "review calc.py")
    assert (r.status, r.summary) == ("done", "fixed 2 confirmed findings in round 1")
    coder_task = provider.requests[2].messages[1].content
    assert coder_task.index("F2 [critical]") < coder_task.index("F1 [low]")
    assert "F3" not in coder_task
    report = (tmp_path / "REVIEW.md").read_text()
    assert r.artifacts[0].content == report
    assert "F3 [medium] calc.py:2\nClaim: claim F3" in report
    assert "Validation: rejected. F3 is rejected" in report
    assert "Verification: fixed. F2 is fixed" in report


@pytest.mark.parametrize(
    ("replies", "summary"),
    [
        ([text(block(findings=[]))], "no findings"),
        (
            [THREE, verdicts(F1="rejected", F2="rejected", F3="rejected")],
            "no confirmed findings (3 rejected)",
        ),
    ],
)
def test_review_validate_fix_stops_early(
    tmp_path: Path, replies: list[Reply], summary: str
) -> None:
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(run, "review")
    assert (r.status, r.summary) == ("done", summary)
    assert len(provider.requests) == len(replies)


def test_review_validate_fix_does_not_loop_unless_asked(tmp_path: Path) -> None:
    replies = [
        THREE,
        verdicts(F1="confirmed", F2="rejected", F3="rejected"),
        *coder_turn(),
        verdicts(F1="unfixed"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(run, "review")
    assert (r.status, r.summary) == (
        "failed",
        "1 of 1 confirmed findings unfixed after 1 rounds",
    )
    assert len(provider.requests) == 5


def test_review_validate_fix_checks_before_the_verifier(tmp_path: Path) -> None:
    """A failing check goes back to the coder; the verifier gets the passing log."""
    one = [THREE, verdicts(F1="confirmed", F2="rejected", F3="rejected")]
    replies = [
        *one,
        calls(call("write", path="calc.py", content="bad\n")),
        text("tried"),
        calls(call("write", path="calc.py", content="good\n")),
        text("fixed"),
        verdicts(F1="fixed"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(
        run, "review", check=["grep -q good calc.py"], check_to_fix=1
    )
    assert (r.status, r.summary) == ("done", "fixed 1 confirmed findings in round 2")
    second_fix = provider.requests[4].messages[1].content
    assert "The checks failed" in second_fix and "[exit 1]" in second_fix
    verify_task = provider.requests[6].messages[1].content
    assert 'name="log"' in verify_task and "[exit 0]" in verify_task


def test_review_validate_fix_fails_on_a_check_without_a_retry(tmp_path: Path) -> None:
    replies = [
        THREE,
        verdicts(F1="confirmed", F2="rejected", F3="rejected"),
        text("tried"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(run, "review", check=["false"])
    assert (r.status, r.summary) == (
        "failed",
        "check in round 1: `false` failed: exit 1",
    )
    assert len(provider.requests) == 3  # no verifier


def test_verify_to_fix_sends_back_only_unfixed_findings(tmp_path: Path) -> None:
    replies = [
        THREE,
        verdicts(F1="confirmed", F2="confirmed", F3="rejected"),
        *coder_turn(),
        verdicts(F1="fixed", F2="unfixed"),
        *coder_turn(),
        verdicts(F2="fixed"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(run, "review", verify_to_fix=1)
    assert (r.status, r.summary) == ("done", "fixed 2 confirmed findings in round 2")
    second_coder_task = provider.requests[5].messages[1].content
    assert "F1" not in second_coder_task
    assert "Verification: unfixed. F2 is unfixed" in second_coder_task


@pytest.mark.parametrize(
    ("replies", "summary"),
    [
        ([text("no block")], "reviewer stopped: unusable final answer: no json block"),
        (
            [text("```json\n{bad\n```")],
            "reviewer stopped: unusable final answer: invalid json",
        ),
        (
            [text(block(findings=[finding("F1", "severe")]))],
            "reviewer stopped: unusable final answer: F1: severity must be one of critical, high, medium, low",
        ),
        (
            [text(block(findings=[finding("F1", "low"), finding("F1", "high")]))],
            "reviewer stopped: unusable final answer: finding ids are not unique",
        ),
        (
            [text(block(findings=[{"id": "F1"}]))],
            "reviewer stopped: unusable final answer: each of findings needs string fields",
        ),
        (
            [THREE, verdicts(F1="confirmed", F2="confirmed")],
            "validator stopped: unusable final answer: need one verdict for each of F1, F2, F3",
        ),
        (
            [THREE, verdicts(F1="confirmed", F2="maybe", F3="rejected")],
            "validator stopped: unusable final answer: F2: status must be confirmed or rejected",
        ),
        (
            [
                THREE,
                verdicts(F1="confirmed", F2="rejected", F3="rejected"),
                *coder_turn(),
                verdicts(F1="fixed", F2="fixed"),
            ],
            "verifier stopped in round 1: unusable final answer: need one verdict for each of F1",
        ),
    ],
)
def test_review_validate_fix_protocol_errors_fail_the_run(
    tmp_path: Path, replies: list[Reply], summary: str
) -> None:
    """The agent is told what is wrong and tries once more; it repeats the error."""
    run, provider, events = make_run(tmp_path, [*replies, replies[-1]])
    r = review_validate_fix(run, "review")
    assert r.status == "failed"
    assert r.summary.startswith(summary), r.summary
    retry = provider.requests[-1].messages[-1].content
    assert retry.startswith("Your final answer cannot be used: ")
    assert any(e.kind == "warning" for e in events)


def test_review_validate_fix_recovers_from_a_bad_block(tmp_path: Path) -> None:
    replies = [
        text("Two bugs, I think."),  # no block: asked again
        THREE,
        verdicts(F1="rejected", F2="rejected", F3="rejected"),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = review_validate_fix(run, "review")
    assert (r.status, r.summary) == ("done", "no confirmed findings (3 rejected)")
    retry = provider.requests[1].messages[-1].content
    assert "no json block; end with the json block" in retry


# ---- reproduce_fix_review ----

# passes until the tester's test exists, then until the coder's fix exists
REPRODUCED = "! test -f tests/test_x.py || test -f fixed.txt"
ADD_TEST = calls(call("write", path="tests/test_x.py", content="assert fixed\n"))


def test_reproduce_fix_review_fixes_against_the_new_test(tmp_path: Path) -> None:
    replies = [
        ADD_TEST,
        text("added tests/test_x.py"),
        calls(call("write", id="w2", path="fixed.txt", content="ok")),
        text("fixed"),
        text(APPROVE),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = reproduce_fix_review(run, "fix it", check=[REPRODUCED])
    assert (r.status, r.summary) == ("done", "approved in round 1")
    tester_task = provider.requests[0].messages[1].content
    assert "new files under tests" in tester_task
    coder_task = provider.requests[2].messages[1].content
    assert "The checks failed" in coder_task and "[exit 1]" in coder_task
    review_task = provider.requests[4].messages[1].content
    assert 'name="reproduction"' in review_task and "tests/test_x.py" in review_task


def test_reproduce_needs_a_passing_suite_first(tmp_path: Path) -> None:
    run, provider, _ = make_run(tmp_path, [])
    r = reproduce_fix_review(run, "o", check=["false"])
    assert r.summary == "the check fails before any change: `false` failed: exit 1"
    assert provider.requests == []


def test_the_tester_may_only_add_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_old.py").write_text("x = 1\n")
    replies = [
        calls(call("write", path="tests/test_old.py", content="x = 2\n")),
        text("edited"),
    ]
    run, _, _ = make_run(tmp_path, replies)
    r = reproduce_fix_review(run, "o", check=["true"])
    assert r.summary == "the tester changed tests/test_old.py; it may only add"


def test_the_tester_writes_only_under_test_paths(tmp_path: Path) -> None:
    replies = [calls(call("write", path="calc.py", content="x")), text("done")]
    run, _, _ = make_run(tmp_path, replies)
    r = reproduce_fix_review(run, "o", check=["true"])
    assert r.summary == "the tester added no files under tests"
    assert not (tmp_path / "calc.py").exists()
    assert TESTER.write_roots == ()  # the workflow sets them per run


def test_a_test_that_does_not_fail_goes_back_to_the_tester(tmp_path: Path) -> None:
    replies = [
        calls(call("write", path="tests/test_a.py", content="pass")),
        text("added"),
        ADD_TEST,
        text("added a failing one"),
        calls(call("write", id="w2", path="fixed.txt", content="ok")),
        text("fixed"),
        text(APPROVE),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = reproduce_fix_review(run, "o", check=[REPRODUCED], check_to_reproduce=1)
    assert r.status == "done", r.summary
    second_tester_task = provider.requests[2].messages[1].content
    assert "The checks still pass with your tests" in second_tester_task
    (tmp_path / "again").mkdir()
    run, _, _ = make_run(tmp_path / "again", replies[:2])
    r = reproduce_fix_review(run, "o", check=["true"])
    assert r.summary == "the check passes with the new tests: 1 commands passed"


def test_the_coder_may_not_change_the_new_tests(tmp_path: Path) -> None:
    replies = [
        ADD_TEST,
        text("added"),
        calls(call("write", id="w2", path="tests/test_x.py", content="pass\n")),
        text("weakened it"),
    ]
    run, _, _ = make_run(tmp_path, replies)
    r = reproduce_fix_review(run, "o", check=[REPRODUCED])
    assert r.summary.startswith("round 1 changed tests/test_x.py, which the check")


def test_a_test_path_must_be_inside_the_workdir(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, [])
    r = reproduce_fix_review(run, "o", check=["true"], test_paths=["../out"])
    assert r.summary == "test path ../out must be inside the workdir"
    assert not (tmp_path.parent / "out").exists()


# ---- best_of_n ----

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@localhost"]


def git_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    """path as a git repo with one commit holding files."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for rel, content in (files or {"README": "base\n"}).items():
        (path / rel).write_text(content)
    subprocess.run([*GIT, "add", "-A"], cwd=path, check=True)
    subprocess.run([*GIT, "commit", "-qm", "base"], cwd=path, check=True)
    return path


def rev(path: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def write_fixed(content: str, cid: str) -> list[Reply]:
    return [
        calls(call("write", id=f"w{cid}", path="fixed.txt", content=content)),
        text(f"fixed in {cid}"),
    ]


@needs_git
def test_best_of_n_stops_at_the_first_pass(tmp_path: Path) -> None:
    git_repo(tmp_path)
    run, provider, _ = make_run(tmp_path, [*write_fixed("one", "1"), text(APPROVE)])
    r = best_of_n(run, "fix", check=["test -f fixed.txt"], stop_after=1)
    assert (r.status, r.summary) == (
        "done",
        "c1 of 1 passing candidates: approved in round 1",
    )
    assert (tmp_path / "fixed.txt").read_text() == "one"
    assert len(provider.requests) == 3  # one coder, no selector, the review


@needs_git
def test_best_of_n_picks_among_the_candidates_that_pass(tmp_path: Path) -> None:
    git_repo(tmp_path)
    replies = [
        text("tried"),  # c1 fails the check
        *write_fixed("two", "2"),
        *write_fixed("three", "3"),
        text(block(choice=[{"id": "c3", "reason": "handles more cases"}])),
        text(APPROVE),
    ]
    run, provider, _ = make_run(tmp_path, replies)
    r = best_of_n(run, "fix", check=["test -f fixed.txt"], n=3)
    assert r.summary == "c3 of 2 passing candidates: approved in round 1"
    assert (tmp_path / "fixed.txt").read_text() == "three"
    select_task = provider.requests[5].messages[1].content
    assert 'name="candidate-c2"' in select_task and 'name="candidate-c3"' in select_task
    assert "candidate-c1" not in select_task


@needs_git
def test_best_of_n_fails_when_no_candidate_passes(tmp_path: Path) -> None:
    git_repo(tmp_path)
    run, provider, _ = make_run(tmp_path, [text("tried"), text("tried")])
    r = best_of_n(run, "fix", check=["false"], n=2)
    fails = "c1: `false` failed: exit 1; c2: `false` failed: exit 1"
    assert (r.status, r.summary) == (
        "failed",
        f"no candidate passed the check: {fails}",
    )
    assert len(provider.requests) == 2


@needs_git
def test_best_of_n_needs_a_clean_repo(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, [])
    assert best_of_n(run, "o", check=["true"]).summary == (
        "best-of-n needs a git repo with a commit"
    )
    git_repo(tmp_path)
    (tmp_path / "README").write_text("changed\n")
    assert best_of_n(run, "o", check=["true"]).summary == (
        "best-of-n needs a clean tree; commit first"
    )


@needs_git
def test_take_branch_copies_changes_and_removals(tmp_path: Path) -> None:
    ws = git_repo(tmp_path / "ws", {"keep.txt": "k\n", "gone.txt": "g\n"})
    copy = tmp_path / "copy"
    subprocess.run(["git", "clone", "-q", str(ws), str(copy)], check=True)
    subprocess.run(["git", "checkout", "-qb", "c1"], cwd=copy, check=True)
    (copy / "gone.txt").unlink()
    (copy / "keep.txt").write_text("changed\n")
    (copy / "new.txt").write_text("n\n")
    subprocess.run([*GIT, "add", "-A"], cwd=copy, check=True)
    subprocess.run([*GIT, "commit", "-qm", "c1"], cwd=copy, check=True)
    base = rev(ws)
    take_branch(ws, copy, "c1", base)
    assert sorted(p.name for p in ws.iterdir() if p.name != ".git") == [
        "keep.txt",
        "new.txt",
    ]
    assert (ws / "keep.txt").read_text() == "changed\n"
    assert rev(ws) == base  # the engine commits later; HEAD does not move


def test_a_view_charges_its_parent_and_keeps_its_own_cap(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path, coder_turn())
    view = run.view(tmp_path, Budget(turns=1))
    r = view.run_agent(CODER, Task("fix"))
    assert r.status == "budget"
    assert view.used.turns == run.used.turns == run.session.used.turns == 1
