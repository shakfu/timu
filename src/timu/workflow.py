"""Workflows: a Run shares one budget, id sequence, sink and cancel event across the
agents it starts. fix_review is the first built-in pipeline (design 7)."""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from timu.agent import Agent
from timu.events import Event, EventSink
from timu.provider.base import Provider
from timu.report import ReportError, file_root, is_gitignored, write_report
from timu.role import Role
from timu.roles import CODER, LEAD, REVIEWER, REVIEWER_WRITE, researcher, with_skills
from timu.sandbox import Sandbox
from timu.tool import DelegateError, Tool
from timu.tools.web import WEB_FETCH
from timu.types import Artifact, Budget, Capability, Origin, Result, Status, Task, Usage

WORKFLOW_BUDGET = Budget(turns=200, tool_calls=400, tokens=2_000_000, wall_seconds=3600)
URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
VERDICT = re.compile(r"^[\s*#>`_]*VERDICT:\s*(APPROVE|CHANGES)\b", re.IGNORECASE)
ReportMode = Literal["return", "write"]


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Run:
    """Starts agents under one workflow budget, charged live as each agent spends, so
    agents running at once (a lead and its children) draw from the same remainder.
    Each agent also gets the smaller of its own budget and that remainder."""

    def __init__(
        self,
        workdir: Path,
        provider_for: Callable[[Role], Provider],
        sink: EventSink,
        budget: Budget = WORKFLOW_BUDGET,
        *,
        run_id: str | None = None,
        cancel: threading.Event | None = None,
        sandbox: Sandbox | None = None,
        skill_roots: Sequence[Path] | None = None,
        clock: Callable[[], float] = time.monotonic,
        approve: Callable[[Artifact, Role], bool] | None = None,
        roles: Mapping[str, Role] | None = None,
        max_depth: int = 2,
    ) -> None:
        """approve, if given, is asked before web content first reaches a role that can
        run commands or write files (design 6). roles are what delegate can start."""
        self.workdir = Path(os.path.realpath(workdir))
        self.provider_for = provider_for
        self.sink = sink
        self.budget = budget
        self.run_id = run_id or new_run_id()
        self.cancel = cancel or threading.Event()
        self.sandbox = sandbox
        self.skill_roots = skill_roots
        self.clock = clock
        self.approve = approve
        self.roles = dict(roles or {})
        self.max_depth = max_depth
        self.used = Usage()
        self.results: dict[
            str, tuple[Role, Result]
        ] = {}  # by agent id, for delegate inputs
        self._approved: set[Artifact] = set()
        self._tainted: set[str] = (
            set()
        )  # agents that received an untrusted child result
        self._agents = 0
        self._start = clock()

    def emit(self, kind: str, **data: Any) -> None:
        self.sink(Event(kind, self.run_id, "", "workflow", time.time(), data))

    def run_agent(
        self, role: Role, task: Task, parent_id: str = "", depth: int = 0
    ) -> Result:
        """Build an agent for role and run task. Raises RoleError for a bad role."""
        self._agents += 1
        agent_id = f"a{self._agents}"
        inherited = (
            parent_id in self._tainted
        )  # the task was written after reading web content
        if refused := self._gate(role, task, inherited):
            return Result("refused", refused, (), Usage(), agent_id, True)
        budget = self._child_budget(task.budget or role.budget)
        if budget is None:
            return Result("budget", "workflow budget exhausted", (), Usage(), agent_id)
        agent = Agent(
            role,
            self.provider_for(role),
            self.sink,
            self.workdir,
            agent_id=agent_id,
            parent_id=parent_id,
            cancel=self.cancel,
            sandbox=self.sandbox,
            skill_roots=self.skill_roots,
            clock=self.clock,
            untrusted=inherited,
            delegate=self._delegator(role, agent_id, depth) if role.delegates else None,
            on_usage=self._charge,
            outer_limit=self._exhausted,
        )
        result = agent.run(replace(task, budget=budget))
        self.results[agent_id] = (role, result)
        if result.untrusted and parent_id:
            self._tainted.add(parent_id)
        return result

    def _charge(self, delta: Usage) -> None:
        self.used += delta

    def _exhausted(self) -> str | None:
        limit = self.used.exceeds(self.budget, self.clock() - self._start)
        return f"workflow {limit}" if limit else None

    def _delegator(
        self, role: Role, agent_id: str, depth: int
    ) -> Callable[[str, str, str, tuple[str, ...]], Result]:
        def delegate(
            name: str, goal: str, accept: str, input_ids: tuple[str, ...]
        ) -> Result:
            if name not in role.delegates:
                allowed = ", ".join(role.delegates)
                raise DelegateError(f"{role.name} may delegate only to: {allowed}")
            if depth + 1 > self.max_depth:
                raise DelegateError(
                    f"delegation depth limit ({self.max_depth}) reached"
                )
            if name not in self.roles:
                raise DelegateError(f"role {name} is not available in this run")
            inputs = []
            for i in input_ids:
                if i not in self.results:
                    raise DelegateError(
                        f"no result with id {i}; ids: {', '.join(self.results)}"
                    )
                src, res = self.results[i]
                origin = Origin.NET if Capability.NET in src.grants else Origin.AGENT
                inputs.append(
                    Artifact(
                        f"result-{i}", res.summary, "result", origin, i, res.untrusted
                    )
                )
            task = Task(goal, tuple(inputs), accept)
            return self.run_agent(self.roles[name], task, agent_id, depth + 1)

        return delegate

    def _gate(self, role: Role, task: Task, inherited: bool) -> str | None:
        """Why role may not receive task, or None. Asks about first-hand web content,
        and about a goal written by an agent that has read web content."""
        if self.approve is None or not role.grants & {
            Capability.EXEC,
            Capability.FS_WRITE,
        }:
            return None
        pending = [
            a for a in task.inputs if a.origin is Origin.NET and a not in self._approved
        ]
        if inherited:
            pending.append(
                Artifact("goal", task.goal, "goal", Origin.AGENT, untrusted=True)
            )
        for a in pending:
            ok = self.approve(a, role)
            self.emit("approval", artifact=a.name, role=role.name, approved=ok)
            if not ok:
                return f"untrusted input {a.name} was not approved for {role.name}"
            self._approved.add(a)
        return None

    def _child_budget(self, want: Budget) -> Budget | None:
        """The smaller of want and the workflow's remainder; None if nothing is left."""
        b, u = self.budget, self.used
        cost = b.cost_usd - u.cost_usd if b.cost_usd is not None else None
        if want.cost_usd is not None:
            cost = want.cost_usd if cost is None else min(want.cost_usd, cost)
        child = Budget(
            turns=min(want.turns, b.turns - u.turns),
            tool_calls=min(want.tool_calls, b.tool_calls - u.tool_calls),
            tokens=min(want.tokens, b.tokens - u.tokens),
            cost_usd=cost,
            wall_seconds=min(
                want.wall_seconds, b.wall_seconds - (self.clock() - self._start)
            ),
        )
        exhausted = (
            min(child.turns, child.tool_calls, child.tokens, child.wall_seconds) <= 0
        )
        return None if exhausted or (cost is not None and cost <= 0) else child


