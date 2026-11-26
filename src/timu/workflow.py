"""Workflows: a Session shares one budget, id sequence, sink and cancel event across
the agents it starts; a Run binds it to one workdir. WORKFLOWS holds the built-ins
(design 7, extensibility 5.1)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

from timu import git
from timu.agent import Agent
from timu.events import Event, EventSink
from timu.git import GitError
from timu.provider.base import Provider
from timu.report import ReportError, file_root, write_report
from timu.role import Role
from timu.roles import (
    CODER,
    FINDER,
    LEAD,
    REVIEWER,
    REVIEWER_WRITE,
    SELECTOR,
    TESTER,
    VALIDATOR,
    VERIFIER,
    researcher,
    with_skills,
)
from timu.sandbox import Policy, Sandbox
from timu.tool import PROTECTED, DelegateError, Tool, state_dir
from timu.tools.shell import refusal, run_sh, scrubbed_env
from timu.tools.web import WEB_FETCH
from timu.types import Artifact, Budget, Capability, Origin, Result, Status, Task, Usage

CHECKER = "reviewer"  # the role a lead's delegation runs after any role that writes
WORKFLOW_BUDGET = Budget(turns=200, tool_calls=400, tokens=2_000_000, wall_seconds=3600)
URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
VERDICT = re.compile(
    r"^[\s*#>`_]*VERDICT[*`_]*:[\s*`_]*(APPROVE|CHANGES)\b", re.IGNORECASE
)
ReportMode = Literal["return", "write"]


RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")  # what new_run_id returns


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Session:
    """What every Run in one invocation shares: id, sink, budget, cancel flag and
    approvals. The budget is charged live as each agent spends, so agents running at
    once (a lead and its children, or parallel graph nodes) draw from the same
    remainder. lock guards the shared state and the sink, so Runs may run in threads."""

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
        model_provider: Callable[[Role, str], Provider] | None = None,
    ) -> None:
        """approve, if given, is asked before web content first reaches a role that can
        run commands or write files, and before a task an agent wrote reaches a role
        with net (design 6). model_provider gives a provider for a role at a model a
        graph node names; without it, provider_for serves every model."""
        self.lock = threading.RLock()
        self.provider_for = provider_for
        self.model_provider = model_provider or (
            lambda role, _model: provider_for(role)
        )

        def locked(e: Event) -> None:
            with self.lock:
                sink(e)

        self.sink: EventSink = locked
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
        # one question at a time, so an artifact is asked about once; events from
        # other threads keep flowing while approve waits for an answer
        self.approval_lock = threading.Lock()
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
        models: Mapping[str, str] | None = None,
    ) -> Run:
        """A Run on workdir in this session. Its events carry node; budget, if given,
        caps what its agents spend together. Its command steps may read reads. models
        maps a role name to the model its agents use instead of the configured one."""
        run = Run.__new__(Run)
        run._bind(self, workdir, roles, node, budget, reads, models)
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
        models: Mapping[str, str] | None = None,
    ) -> None:
        self.session = session
        self.reads = reads
        self.models = dict(models or {})
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
        self._reviewed: tuple[str, str] | None = None  # (tree hash, report path)
        self.parent: Run | None = None  # a view's Run, which it also charges
        self.scope = "node"  # names this Run's budget when it runs out

    def at(self, workdir: Path, roles: Mapping[str, Role] | None = None) -> Run:
        """A Run on workdir in the same session. Delegate inputs and roles are not
        shared, so agents on one workdir cannot name results from another."""
        return self.session.at(workdir, roles)

    def view(self, workdir: Path, budget: Budget | None = None) -> Run:
        """A Run on workdir whose agents also charge this Run and stay within its
        budgets; budget, if given, caps them together."""
        child = Run.__new__(Run)
        child._bind(
            self.session,
            workdir,
            self.roles,
            self.node,
            budget,
            self.reads,
            self.models,
        )
        child.parent, child.scope = self, "candidate"
        return child

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
        with ses.lock:
            ses.agents += 1
            agent_id = f"a{ses.agents}"
        inherited = (
            parent_id in self._tainted
        )  # the task was written after reading web content
        if refused := self._gate(role, task, inherited, parent_id):
            return Result("refused", refused, (), Usage(), agent_id, True)
        budget, scope = self._limit(task.budget or role.budget)
        if budget is None:
            return Result("budget", f"{scope} budget exhausted", (), Usage(), agent_id)
        if inherited or any(a.tainted for a in task.inputs):
            self._tainted.add(agent_id)  # goals it writes follow its inputs
        model = self.models.get(role.name)
        agent = Agent(
            role,
            ses.model_provider(role, model) if model else ses.provider_for(role),
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

    def mark_reviewed(self, report_path: str = "") -> None:
        """Record the tree a review is about to judge. A process that outlives a coder's
        command can still write, so the verdict holds only for this tree."""
        tree = tree_hash(self.workdir, report_path)
        self._reviewed = None if tree is None else (tree, report_path)

    def changed_after_review(self) -> bool:
        if self._reviewed is None:
            return False
        tree, report_path = self._reviewed
        return tree_hash(self.workdir, report_path) != tree

    def affords(self, want: Usage, seconds: float) -> bool:
        """Whether every budget over this Run has want and seconds left."""
        for _scope, b, used, elapsed in self._caps():
            short = (
                b.turns - used.turns < want.turns
                or b.tool_calls - used.tool_calls < want.tool_calls
                or b.tokens - used.tokens < want.tokens
                or b.wall_seconds - elapsed < seconds
                or (
                    b.cost_usd is not None
                    and b.cost_usd - used.cost_usd < want.cost_usd
                )
            )
            if short:
                return False
        return True

    def _charge(self, delta: Usage) -> None:
        self.used += delta
        if self.parent is not None:
            self.parent._charge(delta)
            return
        with self.session.lock:
            self.session.used += delta

    def _caps(self) -> list[tuple[str, Budget, Usage, float]]:
        """(scope, budget, used, elapsed) for the session and, if capped, this Run."""
        s = self.session
        now = s.clock()
        caps = (
            self.parent._caps()
            if self.parent is not None
            else [("workflow", s.budget, s.used, now - s.start)]
        )
        if self.budget is not None:
            caps.append((self.scope, self.budget, self.used, now - self._start))
        return caps

    def _exhausted(self, turns: bool = True) -> str | None:
        for scope, budget, used, elapsed in self._caps():
            if limit := used.exceeds(budget, elapsed, turns=turns):
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
            target = self.roles[name]
            writes = Capability.FS_WRITE in target.grants and name != CHECKER
            if writes and CHECKER not in self.roles:
                raise DelegateError(
                    f"{name} changes files, so the run needs a {CHECKER} to check it"
                )
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
            result = self.run_agent(target, task, agent_id, depth + 1)
            if not writes or result.status != "done":
                return result
            return self._check(name, goal, result, agent_id, depth)

        return delegate

    def _check(
        self, name: str, goal: str, coded: Result, parent_id: str, depth: int
    ) -> Result:
        """Review the work of a role that changed files: every fix is checked. Done
        only on APPROVE; the summary holds both results and their ids."""
        work = Artifact(
            "coder-summary",
            coded.summary,
            "text",
            Origin.AGENT,
            coded.trace_id,
            coded.untrusted,
        )
        task = Task(f"Review the {name}'s work on this goal:\n\n{goal}", (work,))
        self.mark_reviewed()
        reviewed = self.run_agent(self.roles[CHECKER], task, parent_id, depth + 1)
        status = reviewed.status
        if status == "done" and verdict(reviewed.summary) != "APPROVE":
            status = "failed"
        return replace(
            reviewed,
            status=status,
            summary=f"{name} result {coded.trace_id}:\n{coded.summary}\n\n"
            f"{CHECKER} result {reviewed.trace_id}:\n{reviewed.summary}",
            usage=coded.usage + reviewed.usage,
            untrusted=coded.untrusted or reviewed.untrusted,
        )

    def _gate(
        self, role: Role, task: Task, inherited: bool, parent_id: str
    ) -> str | None:
        """Why role may not receive task, or None. For a role that can run commands or
        write files, asks about first-hand web content and a goal written by an agent
        that has read web content. For a role with net, asks about a task an agent
        wrote: its goal and inputs can carry workspace data out (design 6)."""
        approve, approved = self.session.approve, self.session.approved
        outbound = Capability.NET in role.grants and bool(parent_id)
        risky = role.grants & {Capability.EXEC, Capability.FS_WRITE}
        if approve is None or not (outbound or risky):
            return None
        with self.session.approval_lock:
            pending = [
                a
                for a in task.inputs
                if (outbound or a.origin is Origin.NET) and a not in approved
            ]
            if inherited or outbound:
                pending.append(
                    Artifact("goal", task.goal, "goal", Origin.AGENT, parent_id, True)
                )
            for a in pending:
                ok = approve(a, role)
                self.emit("approval", artifact=a.name, role=role.name, approved=ok)
                if not ok:
                    return f"untrusted input {a.name} was not approved for {role.name}"
                approved.add(a)
        return None

    def ask(self, artifact: Artifact, role: Role) -> str | None:
        """Why artifact may not reach role, or None. Asks the session's approve once
        per artifact; with the gate off, nothing is asked."""
        ses = self.session
        if ses.approve is None:
            return None
        with ses.approval_lock:
            if artifact in ses.approved:
                return None
            ok = ses.approve(artifact, role)
            self.emit("approval", artifact=artifact.name, role=role.name, approved=ok)
            if not ok:
                return (
                    f"untrusted input {artifact.name} was not approved for {role.name}"
                )
            ses.approved.add(artifact)
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


def tree_hash(workdir: Path, exclude: str = "") -> str | None:
    """A hash of what a commit under workdir would see: paths, and file contents or link
    targets, without ignored files and exclude. None outside a git repo."""
    files = git.files(workdir)
    if files is None:
        return None
    skip = ""
    if exclude:  # resolved as file_root does, so an absolute path matches too
        lexical = os.path.join(workdir, exclude)
        parent = os.path.realpath(os.path.dirname(lexical))
        skip = os.path.relpath(os.path.join(parent, os.path.basename(lexical)), workdir)
    h = hashlib.sha256()
    for path in files:
        rel = os.path.relpath(path, workdir)
        if rel == skip:
            continue
        h.update(os.fsencode(rel) + b"\0")
        try:
            if path.is_symlink():
                h.update(os.fsencode(os.readlink(path)))
            elif path.is_file():
                h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            pass  # removed since it was listed; the next hash differs
        h.update(b"\0")
    return h.hexdigest()


def verdict(report: str) -> str | None:
    """APPROVE or CHANGES from the report's first three non-empty lines, else None."""
    for line in [x for x in report.splitlines() if x.strip()][:3]:
        if m := VERDICT.match(line):
            return m.group(1).upper()
    return None


