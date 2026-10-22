"""The timu command line. `timu run` starts a workflow and streams it to the terminal.

Traces go to $XDG_STATE_HOME/timu/runs/<run id>.jsonl (default ~/.local/state/...),
outside the workspace, so the agents being traced cannot read or change them.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, TypeVar

from timu.config import Config, ConfigError, load_config
from timu.events import Event, JsonlSink
from timu.graph import (
    Graph,
    GraphError,
    GraphResult,
    NodeResult,
    find_projects,
    load_graph,
    resumable,
    run_graph,
    serial_order,
)
from timu.provider.base import Provider
from timu.report import ReportError
from timu.role import Role, RoleError
from timu.sandbox import NoSandbox, Sandbox, detect, why_none
from timu.tool import Tool
from timu.trace import BY, TraceError, load, render, totals
from timu.types import Artifact, Result
from timu.workflow import WORKFLOW_BUDGET, WORKFLOWS, Options, Session, new_run_id

EXIT = {
    "done": 0,
    "failed": 1,
    "refused": 1,
    "skipped": 1,
    "budget": 3,
    "cancelled": 130,
}
T = TypeVar("T")
USAGE_ERROR = 2
# C0 and C1 controls but newline and tab, and bidi overrides: on a terminal they can
# move the cursor, hide text or reorder it
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
# workflow param -> `timu run` argument; a workflow gets the ones it declares
RUN_PARAMS = {
    "review_to_fix": "review_to_fix",
    "verify_to_fix": "verify_to_fix",
    "report_path": "report",
    "report_mode": "report_mode",
}


def printable(text: str) -> str:
    """text with each CONTROL character written as its escape, such as \\x1b."""
    return CONTROL.sub(
        lambda m: m.group().encode("unicode_escape").decode(),
        text.replace("\r\n", "\n"),
    )


class ConsoleSink:
    """Model text to out; one line per tool call, warning and result to err. With
    parallel, nodes run at once, so model text goes out by whole lines and each line
    names its node."""

    def __init__(
        self, out: TextIO, err: TextIO, verbose: bool = False, parallel: bool = False
    ) -> None:
        self.out, self.err, self.verbose = out, err, verbose
        self.parallel = parallel
        self._mid_line = False
        self._partial: dict[str, str] = {}  # agent id -> model text after its last \n
        self._node = ""

    def __call__(self, e: Event) -> None:
        d, who = e.data, f"[{e.role} {e.agent_id}]"
        self._node = e.node if self.parallel else ""
        if e.kind == "model_delta" and self.parallel:
            *lines, self._partial[e.agent_id] = (
                self._partial.get(e.agent_id, "") + d["text"]
            ).split("\n")
            for line in lines:
                self.out.write(
                    printable(f"[{e.node}] {line}" if e.node else line) + "\n"
                )
            self.out.flush()
            return
        if e.kind == "model_delta":
            self.out.write(printable(d["text"]))
            self.out.flush()
            self._mid_line = not d["text"].endswith("\n")
            return
        if e.kind == "result" and (rest := self._partial.pop(e.agent_id, "")):
            self.out.write(printable(f"[{e.node}] {rest}" if e.node else rest) + "\n")
        if self._mid_line:
            self.out.write("\n")
            self.out.flush()
            self._mid_line = False
        if e.kind == "tool_call":
            self._line(
                f"{who} {d['name']} {_brief(d.get('arguments') or d.get('raw'))}"
            )
        elif e.kind == "tool_result" and (self.verbose or d["is_error"]):
            first = (d["text"].strip().splitlines() or [""])[0]
            self._line(f"    {'error: ' if d['is_error'] else ''}{first[:200]}")
        elif e.kind == "warning":
            self._line(f"warning: {d['message']}")
        elif e.kind == "config":
            self._line(f"config: {d['source']}")
        elif e.kind == "command":
            self._line(f"{who} $ {d['command']}")
        elif e.kind == "command_result":
            self._line(f"    {d['result']}")
        elif e.kind == "node":
            self._line(f"== node {e.node}: {d['repo']} ({d['workflow']})")
        elif e.kind == "node_result":
            self._line(f"== node {d['node']}: {d['status']}: {d['summary']}")
        elif e.kind == "round":
            extra = f": {d['verdict']}" if d.get("verdict") else ""
            self._line(f"== round {d['n']} {d['stage']}{extra}")
        elif e.kind == "result":
            u = d["usage"]
            tokens = u["input_tokens"] + u["output_tokens"]
            self._line(f"{who} {d['status']}: {u['turns']} turns, {tokens} tokens")

    def _line(self, text: str) -> None:
        if self._node and not text.startswith("== node"):
            text = f"[{self._node}] {text}"
        self.err.write(printable(text) + "\n")
        self.err.flush()


def _brief(args: Any, width: int = 100) -> str:
    """A tool's arguments on one line: the command or path if there is one."""
    if isinstance(args, dict) and isinstance(args.get("role"), str):
        text = f"{args['role']}: {args.get('goal', '')}"  # delegate
    elif isinstance(args, dict):
        for key in ("command", "path", "pattern", "name"):
            if isinstance(args.get(key), str):
                text = args[key]
                break
        else:
            text = json.dumps(args)
    else:
        text = str(args)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def trace_dir(env: dict[str, str] | os._Environ[str] = os.environ) -> Path:
    base = env.get("XDG_STATE_HOME") or str(
        Path(env.get("HOME", ".")) / ".local" / "state"
    )
    return Path(base) / "timu" / "runs"


