"""Workflows: a Session shares one budget, id sequence, sink and cancel event across
the agents it starts; a Run binds it to one workdir. WORKFLOWS holds the built-ins
(design 7, extensibility 5.1)."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from timu.agent import Agent
from timu.events import Event, EventSink
from timu.provider.base import Provider
from timu.report import ReportError, file_root, is_gitignored, write_report
from timu.role import Role
from timu.roles import CODER, LEAD, REVIEWER, REVIEWER_WRITE, researcher, with_skills
from timu.sandbox import Policy, Sandbox
from timu.tool import PROTECTED, DelegateError, Tool
from timu.tools.shell import run_sh, scrubbed_env
from timu.tools.web import WEB_FETCH
from timu.types import Artifact, Budget, Capability, Origin, Result, Status, Task, Usage

WORKFLOW_BUDGET = Budget(turns=200, tool_calls=400, tokens=2_000_000, wall_seconds=3600)
URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
VERDICT = re.compile(r"^[\s*#>`_]*VERDICT:\s*(APPROVE|CHANGES)\b", re.IGNORECASE)
ReportMode = Literal["return", "write"]


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Session:
    """What every Run in one invocation shares: id, sink, budget, cancel flag and
    approvals. The budget is charged live as each agent spends, so agents running at
    once (a lead and its children) draw from the same remainder."""

    def __init__(
        self,
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
        max_depth: int = 2,
    ) -> None:
        """approve, if given, is asked before web content first reaches a role that can
        run commands or write files (design 6)."""
        self.provider_for = provider_for
        self.sink = sink
        self.budget = budget
        self.run_id = run_id or new_run_id()
        self.cancel = cancel or threading.Event()
        self.sandbox = sandbox
        self.skill_roots = skill_roots
        self.clock = clock
        self.approve = approve
        self.max_depth = max_depth
        self.used = Usage()
        self.approved: set[Artifact] = set()
        self.agents = 0  # agent ids are unique across the session
        self.start = clock()

    def at(
        self,
        workdir: Path,
        roles: Mapping[str, Role] | None = None,
        *,
        node: str = "",
        budget: Budget | None = None,
        reads: tuple[Path, ...] = (),
    ) -> Run:
        """A Run on workdir in this session. Its events carry node; budget, if given,
        caps what its agents spend together. Its command steps may read reads."""
        run = Run.__new__(Run)
        run._bind(self, workdir, roles, node, budget, reads)
        return run

    def emit(self, kind: str, **data: Any) -> None:
        self.sink(Event(kind, self.run_id, "", "workflow", time.time(), data))


class Run:
    """Starts agents on one workdir under a Session. Each agent gets the smaller of its
    own budget, the Run's remainder if it has a budget, and the session's remainder."""

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
        """A Run with its own Session, which takes the other arguments. roles are what
        delegate can start."""
        session = Session(
            provider_for,
            sink,
            budget,
            run_id=run_id,
            cancel=cancel,
            sandbox=sandbox,
            skill_roots=skill_roots,
            clock=clock,
            approve=approve,
            max_depth=max_depth,
        )
        self._bind(session, workdir, roles)

    def _bind(
        self,
        session: Session,
        workdir: Path,
        roles: Mapping[str, Role] | None,
        node: str = "",
        budget: Budget | None = None,
        reads: tuple[Path, ...] = (),
    ) -> None:
        self.session = session
        self.reads = reads
        self.workdir = Path(os.path.realpath(workdir))
        self.roles = dict(roles or {})
        self.node = node
        self.budget = budget
        self.used = Usage()  # this Run's agents; the session also counts them
        self._start = session.clock()
        self.sink: EventSink = (
            session.sink if not node else lambda e: session.sink(replace(e, node=node))
        )
        self.results: dict[
            str, tuple[Role, Result]
        ] = {}  # by agent id, for delegate inputs
        self._tainted: set[str] = (
            set()
        )  # agents that received an untrusted child result

    def at(self, workdir: Path, roles: Mapping[str, Role] | None = None) -> Run:
        """A Run on workdir in the same session. Delegate inputs and roles are not
        shared, so agents on one workdir cannot name results from another."""
        return self.session.at(workdir, roles)

    @property
    def run_id(self) -> str:
        return self.session.run_id

    def emit(self, kind: str, **data: Any) -> None:
        self.sink(Event(kind, self.run_id, "", "workflow", time.time(), data))

    def run_agent(
        self, role: Role, task: Task, parent_id: str = "", depth: int = 0
    ) -> Result:
        """Build an agent for role and run task. Raises RoleError for a bad role."""
        ses = self.session
        ses.agents += 1
        agent_id = f"a{ses.agents}"
        inherited = (
            parent_id in self._tainted
        )  # the task was written after reading web content
        if refused := self._gate(role, task, inherited):
            return Result("refused", refused, (), Usage(), agent_id, True)
        budget, scope = self._limit(task.budget or role.budget)
        if budget is None:
            return Result("budget", f"{scope} budget exhausted", (), Usage(), agent_id)
        agent = Agent(
            role,
            ses.provider_for(role),
            self.sink,
            self.workdir,
            agent_id=agent_id,
            parent_id=parent_id,
            cancel=ses.cancel,
            sandbox=ses.sandbox,
            skill_roots=ses.skill_roots,
            clock=ses.clock,
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
        self.session.used += delta

    def _caps(self) -> list[tuple[str, Budget, Usage, float]]:
        """(scope, budget, used, elapsed) for the session and, if capped, this Run."""
        s = self.session
        now = s.clock()
        caps = [("workflow", s.budget, s.used, now - s.start)]
        if self.budget is not None:
            caps.append(("node", self.budget, self.used, now - self._start))
        return caps

    def _exhausted(self) -> str | None:
        for scope, budget, used, elapsed in self._caps():
            if limit := used.exceeds(budget, elapsed):
                return f"{scope} {limit}"
        return None

    def _delegator(
        self, role: Role, agent_id: str, depth: int
    ) -> Callable[[str, str, str, tuple[str, ...]], Result]:
        def delegate(
            name: str, goal: str, accept: str, input_ids: tuple[str, ...]
        ) -> Result:
            if name not in role.delegates:
                allowed = ", ".join(role.delegates)
                raise DelegateError(f"{role.name} may delegate only to: {allowed}")
            if depth + 1 > self.session.max_depth:
                raise DelegateError(
                    f"delegation depth limit ({self.session.max_depth}) reached"
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
        approve, approved = self.session.approve, self.session.approved
        if approve is None or not role.grants & {
            Capability.EXEC,
            Capability.FS_WRITE,
        }:
            return None
        pending = [
            a for a in task.inputs if a.origin is Origin.NET and a not in approved
        ]
        if inherited:
            pending.append(
                Artifact("goal", task.goal, "goal", Origin.AGENT, untrusted=True)
            )
        for a in pending:
            ok = approve(a, role)
            self.emit("approval", artifact=a.name, role=role.name, approved=ok)
            if not ok:
                return f"untrusted input {a.name} was not approved for {role.name}"
            approved.add(a)
        return None

    def _child_budget(self, want: Budget) -> Budget | None:
        """The smaller of want and each remainder; None if one is spent."""
        return self._limit(want)[0]

    def _limit(self, want: Budget) -> tuple[Budget | None, str]:
        """_child_budget, and the scope that is spent when it is None."""
        for scope, budget, used, elapsed in self._caps():
            if (left := _remainder(want, budget, used, elapsed)) is None:
                return None, scope
            want = left
        return want, ""


def _remainder(want: Budget, b: Budget, u: Usage, elapsed: float) -> Budget | None:
    """The smaller of want and what b has left after u; None if nothing is left."""
    cost = b.cost_usd - u.cost_usd if b.cost_usd is not None else None
    if want.cost_usd is not None:
        cost = want.cost_usd if cost is None else min(want.cost_usd, cost)
    child = Budget(
        turns=min(want.turns, b.turns - u.turns),
        tool_calls=min(want.tool_calls, b.tool_calls - u.tool_calls),
        tokens=min(want.tokens, b.tokens - u.tokens),
        cost_usd=cost,
        wall_seconds=min(want.wall_seconds, b.wall_seconds - elapsed),
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
    inputs: tuple[Artifact, ...] = (),
    **fix: Any,
) -> Result:
    """Research on the web, then fix_review with the findings as an untrusted input,
    after inputs. fix takes fix_review's other keyword arguments."""
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
    result = fix_review(
        run, objective, skills=skills, inputs=(*inputs, research), **fix
    )
    return replace(result, artifacts=(*result.artifacts, research, sources))


