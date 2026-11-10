"""Run workflows and graphs from Python: what `timu run` and `timu graph run` do,
without a terminal. Public API; the CLI is built on it.

    from timu import api

    out = api.run("make test fails; fix calc.py", Path("~/p/calc").expanduser())
    print(out.status, out.summary, out.branch)

Events stream to sink as they happen, and to the trace under
$XDG_STATE_HOME/timu/runs. Nothing prints. Untrusted content reaches a role only if
approve says yes (design 6); without approve, the answer is no. Setting cancel stops
the run after the current step. A run starts in the calling thread and returns when
it ends; a UI runs it in a worker thread and reads events from sink.

UsageError means nothing was spent: a bad config, workflow, param, workdir or graph.
After spending, a failure is an Outcome with status "failed".
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from timu.config import Config, ConfigError, load_config
from timu.events import Event, EventSink, JsonlSink
from timu.graph import (
    Graph,
    GraphError,
    GraphResult,
    NodeResult,
    _git,
    find_projects,
    load_graph,
    provision,
    resumable,
    seal,
)
from timu.graph import (
    run_graph as _run_graph,
)
from timu.provider.base import Provider
from timu.report import ReportError
from timu.role import Role, RoleError
from timu.sandbox import NoSandbox, Sandbox, detect, why_none
from timu.tool import Tool, state_dir
from timu.trace import TraceError, load
from timu.types import Artifact, Budget, Result, Usage
from timu.workflow import (
    WORKFLOW_BUDGET,
    WORKFLOWS,
    Options,
    Session,
    Workflow,
    new_run_id,
)

T = TypeVar("T")
Approve = Callable[[Artifact, Role], bool]
"""approve(artifact, role): may this untrusted artifact reach role?"""


@dataclass(frozen=True)
class ApprovalRequest:
    """One question: may artifact reach role? artifact.content is what the role
    would receive; show all of it."""

    id: int
    artifact: Artifact
    role: str


class Approvals:
    """An approve that a UI answers from its own thread. Pass the instance as
    approve; each call blocks the run's thread until answer() is called for it, the
    run's cancel event is set, or timeout seconds pass. The last two count as no.

        approvals = api.Approvals(cancel, on_request=lambda r: loop.call_soon_threadsafe(show, r))
        worker thread: api.run(..., approve=approvals, cancel=cancel)
        UI thread:     approvals.answer(request.id, True)

    on_request runs on the run's thread; it must return quickly. Questions come one
    at a time per run, but one instance may serve several runs.
    """

    def __init__(
        self,
        cancel: threading.Event | None = None,
        *,
        on_request: Callable[[ApprovalRequest], None] | None = None,
        timeout: float | None = None,
        poll: float = 0.05,
    ) -> None:
        self._cancel = cancel or threading.Event()
        self._on_request = on_request
        self._timeout = timeout
        self._poll = poll
        self._lock = threading.Lock()
        self._next = 0
        self._waiting: dict[int, tuple[ApprovalRequest, threading.Event]] = {}
        self._answers: dict[int, bool] = {}

    def __call__(self, artifact: Artifact, role: Role) -> bool:
        with self._lock:
            self._next += 1
            request = ApprovalRequest(self._next, artifact, role.name)
            answered = threading.Event()
            self._waiting[request.id] = (request, answered)
        try:
            if self._on_request is not None:
                self._on_request(request)
            waited = 0.0
            while not answered.wait(self._poll):
                waited += self._poll
                if self._cancel.is_set() or (
                    self._timeout is not None and waited >= self._timeout
                ):
                    break
            with self._lock:  # an answer that raced the timeout still counts
                self._waiting.pop(request.id, None)
                return self._answers.pop(request.id, False)
        finally:
            with self._lock:  # also when on_request raised
                self._waiting.pop(request.id, None)
                self._answers.pop(request.id, None)

    def pending(self) -> list[ApprovalRequest]:
        """The questions waiting for an answer, oldest first."""
        with self._lock:
            return [r for r, _ in sorted(self._waiting.values(), key=lambda w: w[0].id)]

    def answer(self, request_id: int, approved: bool) -> bool:
        """Answer request_id. False if it is no longer waiting: answered, timed out
        or cancelled."""
        with self._lock:
            waiting = self._waiting.get(request_id)
            if waiting is None or request_id in self._answers:
                return False
            self._answers[request_id] = bool(approved)
            waiting[1].set()
            return True


class UsageError(ValueError):
    """The run did not start, or stopped before spending anything."""


@dataclass(frozen=True)
class Outcome:
    """How a run ended. result is None when an error stopped it after spending."""

    status: str  # done, failed, refused, skipped, budget or cancelled
    summary: str
    usage: Usage
    run_id: str
    trace: Path
    result: Result | GraphResult | None = None
    repo: Path | None = None  # the git top level the copy was cloned from
    copy: Path | None = None  # the clone the run worked in; None with in_place
    branch: str | None = None  # the branch in copy holding the run's commit, if any
    report: Path | None = None  # the reviewer's report file, if one was written
    projects: Mapping[str, Path] | None = None  # a graph's node -> project


@dataclass(frozen=True)
class Providers:
    """A provider for each role, and one for a role at a model a graph node names."""

    for_role: Callable[[Role], Provider]
    at_model: Callable[[Role, str], Provider] | None = None


def trace_dir(env: Mapping[str, str] = os.environ) -> Path:
    return state_dir(env) / "runs"


def check_providers(config: Config, roles: tuple[str, ...]) -> Providers:
    """Providers from config, each built once now so a missing key or bad provider
    type fails before the run, not halfway. Raises UsageError."""
    try:
        for name in roles:
            config.provider(name)
        if "researcher" in roles:
            config.search_tool()  # a bad key file fails here, not mid-run
    except ConfigError as e:
        raise UsageError(str(e)) from None
    return Providers(
        lambda role: config.provider(role.name),
        lambda role, model: config.provider(role.name, model=model),
    )


def run(
    objective: str,
    workdir: Path,
    *,
    workflow: str = "fix-review",
    params: Mapping[str, Any] | None = None,
    config: Config | None = None,
    sink: EventSink | None = None,
    approve: Approve | None = None,
    gate: bool | None = None,
    in_place: bool = False,
    max_cost: float | None = None,
    cancel: threading.Event | None = None,
    providers: Providers | None = None,
    unsafe_no_sandbox: bool = False,
) -> Outcome:
    """Run workflow on objective in workdir. Without in_place, the run works on a
    clone of workdir's repo and commits there on Outcome.branch; workdir does not
    change. params are the workflow's params (WORKFLOWS[workflow].params). gate None
    means the workflow's default. config None reads the user's config. Raises
    UsageError."""
    wf = WORKFLOWS.get(workflow)
    if wf is None or wf.graph_only:
        names = ", ".join(n for n, w in WORKFLOWS.items() if not w.graph_only)
        raise UsageError(f"unknown workflow {workflow!r}; workflows: {names}")
    given: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if key not in wf.params:
            raise UsageError(f"{key} does not apply to {workflow}")
        try:
            given[key] = wf.params[key](value)
        except ValueError as e:
            raise UsageError(f"{key} {e}") from None
    config = config or _config()
    providers = providers or check_providers(config, wf.roles)
    if not workdir.is_dir():
        raise UsageError(f"{workdir} is not a directory")
    source = Path(os.path.realpath(workdir))
    top = source
    if not in_place:
        try:
            top = Path(
                os.path.realpath(_git(source, "rev-parse", "--show-toplevel").strip())
            )
        except GraphError:
            raise UsageError(f"{source} is not in a git repo; use --in-place") from None
    copied: dict[str, Any] = {}

    def body(session: Session) -> Result:
        search = _search(session, config, wf.roles)
        options = Options(config.role_skills, search, given)
        if in_place:
            r = session.at(source)
            session.emit("run", workflow=wf.name, workdir=str(r.workdir))
            return wf.run(r, objective, options)
        return _run_on_copy(session, objective, wf, options, source, top, copied)

    result, error, session, trace = _execute(
        config,
        providers,
        wf.approve_untrusted if gate is None else gate,
        approve,
        sink,
        cancel,
        max_cost,
        unsafe_no_sandbox,
        body,
    )
    if result is None:
        return _failed(error, session, trace, copied)
    return Outcome(
        result.status,
        result.summary,
        result.usage,
        session.run_id,
        trace,
        result,
        **copied,
    )


def run_graph(
    graph: Path | Graph,
    *,
    config: Config | None = None,
    sink: EventSink | None = None,
    approve: Approve | None = None,
    gate: bool | None = None,
    max_cost: float | None = None,
    max_parallel: int = 1,
    resume: Path | None = None,
    cancel: threading.Event | None = None,
    providers: Providers | None = None,
    unsafe_no_sandbox: bool = False,
) -> Outcome:
    """Run a graph file, or a parsed Graph. resume is an earlier run's trace whose
    finished nodes may be reused; a "resume" event names them, and says why each
    other node runs again. gate None means on if any node's workflow defaults to on.
    Raises UsageError."""
    try:
        g = load_graph(graph) if isinstance(graph, Path) else graph
    except GraphError as e:
        raise UsageError(str(e)) from None
    workflows = [WORKFLOWS[n.workflow] for n in g.nodes.values()]
    roles = tuple(dict.fromkeys(r for w in workflows for r in w.roles))
    config = config or _config()
    providers = providers or check_providers(config, roles)
    try:
        projects = find_projects(g, config.project_roots)
        prior: dict[str, NodeResult] = {}
        why_not: dict[str, str] = {}
        if resume is not None:
            prior = resumable(load(resume), g, projects, why_not)
    except (GraphError, TraceError) as e:
        raise UsageError(str(e)) from None

    def body(session: Session) -> GraphResult:
        options = Options(config.role_skills, _search(session, config, roles))
        if resume is not None:
            session.emit("resume", run=resume.stem, nodes=list(prior), again=why_not)
        return _run_graph(
            session,
            g,
            projects,
            state_dir() / "work" / session.run_id,
            options,
            max_parallel=max_parallel,
            prior=prior,
        )

    if gate is None:
        gate = any(w.approve_untrusted for w in workflows)
    result, error, session, trace = _execute(
        config,
        providers,
        gate,
        approve,
        sink,
        cancel,
        max_cost,
        unsafe_no_sandbox,
        body,
        g.budget,
    )
    if result is None:
        return _failed(error, session, trace, {"projects": projects})
    summary = ", ".join(f"{nid} {r.status}" for nid, r in result.nodes.items())
    return Outcome(
        result.status,
        summary,
        session.used,
        session.run_id,
        trace,
        result,
        projects=projects,
    )


def _config() -> Config:
    try:
        return load_config()
    except ConfigError as e:
        raise UsageError(str(e)) from None


def _failed(
    error: str, session: Session, trace: Path, fields: dict[str, Any]
) -> Outcome:
    return Outcome("failed", error, session.used, session.run_id, trace, **fields)


def _search(session: Session, config: Config, roles: tuple[str, ...]) -> Tool | None:
    """The search tool if a researcher may run; warns when its key is missing."""
    if "researcher" not in roles:
        return None
    search = config.search_tool()
    if search is None:
        session.emit(
            "warning",
            message=f"{config.search_key_env} is not set; "
            "the researcher can fetch URLs but not search",
        )
    return search


def _run_on_copy(
    session: Session,
    objective: str,
    workflow: Workflow,
    options: Options,
    source: Path,
    top: Path,
    copied: dict[str, Any],
) -> Result:
    """Run workflow on a clone of top, and commit its changes on a branch there. The
    checkout stays unchanged, so nothing an agent plants in the copy runs under the
    user's git unless the user goes there. Fills copied with the Outcome fields.
    Raises GraphError."""
    rel = source.relative_to(top)
    copy = state_dir() / "work" / session.run_id / "run"
    branch = f"timu/{session.run_id}/run"
    try:
        base, dirty = provision(top, copy, "HEAD", branch)
    except GraphError as e:
        raise GraphError(f"{e}; commit first, or use --in-place") from None
    if not (copy / rel).is_dir():
        raise GraphError(f"{rel} is not in HEAD; commit it, or use --in-place")
    copied.update(repo=top, copy=copy)
    r = session.at(copy / rel)
    session.emit("run", workflow=workflow.name, workdir=str(source))
    if dirty:
        session.emit(
            "warning",
            message=f"{top} has uncommitted changes; the copy starts from HEAD",
        )
    result = workflow.run(r, objective, options)
    report = (  # the workflow's default when no param names it, as in graph nodes
        options.params.get("report_path", "REVIEW.md")
        if "report_path" in workflow.params
        else None
    )
    exclude = (os.path.normpath(rel / report),) if report else ()
    message = f"timu: {objective.splitlines()[0][:60]}\n\n"
    message += (
        f"{workflow.name} {result.status}: {result.summary}\n\nrun {session.run_id}"
    )
    try:
        new_head = seal(r, copy, base, branch, message, exclude, result.status)
    except GraphError as e:
        session.emit("workflow_result", status="failed", summary=str(e))
        result = replace(result, status="failed", summary=str(e))
        new_head = base
    if report and (copy / rel / report).is_file():
        copied["report"] = copy / rel / report
    if new_head != base:
        copied["branch"] = branch
    return result


def _execute(
    config: Config,
    providers: Providers,
    gate: bool,
    approve: Approve | None,
    sink: EventSink | None,
    cancel: threading.Event | None,
    max_cost: float | None,
    unsafe_no_sandbox: bool,
    body: Callable[[Session], T],
    budget: Budget | None = None,
) -> tuple[T | None, str, Session, Path]:
    """Run body in a Session that writes a trace and streams to sink. Returns body's
    result, or None and the error after an error that spent something, with the
    session and the trace. Raises UsageError for an error before spending."""
    run_id = new_run_id()
    trace = trace_dir() / f"{run_id}.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.parent.parent.chmod(0o700)  # traces and copies hold workspace contents
    sandbox: Sandbox | None = (
        NoSandbox() if unsafe_no_sandbox else detect(config.extra_read)
    )
    with JsonlSink(trace) as jsonl:

        def both(e: Event) -> None:
            jsonl(e)
            if sink is not None:
                sink(e)

        session = Session(
            providers.for_role,
            both,
            _session_budget(budget, max_cost),
            run_id=run_id,
            cancel=cancel,
            approve=(approve or _deny) if gate else None,
            sandbox=sandbox,
            model_provider=providers.at_model,
        )
        session.emit("config", source=config.source)
        if sandbox is None:
            session.emit(
                "warning",
                message=f"{why_none()}; roles that run commands will not start",
            )
        try:
            return body(session), "", session, trace
        except (RoleError, ReportError, GraphError) as e:
            if not (session.used.turns or session.used.tool_calls):
                raise UsageError(str(e)) from None
            return None, str(e), session, trace


def _deny(artifact: Artifact, role: Role) -> bool:
    return False


def _session_budget(budget: Budget | None, max_cost: float | None) -> Budget:
    """budget or the default; max_cost, if given, wins over its cost."""
    total = budget or WORKFLOW_BUDGET
    return total if max_cost is None else replace(total, cost_usd=max_cost)