def positive(v: str) -> int:
    if not v.isdigit() or int(v) < 1:
        raise argparse.ArgumentTypeError(f"{v!r} is not a positive integer")
    return int(v)


def usd(v: str) -> float:
    """A positive, finite amount; nan would disable the limit."""
    try:
        x = float(v)
    except ValueError:
        x = math.nan
    if not math.isfinite(x) or x <= 0:
        raise argparse.ArgumentTypeError(f"{v!r} is not a positive amount")
    return x


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="timu", description="A team of specialised agents."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--approve-untrusted",
        action=argparse.BooleanOptionalAction,
        help="ask on the terminal before web content, or a goal written after reading "
        "it, reaches the coder, and before a task the lead writes reaches the "
        "researcher. Default: on for lead, off otherwise. With no terminal, the answer "
        "is no",
    )
    common.add_argument(
        "--config",
        type=Path,
        help="default: ~/.config/timu/timu.toml. A workspace timu.toml is not read",
    )
    common.add_argument("--max-cost", type=usd, help="stop the run at this cost in USD")
    common.add_argument(
        "--unsafe-no-sandbox",
        action="store_true",
        help="run shell commands without a sandbox; only where none exists",
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", help="show every tool result"
    )
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", parents=[common], help="run a workflow on an objective")
    run.add_argument("objective", help="what the team should achieve")
    run.add_argument(
        "--workflow",
        choices=[w.name for w in WORKFLOWS.values() if not w.graph_only],
        default="fix-review",
        help="; ".join(
            f"{w.name}: {w.help}" for w in WORKFLOWS.values() if not w.graph_only
        ),
    )
    run.add_argument("-C", "--workdir", type=Path, default=Path("."), help="default: .")
    run.add_argument(
        "--report", default="REVIEW.md", help="report path (default: REVIEW.md)"
    )
    run.add_argument(
        "--report-mode",
        choices=["return", "write"],
        default="return",
        help="return: timu writes the report (default); write: the reviewer does",
    )
    run.add_argument(
        "--review-to-fix",
        type=int,
        default=0,
        help="how many times a CHANGES verdict goes back to the coder (default: 0)",
    )
    run.add_argument(
        "--verify-to-fix",
        type=int,
        default=0,
        help="review-validate-fix: how many times unfixed findings go back to the coder "
        "(default: 0)",
    )
    graph = sub.add_parser("graph", help="run a graph of workflows across projects")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    graph_run = graph_sub.add_parser(
        "run", parents=[common], help="run every node of a graph file"
    )
    graph_run.add_argument("file", type=Path, help="the graph file (docs/dev/graph.md)")
    graph_run.add_argument(
        "--max-parallel",
        type=positive,
        default=1,
        help="how many nodes may run at once (default: 1)",
    )
    graph_run.add_argument(
        "--resume",
        metavar="RUN",
        help="reuse the nodes an earlier run of this graph finished, where their "
        "definition is unchanged and their copy is intact",
    )
    graph_run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the nodes in order, their models and cost caps, and the ceiling; "
        "run nothing",
    )
    show = sub.add_parser("trace", help="show a run's agent tree, status and cost")
    show.add_argument(
        "run", nargs="?", help="a run id or .jsonl path; default: the latest run"
    )
    report = sub.add_parser(
        "report", help="totals across every trace: runs, done, cost, time"
    )
    report.add_argument(
        "--by",
        choices=BY,
        default="workflow",
        help="group workflow runs and graph nodes by workflow or project, or agents "
        "by model (default: workflow)",
    )
    return ap