def lead(
    run: Run,
    objective: str,
    *,
    search: Tool | None = None,
    fetch: Tool = WEB_FETCH,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
) -> Result:
    """A lead agent meets the objective by delegating to the researcher, coder and
    reviewer. The run's roles are set here, so delegate can start them. inputs go to
    the lead."""
    skills = skills or {}
    team = {
        "researcher": researcher(search, fetch),
        "coder": CODER,
        "reviewer": REVIEWER,
    }
    run.roles.update({n: with_skills(r, skills.get(n, ())) for n, r in team.items()})
    lead_role = with_skills(LEAD, skills.get("lead", ()))
    result = run.run_agent(lead_role, Task(objective, inputs))
    run.emit("workflow_result", status=result.status, summary=result.summary)
    return replace(result, usage=run.used, trace_id=run.run_id)


MAX_LOG = 64 * 1024  # bytes of output kept per command


def run_steps(
    run: Run, steps: Sequence[str], network: bool, timeout: float
) -> tuple[Status, str, Artifact]:
    """Run steps in order in run's workdir and sandbox, with no model. Stops at the
    first failure. Returns the status, a summary and the log. Output from a step with
    network on counts as web content (design 6)."""
    ses = run.session
    if ses.sandbox is None:
        return "failed", "command steps need a sandbox", Artifact("log", "", "log")
    policy = Policy(
        (run.workdir, *run.reads),
        (run.workdir,),
        tuple(run.workdir / p for p in PROTECTED),
        network,
    )
    origin = Origin.NET if network else Origin.AGENT
    log: list[str] = []
    status: Status = "done"
    summary = f"{len(steps)} commands passed"
    with tempfile.TemporaryDirectory(prefix="timu-") as tmp:
        home = Path(os.path.realpath(tmp))
        step_policy = replace(policy, write_roots=(*policy.write_roots, home))
        for cmd in steps:
            left, scope = run._limit(
                Budget(turns=1, tool_calls=1, tokens=1, wall_seconds=timeout)
            )
            if left is None:
                status, summary = "budget", f"{scope} budget exhausted before `{cmd}`"
                break
            run.emit("command", command=cmd, network=network)
            ran = run_sh(
                cmd,
                cwd=run.workdir,
                sandbox=ses.sandbox,
                policy=step_policy,
                env=scrubbed_env(home),
                timeout=left.wall_seconds,
                cancel=ses.cancel,
                max_output=MAX_LOG,
            )
            if ran.stopped:
                end = ran.stopped
            elif ran.code is None:
                end = "not started"
            else:
                end = f"exit {ran.code}"
            run.emit("command_result", command=cmd, result=end)
            log.append(f"$ {cmd}\n{ran.text.rstrip()}\n[{end}]\n")
            if ran.code != 0:
                status = "cancelled" if ran.stopped == "cancelled" else "failed"
                summary = f"`{cmd}` failed: {end}"
                break
    text = "".join(log)
    return status, summary, Artifact("log", text, "log", origin, run.run_id, network)