TIMING = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b")


def _same_log(a: Artifact, b: Artifact, workdir: Path) -> bool:
    """Whether two check logs match once timings and the workdir path are removed."""

    def norm(text: str) -> str:
        return TIMING.sub("<t>", text.replace(str(workdir), "<w>"))

    return norm(a.content) == norm(b.content)


def _less(a: Usage, b: Usage) -> Usage:
    """a - b, field by field."""
    return Usage(*(getattr(a, f.name) - getattr(b, f.name) for f in fields(Usage)))


def _most(a: Usage, b: Usage) -> Usage:
    """The larger of a and b in each field."""
    return Usage(*(max(getattr(a, f.name), getattr(b, f.name)) for f in fields(Usage)))


def fix_review(
    run: Run,
    objective: str,
    *,
    review_to_fix: int = 0,
    report_path: str = "REVIEW.md",
    report_mode: ReportMode = "return",
    check: Sequence[str] = (),
    check_to_fix: int = 0,
    allow_check_edits: bool = False,
    check_paths: Sequence[str] = (),
    coder_summary: bool = True,
    timeout: float = 600,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
    failed_check: Artifact | None = None,
    review_inputs: tuple[Artifact, ...] = (),
    first: Result | None = None,
) -> Result:
    """Code, then review. On CHANGES, the report goes back to the coder, at most
    review_to_fix times. The report reaches report_path by the workflow (mode
    "return") or the reviewer ("write"). inputs go to the coder in every round.

    check, if given, runs offline after each coder run and before the review. A
    failing check sends its log back to the coder at most check_to_fix times, with
    the unanswered review, if any; the reviewer gets the passing log. A fix that
    changes a file implementing check (check_files) or matching check_paths fails
    the run, unless allow_check_edits. timeout is per check command. coder_summary
    False keeps the coder's summary from the reviewer. failed_check is a check log
    the first coder run starts from; review_inputs go to the reviewer in every round.
    first is a coder result already in the workdir, which round 1 checks and reviews
    instead of running the coder. The result holds the report and the latest check
    log. Raises ReportError if report_path fails design 4.3."""
    report_file = file_root(run.workdir, report_path)
    if git.is_ignored(run.workdir, report_path) is False:
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
    checker = Check(run, check, check_paths, allow_check_edits, timeout)

    seen = (*inputs, failed_check) if failed_check else inputs
    findings: Artifact | None = None  # the latest review, CHANGES until it returns
    log = failed_check  # the failing check's log, for the next coder run
    latest = failed_check  # the latest check log, for the result

    def done(status: Status, summary: str, report: Artifact | None = None) -> Result:
        run.emit("workflow_result", status=status, summary=summary)
        artifacts = tuple(a for a in (report, latest) if a)
        tainted = any(a.tainted for a in (*artifacts, *seen))
        return Result(status, summary, artifacts, run.used, run.run_id, tainted)

    reviews_left, checks_left = review_to_fix, check_to_fix
    largest, longest = Usage(), 0.0  # the most one round has used
    used, started = run.used, run.session.clock()
    n = 0
    while True:
        n += 1
        if n > 1:  # stop early when another round cannot help (proposals 6)
            largest = _most(largest, _less(run.used, used))
            longest = max(longest, run.session.clock() - started)
            if not run.affords(largest, longest):
                return done(
                    "budget",
                    f"round {n} not started: less budget is left than a round used",
                    findings,
                )
            used, started = run.used, run.session.clock()
        tree = tree_hash(run.workdir, report_path) if n > 1 else None
        goal = objective
        if log:
            goal += (
                "\n\nThe checks failed. Their output is the log input; make them pass."
            )
        if findings:
            goal += (
                "\n\nThe reviewer asked for changes. Their report is the review input."
            )
        given = (*inputs, *(a for a in (findings, log) if a))
        if n == 1 and first is not None:
            coded = first
        else:
            run.emit("round", n=n, stage="code")
            coded = run.run_agent(
                coder, Task(goal, given, "The goal is met and the tests pass.")
            )
        if coded.status != "done":
            return done(
                coded.status, f"coder stopped in round {n}: {coded.summary}", findings
            )
        if tree is not None and tree == tree_hash(run.workdir, report_path):
            return done("failed", f"round {n} made no change", findings)

        evidence: tuple[Artifact, ...] = ()
        if check:
            status, summary, ran = checker(n)
            if ran is None:
                return done(status, summary, findings)
            if status == "failed" and checks_left:
                if latest and _same_log(ran, latest, run.workdir):
                    return done(
                        "failed",
                        f"check in round {n} failed with the same output as before",
                        findings,
                    )
                checks_left -= 1
                log = latest = ran
                continue
            latest = ran
            if status != "done":
                return done(status, f"check in round {n}: {summary}", findings)
            log, evidence = None, (ran,)

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
        shown = (*review_inputs, *((work,) if coder_summary else ()), *evidence)
        run.mark_reviewed(report_path)
        reviewed = run.run_agent(reviewer, Task(review_goal, shown))
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
        if not reviews_left:
            return done(
                "failed", f"not approved after {review_to_fix + 1} rounds", findings
            )
        reviews_left -= 1