def main(
    argv: Sequence[str] | None = None,
    provider_for: Callable[[Role], Provider] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Entry point. provider_for replaces the configured providers, for tests."""
    args = parser().parse_args(argv)
    if args.command == "trace":
        return show_trace(args.run, out, err)
    if args.command == "report":
        return show_report(args.by, out, err)
    if args.command == "graph":
        return run_graph_file(args, provider_for, out, err)
    workflow = WORKFLOWS[args.workflow]
    loaded = _load(args, workflow.roles, provider_for, err)
    if isinstance(loaded, int):
        return loaded
    config, provider_for, model_provider = loaded
    if not args.workdir.is_dir():
        err.write(f"timu: {args.workdir} is not a directory\n")
        return USAGE_ERROR
    defaults = parser().parse_args(["run", "x"])
    given = {}
    for param, attr in RUN_PARAMS.items():
        value, flag = getattr(args, attr), "--" + attr.replace("_", "-")
        if param not in workflow.params:
            if value != getattr(defaults, attr):
                err.write(f"timu: {flag} does not apply to {workflow.name}\n")
                return USAGE_ERROR
            continue
        try:
            given[param] = workflow.params[param](value)
        except ValueError as e:
            err.write(f"timu: {flag} {e}\n")
            return USAGE_ERROR

    def body(session: Session) -> Result:
        run = session.at(args.workdir)
        session.emit("run", workflow=workflow.name, workdir=str(run.workdir))
        search = _search(session, config, workflow.roles)
        options = Options(config.role_skills, search, given)
        return workflow.run(run, args.objective, options)

    done = _execute(
        args,
        config,
        (provider_for, model_provider),
        workflow.approve_untrusted,
        out,
        err,
        body,
    )
    if isinstance(done, int):
        return done
    result, trace = done
    _report(result, trace, err)
    return EXIT[result.status]


def run_graph_file(
    args: argparse.Namespace,
    provider_for: Callable[[Role], Provider] | None,
    out: TextIO,
    err: TextIO,
) -> int:
    """`timu graph run FILE`: check the graph and find its projects, then run it, or
    with --dry-run print its plan."""
    try:
        graph = load_graph(args.file)
    except GraphError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    workflows = [WORKFLOWS[n.workflow] for n in graph.nodes.values()]
    roles = tuple(dict.fromkeys(r for w in workflows for r in w.roles))
    if args.dry_run:
        try:
            config = load_config(args.config)
        except ConfigError as e:
            err.write(f"timu: {e}\n")
            return USAGE_ERROR
        model_provider = None
    else:
        loaded = _load(args, roles, provider_for, err)
        if isinstance(loaded, int):
            return loaded
        config, provider_for, model_provider = loaded
    try:
        projects = find_projects(graph, config.project_roots)
    except GraphError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    prior: dict[str, NodeResult] = {}
    if args.resume:
        try:
            prior = resumable(load(_trace_path(args.resume)), graph, projects)
        except TraceError as e:
            err.write(f"timu: {e}\n")
            return USAGE_ERROR
        err.write(
            f"timu: from {args.resume}, reusing {', '.join(prior) or 'no nodes'}\n"
        )
    if args.dry_run:
        out.write(
            plan(graph, projects, config, prior, args.max_cost, args.max_parallel)
        )
        return 0
    assert provider_for is not None

    def body(session: Session) -> GraphResult:
        options = Options(config.role_skills, _search(session, config, roles))
        work = trace_dir().parent / "work" / session.run_id
        if args.resume:
            session.emit("resume", run=args.resume, nodes=list(prior))
        return run_graph(
            session,
            graph,
            projects,
            work,
            options,
            max_parallel=args.max_parallel,
            prior=prior,
        )

    gate = any(w.approve_untrusted for w in workflows)
    done = _execute(args, config, (provider_for, model_provider), gate, out, err, body)
    if isinstance(done, int):
        return done
    result, trace = done
    for nid, r in result.nodes.items():
        reused = " (reused)" if r.reused else ""
        err.write(
            printable(f"timu: node {nid}: {r.status}{reused}: {r.summary}") + "\n"
        )
        if r.head != r.base:
            ref = f"{r.branch}:{r.branch}"
            err.write(f"timu:   git -C {projects[nid]} fetch {r.workdir} {ref}\n")
    err.write(f"timu: {result.status}; trace {trace}\n")
    return EXIT[result.status]


def plan(
    graph: Graph,
    projects: Mapping[str, Path],
    config: Config,
    prior: Mapping[str, NodeResult],
    max_cost: float | None,
    max_parallel: int,
) -> str:
    """What `graph run` would do: each node in the order a serial run starts them,
    its models and cost cap, and the most the run can spend."""
    lines = [f"{len(graph.nodes)} nodes, at most {max_parallel} at a time:"]
    uncapped, caps = [], 0.0
    for n, nid in enumerate(serial_order(graph), 1):
        node = graph.nodes[nid]
        wf = WORKFLOWS[node.workflow]
        needs = f" after {', '.join(node.needs)}" if node.needs else ""
        lines.append(f"{n:3}. {nid}: {node.workflow} on {projects[nid]}{needs}")
        if nid in prior:
            lines.append("       reused from the earlier run")
            continue
        if not wf.roles:
            lines.append("       commands only; no model")
            continue
        models = ", ".join(
            f"{r} {node.models.get(r) or config.model_for(r) or '(no model)'}"
            for r in wf.roles
        )
        lines.append(f"       {models}")
        cap = node.budget.cost_usd if node.budget else None
        lines.append(f"       cost cap {f'${cap:.2f}' if cap is not None else 'none'}")
        if cap is None:
            uncapped.append(nid)
        else:
            caps += cap
    if uncapped and max_cost is None:
        lines.append(
            f"ceiling: none; {', '.join(uncapped)} set no budget.cost_usd and "
            "--max-cost is not given"
        )
    else:
        ceiling = min(caps if not uncapped else math.inf, max_cost or math.inf)
        lines.append(f"ceiling: ${ceiling:.2f}, if the server reports cost")
    return "\n".join(lines) + "\n"


def _load(
    args: argparse.Namespace,
    roles: Sequence[str],
    provider_for: Callable[[Role], Provider] | None,
    err: TextIO,
) -> (
    tuple[
        Config,
        Callable[[Role], Provider],
        Callable[[Role, str], Provider] | None,
    ]
    | int
):
    """The config, a provider for each role, and one for a role at a model a graph
    node names, checked before the run so it does not fail halfway. A usage error
    code instead, after reporting it. A given provider_for serves every model."""
    model_provider = None
    try:
        config = load_config(args.config)
        if provider_for is None:
            for name in roles:
                config.provider(name)
            if "researcher" in roles:
                config.search_tool()  # a bad key file fails here, not mid-run
            provider_for = lambda role: config.provider(role.name)
            model_provider = lambda role, model: config.provider(role.name, model=model)
    except ConfigError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    return config, provider_for, model_provider


def _search(session: Session, config: Config, roles: Sequence[str]) -> Tool | None:
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


def _execute(
    args: argparse.Namespace,
    config: Config,
    providers: tuple[
        Callable[[Role], Provider], Callable[[Role, str], Provider] | None
    ],
    gate_default: bool,
    out: TextIO,
    err: TextIO,
    body: Callable[[Session], T],
) -> tuple[T, Path] | int:
    """Run body in a Session that writes a trace and streams to the console, with
    Ctrl-C handling. Returns body's result and the trace, or an exit code."""
    sandbox: Sandbox | None = None
    if args.unsafe_no_sandbox:
        sandbox = NoSandbox()
    elif (sandbox := detect(config.extra_read)) is None:
        err.write(f"timu: {why_none()}; roles that run commands will not start\n")
    gate = args.approve_untrusted
    if gate is None:
        gate = gate_default
    run_id = new_run_id()
    trace = trace_dir() / f"{run_id}.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    cancel = threading.Event()
    console = ConsoleSink(out, err, args.verbose, getattr(args, "max_parallel", 1) > 1)

    def on_sigint(sig: int, frame: FrameType | None) -> None:
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        err.write("\ntimu: stopping after the current step; Ctrl-C again to abort\n")

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        with JsonlSink(trace) as jsonl:

            def sink(e: Event) -> None:
                jsonl(e)
                console(e)

            session = Session(
                providers[0],
                sink,
                replace(WORKFLOW_BUDGET, cost_usd=args.max_cost),
                run_id=run_id,
                cancel=cancel,
                approve=tty_approve if gate else None,
                sandbox=sandbox,
                model_provider=providers[1],
            )
            session.emit("config", source=config.source)
            result = body(session)
    except (RoleError, ReportError) as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    except KeyboardInterrupt:
        err.write("timu: aborted\n")
        return EXIT["cancelled"]
    finally:
        signal.signal(signal.SIGINT, previous)
    return result, trace


def tty_approve(artifact: Artifact, role: Role, tty: str = "/dev/tty") -> bool:
    """Show web content, all of it and with control characters escaped, and ask on
    the terminal whether role may receive it. No terminal means no. A terminal is
    not seekable, so it is opened unbuffered."""
    try:
        f = open(tty, "r+b", buffering=0)  # noqa: SIM115 - closed by the with below
    except OSError:
        return False
    n = len(artifact.content.splitlines())
    head = f"untrusted {artifact.name} from {artifact.source}, {n} lines"
    with f:
        f.write(
            printable(
                f"\n--- {head} ---\n{artifact.content}\n--- end of {head} ---\n"
                f"Pass this to the {role.name}? [y/N] "
            ).encode()
        )
        answer = f.readline().decode(errors="replace")
    return answer.strip().lower() in ("y", "yes")


def _trace_path(run: str) -> Path:
    """run as a trace: a .jsonl path, or a run id in trace_dir."""
    return Path(run) if run.endswith(".jsonl") else trace_dir() / f"{run}.jsonl"


def show_trace(run: str | None, out: TextIO, err: TextIO) -> int:
    """Print the agent tree of run: a path, a run id, or the latest run."""
    runs = trace_dir()
    if run is None:
        found = sorted(runs.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not found:
            err.write(f"timu: no traces in {runs}\n")
            return USAGE_ERROR
        path = found[-1]
    else:
        path = _trace_path(run)
    try:
        out.write(render(load(path)) + "\n")
    except TraceError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    out.write(f"trace: {path}\n")
    return 0


def show_report(by: str, out: TextIO, err: TextIO) -> int:
    """Print totals across every trace in trace_dir, grouped by by."""
    runs = trace_dir()
    traces, unreadable = [], 0
    for path in sorted(runs.glob("*.jsonl")):
        try:
            traces.append(load(path))
        except TraceError:
            unreadable += 1
    if not traces:
        err.write(f"timu: no traces in {runs}\n")
        return USAGE_ERROR
    out.write(printable(totals(traces, by)) + "\n")
    if unreadable:
        err.write(f"timu: skipped {unreadable} unreadable traces in {runs}\n")
    return 0


def _report(result: Result, trace: Path, err: TextIO) -> None:
    u = result.usage
    cost = f", ${u.cost_usd:.4f}" if u.cost_usd else ""
    err.write(printable(f"\ntimu: {result.status}: {result.summary}") + "\n")
    err.write(
        f"timu: {u.turns} turns, {u.tool_calls} tool calls, {u.tokens} tokens{cost}\n"
    )
    err.write(f"timu: trace {trace}\n")


if __name__ == "__main__":
    sys.exit(main())