def verdict(report: str) -> str | None:
    """APPROVE or CHANGES from the report's first three non-empty lines, else None."""
    for line in [x for x in report.splitlines() if x.strip()][:3]:
        if m := VERDICT.match(line):
            return m.group(1).upper()
    return None


def fix_review(
    run: Run,
    objective: str,
    *,
    max_rounds: int = 3,
    report_path: str = "REVIEW.md",
    report_mode: ReportMode = "return",
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
) -> Result:
    """Code, then review, until the reviewer approves or max_rounds pass. The report
    reaches report_path by the workflow (mode "return") or the reviewer ("write").
    inputs go to the coder in every round. Raises ReportError if report_path fails
    the checks in design 4.3."""
    report_file = file_root(run.workdir, report_path)
    if is_gitignored(run.workdir, report_path) is False:
        run.emit("warning", message=f"{report_path} is not gitignored")
    skills = skills or {}
    coder = with_skills(CODER, skills.get("coder", ()))
    reviewer = REVIEWER
    if report_mode == "write":
        reviewer = replace(REVIEWER_WRITE, write_files=(report_path,))
    reviewer = with_skills(reviewer, skills.get("reviewer", ()))
    review_goal = f"Review the coder's work on this goal:\n\n{objective}"
    if report_mode == "write":
        review_goal += f"\n\nReport path: {report_path}"

    def done(status: Status, summary: str, report: Artifact | None = None) -> Result:
        run.emit("workflow_result", status=status, summary=summary)
        artifacts = (report,) if report else ()
        tainted = any(a.tainted for a in (*artifacts, *inputs))
        return Result(status, summary, artifacts, run.used, run.run_id, tainted)

    findings: Artifact | None = None
    for n in range(1, max_rounds + 1):
        goal = objective
        if findings:
            goal += (
                "\n\nThe reviewer asked for changes. Their report is the review input."
            )
        run.emit("round", n=n, stage="code")
        given = (*inputs, findings) if findings else inputs
        coded = run.run_agent(
            coder, Task(goal, given, "The goal is met and the tests pass.")
        )
        if coded.status != "done":
            return done(
                coded.status, f"coder stopped in round {n}: {coded.summary}", findings
            )

        run.emit("round", n=n, stage="review")
        before = _read(report_file)
        work = Artifact(
            "coder-summary",
            coded.summary,
            "text",
            Origin.AGENT,
            coded.trace_id,
            coded.untrusted,
        )
        reviewed = run.run_agent(reviewer, Task(review_goal, (work,)))
        if reviewed.status != "done":
            return done(
                reviewed.status,
                f"reviewer stopped in round {n}: {reviewed.summary}",
                findings,
            )

        if report_mode == "write":
            text = _read(report_file)
            if text is None or text == before:
                return done(
                    "failed",
                    f"reviewer did not write {report_path} in round {n}",
                    findings,
                )
        else:
            text = reviewed.summary
            try:
                write_report(run.workdir, report_path, text)
            except ReportError as e:
                return done("failed", f"cannot write the report: {e}", findings)
        findings = Artifact(
            "review",
            text,
            "report",
            Origin.AGENT,
            reviewed.trace_id,
            reviewed.untrusted,
        )
        result = verdict(text)
        run.emit("round", n=n, stage="verdict", verdict=result)
        if result == "APPROVE":
            return done("done", f"approved in round {n}", findings)
        if result is None:
            return done(
                "failed", f"the review in round {n} has no VERDICT line", findings
            )
    return done("failed", f"not approved after {max_rounds} rounds", findings)