def reproduce_fix_review(
    run: Run,
    objective: str,
    *,
    check: Sequence[str],
    test_paths: Sequence[str] = ("tests",),
    check_to_reproduce: int = 0,
    timeout: float = 600,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
    **fix: Any,
) -> Result:
    """check must pass; a tester adds files under test_paths that make it fail; then
    fix_review runs with the same check, and the new files join check_paths, so the
    coder cannot change them (workflow-proposals.md 1). A check that still passes
    after the tester goes back to it at most check_to_reproduce times. fix takes
    fix_review's other keyword arguments."""
    tester = with_skills(
        replace(TESTER, write_roots=tuple(test_paths)), (skills or {}).get("tester", ())
    )

    def stop(status: Status, summary: str, *artifacts: Artifact) -> Result:
        run.emit("workflow_result", status=status, summary=summary)
        tainted = any(a.tainted for a in (*inputs, *artifacts))
        return Result(status, summary, artifacts, run.used, run.run_id, tainted)

    run.emit("round", n=0, stage="baseline")
    status, summary, log = run_steps(run, check, False, timeout)
    if status == "failed":  # a failing suite gives no fail-then-pass signal
        return stop("failed", f"the check fails before any change: {summary}", log)
    if status != "done":
        return stop(status, f"baseline check: {summary}", log)

    for p in test_paths:  # made here, so the sandbox can mount a new directory
        path = Path(os.path.realpath(run.workdir / p))
        if not path.is_relative_to(run.workdir) or any(
            part.casefold() == ".git" for part in path.relative_to(run.workdir).parts
        ):
            return stop("failed", f"test path {p} must be inside the workdir")
        path.mkdir(parents=True, exist_ok=True)
    patterns = ["*" if p in ("", ".") else p.rstrip("/") + "/*" for p in test_paths]
    before = snapshot(run.workdir, patterns)
    where = ", ".join(test_paths)
    goal = (
        f"{objective}\n\nAdd tests, as new files under {where}, that fail now because "
        "this goal is not met, and pass once it is. Do not change existing files or "
        "the code under test."
    )
    feedback: Artifact | None = None
    left, n = check_to_reproduce, 0
    while True:
        n += 1
        run.emit("round", n=n, stage="reproduce")
        tested = run.run_agent(
            tester,
            Task(
                goal
                + (
                    "\n\nThe checks still pass with your tests. Their output is "
                    "the log input; make the tests fail for the reason the goal states."
                    if feedback
                    else ""
                ),
                (*inputs, *((feedback,) if feedback else ())),
                "The new tests fail on the current code because the goal is not met.",
            ),
        )
        if tested.status != "done":
            return stop(tested.status, f"tester stopped in round {n}: {tested.summary}")
        after = snapshot(run.workdir, patterns)
        if changed := sorted(k for k in before if after.get(k) != before[k]):
            return stop(
                "failed", f"the tester changed {', '.join(changed)}; it may only add"
            )
        added = sorted(set(after) - set(before))
        if not added:
            return stop("failed", f"the tester added no files under {where}")
        run.emit("round", n=n, stage="confirm")
        status, summary, log = run_steps(run, check, False, timeout)
        if status == "failed":
            break
        if status != "done":
            return stop(status, f"check after the tester in round {n}: {summary}", log)
        if not left:
            return stop(
                "failed", f"the check passes with the new tests: {summary}", log
            )
        left -= 1
        feedback = log

    reproduction = Artifact(
        "reproduction",
        f"A tester added {', '.join(added)} before the fix, and the coder could not "
        "change them. Check that they test what the goal asks. The check with them, "
        f"before the fix:\n\n{log.content}",
        "log",
        Origin.AGENT,
        tested.trace_id,
        tested.untrusted,
    )
    return fix_review(
        run,
        objective,
        check=check,
        timeout=timeout,
        check_paths=(*fix.pop("check_paths", ()), *added),
        skills=skills,
        inputs=inputs,
        failed_check=log,
        review_inputs=(reproduction,),
        **fix,
    )