def commands(
    run: Run,
    objective: str,
    *,
    steps: Sequence[str],
    network: bool = False,
    timeout: float = 600,
) -> Result:
    """Run steps with no model (graph.md 7.7). objective only labels the node."""
    status, summary, log = run_steps(run, steps, network, timeout)
    run.emit("workflow_result", status=status, summary=summary)
    return Result(status, summary, (log,), run.used, run.run_id, network)


def check_fix_review(
    run: Run,
    objective: str,
    *,
    check: Sequence[str],
    network: bool = False,
    timeout: float = 600,
    max_rounds: int = 3,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
    **fix: Any,
) -> Result:
    """Run check; while it fails, fix_review with the log as an input and run check
    again, up to max_rounds times (graph.md 7.7). The coder has no network; check
    rebuilds whatever needs it."""
    status, summary, log = run_steps(run, check, network, timeout)
    review: Artifact | None = None
    goal = f"{objective}\n\nThe checks failed. Their output is the log input; make them pass."
    for n in range(1, max_rounds + 1):
        if status != "failed":
            break
        run.emit("round", n=n, stage="fix")
        fixed = fix_review(
            run,
            goal,
            max_rounds=max_rounds,
            skills=skills,
            inputs=(*inputs, log),
            **fix,
        )
        review = next((a for a in fixed.artifacts if a.name == "review"), review)
        if fixed.status != "done":
            status, summary = fixed.status, f"fix round {n}: {fixed.summary}"
            break
        status, summary, log = run_steps(run, check, network, timeout)
    else:
        if status == "failed":
            summary = f"checks still fail after {max_rounds} fix rounds: {summary}"
    run.emit("workflow_result", status=status, summary=summary)
    artifacts = (log, review) if review else (log,)
    untrusted = network or any(a.tainted for a in (*inputs, *artifacts))
    return Result(status, summary, artifacts, run.used, run.run_id, untrusted)