def research_fix_review(
    run: Run,
    objective: str,
    *,
    search: Tool | None = None,
    fetch: Tool = WEB_FETCH,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    **fix: Any,
) -> Result:
    """Research on the web, then fix_review with the findings as an untrusted input.
    fix takes fix_review's keyword arguments."""
    role = with_skills(researcher(search, fetch), (skills or {}).get("researcher", ()))
    run.emit("round", n=0, stage="research")
    found = run.run_agent(
        role,
        Task(
            "Find what a coder needs to know to do this. Report facts; do not solve it."
            f"\n\n{objective}"
        ),
    )
    if found.status != "done":
        summary = f"researcher stopped: {found.summary}"
        run.emit("workflow_result", status=found.status, summary=summary)
        return Result(found.status, summary, (), run.used, run.run_id, True)
    research = Artifact(
        "research", found.summary, "research", Origin.NET, found.trace_id, True
    )
    urls = "\n".join(
        dict.fromkeys(u.rstrip(".,;:") for u in URL.findall(found.summary))
    )
    sources = Artifact("sources", urls, "sources", Origin.NET, found.trace_id, True)
    result = fix_review(run, objective, skills=skills, inputs=(research,), **fix)
    return replace(result, artifacts=(*result.artifacts, research, sources))


def lead(
    run: Run,
    objective: str,
    *,
    search: Tool | None = None,
    fetch: Tool = WEB_FETCH,
    skills: Mapping[str, tuple[str, ...]] | None = None,
) -> Result:
    """A lead agent meets the objective by delegating to the researcher, coder and
    reviewer. The run's roles are set here, so delegate can start them."""
    skills = skills or {}
    team = {
        "researcher": researcher(search, fetch),
        "coder": CODER,
        "reviewer": REVIEWER,
    }
    run.roles.update({n: with_skills(r, skills.get(n, ())) for n, r in team.items()})
    result = run.run_agent(with_skills(LEAD, skills.get("lead", ())), Task(objective))
    run.emit("workflow_result", status=result.status, summary=result.summary)
    return replace(result, usage=run.used, trace_id=run.run_id)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