def parse_choice(text: str, ids: Sequence[str]) -> str:
    """The candidate id a selector chose. Raises ProtocolError."""
    items = json_items(text, "choice", ("id", "reason"))
    if len(items) != 1 or items[0]["id"] not in ids:
        raise ProtocolError(f"choose exactly one of {', '.join(ids)}")
    return items[0]["id"]


def best_of_n(
    run: Run,
    objective: str,
    *,
    check: Sequence[str],
    n: int = 3,
    stop_after: int | None = None,
    models: Sequence[str] = (),
    allow_check_edits: bool = False,
    check_paths: Sequence[str] = (),
    timeout: float = 600,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
    **fix: Any,
) -> Result:
    """Up to n coders, one after another, each on its own clone of the workdir's HEAD
    with an equal share of the budget; stop once stop_after (default n) pass check. A
    selector picks one of those that passed; its change is applied to the workdir,
    then fix_review checks and reviews it (workflow-proposals.md 4). models, if
    given, are the coders' models in turn. fix takes fix_review's other keyword
    arguments."""
    skills = skills or {}
    coder = with_skills(CODER, skills.get("coder", ()))
    selector = with_skills(SELECTOR, skills.get("selector", ()))
    want = n if stop_after is None else min(stop_after, n)

    def stop(status: Status, summary: str) -> Result:
        run.emit("workflow_result", status=status, summary=summary)
        tainted = any(a.tainted for a in inputs)
        return Result(status, summary, (), run.used, run.run_id, tainted)

    try:
        base = git.head(run.workdir)
        dirty = git.run(run.workdir, "status", "--porcelain").strip()
    except GitError:
        return stop("failed", "best-of-n needs a git repo with a commit")
    if dirty:  # a clone starts from HEAD, so it would not see these changes
        return stop("failed", "best-of-n needs a clean tree; commit first")
    left, scope = run._limit(run.session.budget)
    if left is None:
        return stop("budget", f"{scope} budget exhausted")
    k = n + 1  # n candidates and the review share what is left
    share = Budget(
        turns=max(1, left.turns // k),
        tool_calls=max(1, left.tool_calls // k),
        tokens=max(1, left.tokens // k),
        cost_usd=None if left.cost_usd is None else left.cost_usd / k,
        wall_seconds=left.wall_seconds / k,
    )
    root = state_dir() / "work" / run.run_id / "candidates" / (run.node or "run")
    passed: list[tuple[str, Path, str, Result]] = []  # id, copy, branch, coder result
    dropped: list[str] = []
    for i in range(1, n + 1):
        if len(passed) >= want or run.session.cancel.is_set():
            break
        cid, dest = f"c{i}", root / f"c{i}"
        branch = f"timu/{run.run_id}/{cid}"
        run.emit("round", n=i, stage="candidate")
        try:
            git.provision(run.workdir, dest, base, branch)
        except GitError as e:
            return stop("failed", f"cannot copy the workdir for {cid}: {e}")
        cand = run.view(dest, share)
        if models:
            cand.models["coder"] = models[(i - 1) % len(models)]
        checker = Check(cand, check, check_paths, allow_check_edits, timeout)
        coded = cand.run_agent(
            coder, Task(objective, inputs, "The goal is met and the tests pass.")
        )
        if coded.status != "done":
            dropped.append(f"{cid} coder {coded.status}")
            continue
        status, summary, _log = checker(i)
        if status != "done":
            dropped.append(f"{cid}: {summary}")
            continue
        git.commit(dest, f"timu {cid}: {objective.splitlines()[0][:60]}")
        passed.append((cid, dest, branch, coded))
    if run.session.cancel.is_set():
        return stop("cancelled", "cancelled while running candidates")
    if not passed:
        return stop("failed", f"no candidate passed the check: {'; '.join(dropped)}")

    chosen = passed[0]
    if len(passed) > 1:
        ids = [p[0] for p in passed]
        diffs = tuple(
            Artifact(
                f"candidate-{cid}",
                git.diff(dest, base)[:MAX_LOG],
                "diff",
                Origin.AGENT,
                coded.trace_id,
                coded.untrusted,
            )
            for cid, dest, _, coded in passed
        )
        run.emit("round", n=0, stage="select")
        picked = run.run_agent(
            selector,
            Task(
                f"Pick the candidate that best meets this goal:\n\n{objective}",
                (*inputs, *diffs),
                check=protocol(partial(parse_choice, ids=ids)),
            ),
        )
        if picked.status != "done":
            return stop(picked.status, f"selector stopped: {picked.summary}")
        try:
            choice = parse_choice(picked.summary, ids)
        except ProtocolError as e:
            return stop("failed", f"the selection: {e}")
        chosen = next(p for p in passed if p[0] == choice)

    cid, dest, branch, coded = chosen
    try:
        git.take_branch(run.workdir, dest, branch, base)
    except (GitError, OSError) as e:
        return stop("failed", f"cannot apply {cid}: {e}")
    run.emit("round", n=int(cid[1:]), stage="chosen")
    result = fix_review(
        run,
        objective,
        check=check,
        timeout=timeout,
        check_paths=check_paths,
        allow_check_edits=allow_check_edits,
        skills=skills,
        inputs=inputs,
        first=coded,
        **fix,
    )
    summary = f"{cid} of {len(passed)} passing candidates: {result.summary}"
    return replace(result, summary=summary)


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
    run._reviewed = None  # commands may write build output after a review
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
        step_policy = replace(
            policy, write_roots=(*policy.write_roots, home), scratch=(home,)
        )
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
            if ran.created:
                end = refusal(ran, run.workdir)
                run.emit("warning", message=f"`{cmd}`: {end}")
            elif ran.stopped:
                end = ran.stopped
            elif ran.code is None:
                end = "not started"
            else:
                end = f"exit {ran.code}"
            run.emit("command_result", command=cmd, result=end)
            log.append(f"$ {cmd}\n{ran.text.rstrip()}\n[{end}]\n")
            if ran.code != 0 or ran.created:
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


# Files that implement a check, by a word in its command (after pma's accept.rs). A
# coder that edits one can make any tree pass, so a fix that does needs a human.
CHECK_FILES: Mapping[str, tuple[str, ...]] = {
    **dict.fromkeys(("make", "gmake"), ("Makefile", "GNUmakefile", "makefile", "*.mk")),
    "just": ("justfile", "Justfile", ".justfile"),
    "cargo": ("Cargo.toml", "build.rs"),
    "go": ("go.mod",),
    **dict.fromkeys(("npm", "yarn", "pnpm"), ("package.json",)),
    "nox": ("noxfile.py",),
    "tox": ("tox.ini",),
    "pytest": ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"),
}
CHECK_WORD = re.compile(r"[\w./-]+")
UNSCANNED = frozenset({"node_modules", "__pycache__", "site-packages"})  # and dot dirs


def check_files(commands: Sequence[str], workdir: Path) -> tuple[str, ...]:
    """Name patterns of the files that implement commands, and the workdir-relative
    paths of scripts they name, such as scripts/check.sh."""
    found: dict[str, None] = {}
    for word in (w for c in commands for w in CHECK_WORD.findall(c)):
        found.update(dict.fromkeys(CHECK_FILES.get(word.rsplit("/", 1)[-1], ())))
        path = Path(os.path.realpath(workdir / word))
        if path.is_relative_to(workdir) and path.is_file():
            found[str(path.relative_to(workdir))] = None
    return tuple(found)


def snapshot(workdir: Path, patterns: Sequence[str]) -> dict[str, str]:
    """The sha256 of each file under workdir whose name or relative path matches one
    of patterns, where "*" also matches "/". Dot directories and UNSCANNED are skipped."""
    hashes = {}
    for top, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in UNSCANNED]
        for name in files:
            path = Path(top, name)
            rel = str(path.relative_to(workdir))
            if any(
                fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(rel, p)
                for p in patterns
            ):
                try:
                    hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    hashes[rel] = ""
    return hashes


def _edited(
    workdir: Path, before: Mapping[str, str], patterns: Sequence[str]
) -> list[str]:
    """The files matching patterns whose hash differs from before."""
    after = snapshot(workdir, patterns)
    return sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
    )


class Check:
    """A fix's deterministic check: commands run offline after each coder run, and the
    files that implement them (check_files) or match paths, which a fix may not
    change unless allow_edits. The files are hashed when the Check is made."""

    def __init__(
        self,
        run: Run,
        commands: Sequence[str],
        paths: Sequence[str] = (),
        allow_edits: bool = False,
        timeout: float = 600,
    ) -> None:
        self.run, self.commands = run, commands
        self.allow_edits, self.timeout = allow_edits, timeout
        self.patterns = (*check_files(commands, run.workdir), *paths)
        self.guarded = snapshot(run.workdir, self.patterns) if commands else {}

    def __call__(self, n: int) -> tuple[Status, str, Artifact | None]:
        """Run the check after round n: status, summary and log. The log is None if a
        fix changed a guarded file; the check did not run then."""
        self.run.emit("round", n=n, stage="check")
        edited = _edited(self.run.workdir, self.guarded, self.patterns)
        if edited and not self.allow_edits:
            why = (
                f"round {n} changed {', '.join(edited)}, which the check runs; "
                "review the change, or set allow_check_edits = true"
            )
            return "failed", why, None
        return run_steps(self.run, self.commands, False, self.timeout)


def check_fix_review(
    run: Run,
    objective: str,
    *,
    check: Sequence[str],
    network: bool = False,
    timeout: float = 600,
    check_to_fix: int = 0,
    allow_check_edits: bool = False,
    check_paths: Sequence[str] = (),
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
    **fix: Any,
) -> Result:
    """Run check; if it fails, fix_review from its log with the same check (graph.md
    7.7). fix takes fix_review's other keyword arguments. network applies to the
    first run only: later runs execute code the coder wrote, so they run offline and
    reuse what the first run fetched."""
    status, summary, log = run_steps(run, check, network, timeout)
    if status != "failed":
        run.emit("workflow_result", status=status, summary=summary)
        tainted = any(a.tainted for a in (*inputs, log))  # log.tainted is network
        return Result(status, summary, (log,), run.used, run.run_id, tainted)
    fixed = fix_review(
        run,
        objective,
        check=check,
        timeout=timeout,
        check_to_fix=check_to_fix,
        allow_check_edits=allow_check_edits,
        check_paths=check_paths,
        skills=skills,
        inputs=inputs,
        failed_check=log,
        **fix,
    )
    return replace(fixed, artifacts=fixed.artifacts[::-1])  # log, then review


SEVERITY = ("critical", "high", "medium", "low")
FINDING_FIELDS = ("id", "severity", "location", "claim", "evidence")
VALIDATIONS = ("confirmed", "rejected")
VERIFICATIONS = ("fixed", "unfixed")
JSON_BLOCK = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)