@dataclass(frozen=True)
class Options:
    """What the caller passes every workflow. params are checked against the
    workflow's schema; inputs are artifacts from upstream graph nodes."""

    skills: Mapping[str, tuple[str, ...]]
    search: Tool | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    inputs: tuple[Artifact, ...] = ()


Param = Callable[[Any], Any]
"""Converts a param value, from the CLI or a graph file, or raises ValueError."""


@dataclass(frozen=True)
class Workflow:
    name: str
    help: str
    roles: tuple[str, ...]  # whose providers are checked before the run
    approve_untrusted: bool  # the default for --approve-untrusted
    run: Callable[[Run, str, Options], Result]
    params: Mapping[str, Param] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()  # names of the artifacts a Result may carry
    graph_only: bool = False  # needs params that only a graph file can set


def positive_int(v: Any) -> int:
    n = int(v) if isinstance(v, str) and v.isdigit() else v
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError("must be a positive integer")
    return n


def string(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError("must be a string")  # noqa: TRY004 - the Param contract
    return v


def report_mode(v: Any) -> ReportMode:
    if v not in ("return", "write"):
        raise ValueError('must be "return" or "write"')
    return v  # type: ignore[no-any-return]


def boolean(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("must be true or false")  # noqa: TRY004 - the Param contract
    return v


class Commands:
    """A list of shell commands. The graph may substitute outputs into them
    (graph.md 7.7); this converter sees them after substitution."""

    def __call__(self, v: Any) -> tuple[str, ...]:
        if (
            not isinstance(v, list | tuple)
            or not v
            or not all(isinstance(c, str) and c.strip() for c in v)
        ):
            raise ValueError("must be a non-empty list of commands")
        return tuple(v)


COMMANDS = Commands()

FIX_PARAMS: Mapping[str, Param] = {
    "max_rounds": positive_int,
    "report_path": string,
    "report_mode": report_mode,
}

WORKFLOWS = {
    w.name: w
    for w in (
        Workflow(
            "fix-review",
            "a coder, then a reviewer, until the reviewer approves",
            ("coder", "reviewer"),
            False,
            lambda run, obj, o: fix_review(
                run, obj, skills=o.skills, inputs=o.inputs, **o.params
            ),
            FIX_PARAMS,
            ("review",),
        ),
        Workflow(
            "research-fix-review",
            "a researcher looks things up on the web first",
            ("coder", "reviewer", "researcher"),
            False,
            lambda run, obj, o: research_fix_review(
                run, obj, search=o.search, skills=o.skills, inputs=o.inputs, **o.params
            ),
            FIX_PARAMS,
            ("review", "research", "sources"),
        ),
        Workflow(
            "commands",
            "fixed commands in the sandbox, with no model",
            (),
            False,
            lambda run, obj, o: commands(run, obj, **o.params),
            {"steps": COMMANDS, "network": boolean, "timeout": positive_int},
            ("log",),
            graph_only=True,
        ),
        Workflow(
            "check-fix-review",
            "run checks; if they fail, fix-review with their log, then check again",
            ("coder", "reviewer"),
            False,
            lambda run, obj, o: check_fix_review(
                run, obj, skills=o.skills, inputs=o.inputs, **o.params
            ),
            {
                **FIX_PARAMS,
                "check": COMMANDS,
                "network": boolean,
                "timeout": positive_int,
            },
            ("log", "review"),
            graph_only=True,
        ),
        Workflow(
            "lead",
            "a lead agent delegates to the researcher, coder and reviewer",
            ("coder", "reviewer", "researcher", "lead"),
            True,  # a lead can copy injected text into goals (design 7)
            lambda run, obj, o: lead(
                run, obj, search=o.search, skills=o.skills, inputs=o.inputs
            ),
        ),
    )
}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