class ProtocolError(ValueError):
    """An agent's reply lacks the json block its step needs, or the block is wrong."""


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    location: str
    claim: str
    evidence: str


def json_items(text: str, key: str, fields: Sequence[str]) -> list[dict[str, str]]:
    """The objects under key in the last json block of text, each with string fields.
    Raises ProtocolError."""
    blocks = JSON_BLOCK.findall(text)
    if not blocks:
        raise ProtocolError("no json block")
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid json: {e}") from None
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ProtocolError(f"no {key} list")
    for i in items:
        if not isinstance(i, dict) or not all(
            isinstance(i.get(f), str) for f in fields
        ):
            raise ProtocolError(
                f"each of {key} needs string fields {', '.join(fields)}"
            )
    return items


def parse_findings(text: str) -> tuple[Finding, ...]:
    """The findings in a review. Raises ProtocolError."""
    items = json_items(text, "findings", FINDING_FIELDS)
    findings = tuple(Finding(*(i[f] for f in FINDING_FIELDS)) for i in items)
    for f in findings:
        if f.severity not in SEVERITY:
            raise ProtocolError(
                f"{f.id}: severity must be one of {', '.join(SEVERITY)}"
            )
    if len({f.id for f in findings}) != len(findings):
        raise ProtocolError("finding ids are not unique")
    return findings


def parse_verdicts(
    text: str, ids: Sequence[str], statuses: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """id -> (status, evidence), one for each of ids. Raises ProtocolError."""
    verdicts: dict[str, tuple[str, str]] = {}
    for v in json_items(text, "verdicts", ("id", "status", "evidence")):
        if v["status"] not in statuses:
            raise ProtocolError(f"{v['id']}: status must be {' or '.join(statuses)}")
        if v["id"] in verdicts:
            raise ProtocolError(f"{v['id']} has two verdicts")
        verdicts[v["id"]] = (v["status"], v["evidence"])
    if set(verdicts) != set(ids):
        raise ProtocolError(f"need one verdict for each of {', '.join(ids)}")
    return verdicts


def protocol(parse: Callable[[str], object]) -> Callable[[str], str | None]:
    """A Task.check that names what parse rejects, so the agent can try again."""

    def check(text: str) -> str | None:
        try:
            parse(text)
        except ProtocolError as e:
            return f"{e}; end with the json block your instructions describe"
        return None

    return check


def render_findings(
    findings: Sequence[Finding], notes: Mapping[str, Mapping[str, tuple[str, str]]]
) -> str:
    """Each finding, then what each step found about it."""
    parts = []
    for f in findings:
        lines = [
            f"{f.id} [{f.severity}] {f.location}",
            f"Claim: {f.claim}",
            f"Evidence: {f.evidence}",
        ]
        for stage, (status, evidence) in notes.get(f.id, {}).items():
            lines.append(f"{stage.capitalize()}: {status}. {evidence}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def review_validate_fix(
    run: Run,
    objective: str,
    *,
    verify_to_fix: int = 0,
    report_path: str = "REVIEW.md",
    check: Sequence[str] = (),
    check_to_fix: int = 0,
    allow_check_edits: bool = False,
    check_paths: Sequence[str] = (),
    timeout: float = 600,
    skills: Mapping[str, tuple[str, ...]] | None = None,
    inputs: tuple[Artifact, ...] = (),
) -> Result:
    """Review, validate each finding, fix the confirmed ones, most severe first, then
    verify each fix. Unfixed findings go back to the coder at most verify_to_fix
    times. check, if given, runs as in fix_review, after each coder run and before
    the verifier; a failing log goes back to the coder at most check_to_fix times.
    The report lists every finding with each step's verdict. inputs go to the
    reviewer and the coder. Raises ReportError if report_path fails design 4.3."""
    file_root(run.workdir, report_path)
    if git.is_ignored(run.workdir, report_path) is False:
        run.emit("warning", message=f"{report_path} is not gitignored")
    skills = skills or {}
    finder, validator, coder, verifier = (
        with_skills(r, skills.get(r.name, ()))
        for r in (FINDER, VALIDATOR, CODER, VERIFIER)
    )
    checker = Check(run, check, check_paths, allow_check_edits, timeout)
    findings: tuple[Finding, ...] = ()
    notes: dict[str, dict[str, tuple[str, str]]] = {}
    results: list[Result] = []

    def tainted() -> bool:
        return any(a.tainted for a in inputs) or any(r.untrusted for r in results)

    def done(status: Status, summary: str) -> Result:
        report = f"# Review\n\n{summary}\n\n{render_findings(findings, notes)}\n"
        try:
            write_report(run.workdir, report_path, report)
        except ReportError as e:
            status, summary = "failed", f"cannot write the report: {e}"
        run.emit("workflow_result", status=status, summary=summary)
        art = Artifact("review", report, "report", Origin.AGENT, run.run_id, tainted())
        return Result(status, summary, (art,), run.used, run.run_id, tainted())

    def step(stage: str, role: Role, task: Task, n: int = 1) -> Result:
        run.emit("round", n=n, stage=stage)
        results.append(run.run_agent(role, task))
        return results[-1]

    def given(todo: Sequence[Finding], source: Result) -> Artifact:
        return Artifact(
            "findings",
            render_findings(todo, notes),
            "findings",
            Origin.AGENT,
            source.trace_id,
            tainted(),
        )

    reviewed = step(
        "review", finder, Task(objective, inputs, check=protocol(parse_findings))
    )
    if reviewed.status != "done":
        return done(reviewed.status, f"reviewer stopped: {reviewed.summary}")
    try:
        findings = parse_findings(reviewed.summary)
    except ProtocolError as e:
        return done("failed", f"the review: {e}")
    if not findings:
        return done("done", "no findings")

    ids = [f.id for f in findings]
    validated = step(
        "validate",
        validator,
        Task(
            f"Validate each finding of a review for this goal:\n\n{objective}",
            (given(findings, reviewed),),
            check=protocol(partial(parse_verdicts, ids=ids, statuses=VALIDATIONS)),
        ),
    )
    if validated.status != "done":
        return done(validated.status, f"validator stopped: {validated.summary}")
    try:
        verdicts = parse_verdicts(validated.summary, ids, VALIDATIONS)
    except ProtocolError as e:
        return done("failed", f"the validation: {e}")
    for fid, v in verdicts.items():
        notes[fid] = {"validation": v}
    todo = sorted(
        (f for f in findings if verdicts[f.id][0] == "confirmed"),
        key=lambda f: SEVERITY.index(f.severity),
    )
    confirmed = len(todo)
    if not todo:
        return done("done", f"no confirmed findings ({len(findings)} rejected)")

    rounds = verify_to_fix + 1
    verifies, checks_left = 0, check_to_fix
    log: Artifact | None = None  # a failing check's log, for the next coder run
    n = 0
    while verifies < rounds:
        n += 1
        goal = (
            f"{objective}\n\nFix each finding in the findings input, in the order "
            "given: most severe first. Say which you fixed and which you did not."
        )
        if log:
            goal += (
                "\n\nThe checks failed. Their output is the log input; make them pass."
            )
        coded = step(
            "fix",
            coder,
            Task(
                goal,
                (*inputs, given(todo, validated), *((log,) if log else ())),
                "Each finding is fixed and the tests pass.",
            ),
            n,
        )
        if coded.status != "done":
            return done(coded.status, f"coder stopped in round {n}: {coded.summary}")
        evidence: tuple[Artifact, ...] = ()
        if check:
            status, summary, ran = checker(n)
            if ran is None:
                return done(status, summary)
            if status == "failed" and checks_left:
                checks_left -= 1
                log = ran
                continue
            if status != "done":
                return done(status, f"check in round {n}: {summary}")
            log, evidence = None, (ran,)
        verifies += 1
        work = Artifact(
            "coder-summary",
            coded.summary,
            "text",
            Origin.AGENT,
            coded.trace_id,
            coded.untrusted,
        )
        todo_ids = [f.id for f in todo]
        run.mark_reviewed(report_path)
        verified = step(
            "verify",
            verifier,
            Task(
                "Check whether each finding in the findings input is fixed.",
                (given(todo, coded), work, *evidence),
                check=protocol(
                    partial(parse_verdicts, ids=todo_ids, statuses=VERIFICATIONS)
                ),
            ),
            n,
        )
        if verified.status != "done":
            return done(
                verified.status, f"verifier stopped in round {n}: {verified.summary}"
            )
        try:
            checks = parse_verdicts(verified.summary, todo_ids, VERIFICATIONS)
        except ProtocolError as e:
            return done("failed", f"the verification in round {n}: {e}")
        for fid, v in checks.items():
            notes[fid]["verification"] = v
        todo = [f for f in todo if checks[f.id][0] == "unfixed"]
        if not todo:
            return done("done", f"fixed {confirmed} confirmed findings in round {n}")
    return done(
        "failed",
        f"{len(todo)} of {confirmed} confirmed findings unfixed after {rounds} rounds",
    )


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
    required: tuple[str, ...] = ()  # params without a default
    # the most coder, tester and reviewer-fed fix runs for params; None: no bound
    runs: Callable[[Mapping[str, Any]], int] | None = None


def non_negative_int(v: Any) -> int:
    n = int(v) if isinstance(v, str) and v.isdigit() else v
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("must be a non-negative integer")
    return n


def positive_int(v: Any) -> int:
    n = int(v) if isinstance(v, str) and v.isdigit() else v
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError("must be a positive integer")
    return n


def report_file(v: Any) -> str:
    """The report path. A graph may not bind it to an output (graph.NO_REF): the file
    is left out of the commit, so an upstream coder could drop any file from it."""
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


def paths(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list | tuple) or not all(isinstance(p, str) and p for p in v):
        raise ValueError("must be a list of non-empty path globs")
    return tuple(v)


def names(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list | tuple) or not all(isinstance(x, str) and x for x in v):
        raise ValueError("must be a list of non-empty strings")
    return tuple(v)


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


def _runs(*edges: str, base: int | str = 1) -> Callable[[Mapping[str, Any]], int]:
    """Workflow.runs: base runs, or the param base names (default 3), plus one per
    loop edge. Raises TypeError if a value is not an int, such as a graph Ref."""

    def count(v: Any) -> int:
        if not isinstance(v, int) or isinstance(v, bool):
            raise TypeError(f"{v!r} is not a count")
        return v

    def runs(p: Mapping[str, Any]) -> int:
        first = count(p.get(base, 3)) if isinstance(base, str) else base
        return first + sum(count(p.get(e, 0)) for e in edges)

    return runs


FIX_PARAMS: Mapping[str, Param] = {
    "review_to_fix": non_negative_int,
    "report_path": report_file,
    "report_mode": report_mode,
}
# fix_review's own check; check-fix-review has its own check params
CHECK_PARAMS: Mapping[str, Param] = {
    "check": COMMANDS,
    "check_to_fix": non_negative_int,
    "allow_check_edits": boolean,
    "check_paths": paths,
    "coder_summary": boolean,
    "timeout": positive_int,
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
            {**FIX_PARAMS, **CHECK_PARAMS},
            ("review",),
            runs=_runs("check_to_fix", "review_to_fix"),
        ),
        Workflow(
            "reproduce-fix-review",
            "a tester adds failing tests, then fix-review makes them pass",
            ("tester", "coder", "reviewer"),
            False,
            lambda run, obj, o: reproduce_fix_review(
                run, obj, skills=o.skills, inputs=o.inputs, **o.params
            ),
            {
                **FIX_PARAMS,
                **CHECK_PARAMS,
                "test_paths": paths,
                "check_to_reproduce": non_negative_int,
            },
            ("review", "log"),
            required=("check",),
            runs=_runs("check_to_reproduce", "check_to_fix", "review_to_fix", base=2),
        ),
        Workflow(
            "best-of-n",
            "coders on separate copies; the check filters them, one is picked and "
            "reviewed",
            ("coder", "selector", "reviewer"),
            False,
            lambda run, obj, o: best_of_n(
                run, obj, skills=o.skills, inputs=o.inputs, **o.params
            ),
            {
                **FIX_PARAMS,
                **CHECK_PARAMS,
                "n": positive_int,
                "stop_after": positive_int,
                "models": names,
            },
            ("review", "log"),
            required=("check",),
            runs=_runs("check_to_fix", "review_to_fix", base="n"),
        ),
        Workflow(
            "research-fix-review",
            "a researcher looks things up on the web first",
            ("coder", "reviewer", "researcher"),
            True,  # web content reaches the coder in every run
            lambda run, obj, o: research_fix_review(
                run, obj, search=o.search, skills=o.skills, inputs=o.inputs, **o.params
            ),
            {**FIX_PARAMS, **CHECK_PARAMS},
            ("review", "research", "sources"),
            runs=_runs("check_to_fix", "review_to_fix"),
        ),
        Workflow(
            "review-validate-fix",
            "review, validate each finding, fix the confirmed ones, verify each fix",
            ("reviewer", "validator", "coder", "verifier"),
            False,
            lambda run, obj, o: review_validate_fix(
                run, obj, skills=o.skills, inputs=o.inputs, **o.params
            ),
            {
                "verify_to_fix": non_negative_int,
                "report_path": report_file,
                **{k: v for k, v in CHECK_PARAMS.items() if k != "coder_summary"},
            },
            ("review",),
            runs=_runs("check_to_fix", "verify_to_fix"),
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
            required=("steps",),
            runs=lambda p: 0,
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
                "check_to_fix": non_negative_int,
                "allow_check_edits": boolean,
                "check_paths": paths,
                "check": COMMANDS,
                "network": boolean,
                "timeout": positive_int,
            },
            ("log", "review"),
            graph_only=True,
            required=("check",),
            runs=_runs("check_to_fix", "review_to_fix"),
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
