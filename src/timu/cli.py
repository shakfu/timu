"""The timu command line. `timu run` starts a workflow and streams it to the terminal.

Traces go to $XDG_STATE_HOME/timu/runs/<run id>.jsonl (default ~/.local/state/...),
outside the workspace, so the agents being traced cannot read or change them.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, TypeVar

from timu import api
from timu.api import trace_dir
from timu.config import Config, ConfigError, load_config
from timu.events import Event, EventSink
from timu.graph import (
    Graph,
    GraphError,
    GraphResult,
    NodeResult,
    find_projects,
    load_graph,
    resumable,
    serial_order,
)
from timu.provider.base import Provider
from timu.role import Role
from timu.trace import BY, TraceError, cache_note, load, render, totals
from timu.types import Artifact, Usage
from timu.workflow import (
    WORKFLOW_BUDGET,
    WORKFLOWS,
)

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
# move the cursor, hide text or reorder it. Lone surrogates cannot be encoded at all.
CONTROL = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]"
)
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
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
        elif e.kind == "resume":
            self.err.writelines(
                printable(line)
                for line in resume_lines(d["run"], d["nodes"], d.get("again", {}))
            )
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
        "--in-place",
        action="store_true",
        help="work in the checkout itself, not a copy; agents can then plant code that "
        "your git runs (see README)",
    )
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
    clean = sub.add_parser("clean", help="list or delete old copies and traces")
    clean.add_argument(
        "--older-than",
        type=positive,
        required=True,
        metavar="DAYS",
        help="runs last changed more than this many days ago",
    )
    clean.add_argument(
        "--yes",
        action="store_true",
        help="delete them, including branches not yet fetched; default: only list them",
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
    for stream in (out, err):  # model text may not fit the terminal's encoding
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    if args.command == "clean":
        return clean_state(args.older_than, args.yes, out)
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
    config, providers = loaded
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
    outcome = _interactive(
        args,
        out,
        err,
        lambda cancel, sink, approve: api.run(
            args.objective,
            args.workdir,
            workflow=workflow.name,
            params=given,
            config=config,
            sink=sink,
            approve=approve,
            gate=args.approve_untrusted,
            in_place=args.in_place,
            max_cost=args.max_cost,
            cancel=cancel,
            providers=providers,
            unsafe_no_sandbox=args.unsafe_no_sandbox,
        ),
    )
    if isinstance(outcome, int):
        return outcome
    if outcome.report:
        err.write(f"timu: report {outcome.report}\n")
    if outcome.branch:
        ref = f"{outcome.branch}:{outcome.branch}"
        err.write(f"timu: git -C {outcome.repo} fetch {outcome.copy} {ref}\n")
    err.write(printable(f"\ntimu: {outcome.status}: {outcome.summary}") + "\n")
    _spent(outcome.usage, outcome.trace, err)
    return EXIT[outcome.status]


def _interactive(
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    call: Callable[[threading.Event, EventSink, api.Approve], api.Outcome],
) -> api.Outcome | int:
    """call with a cancel event that the first Ctrl-C sets, a console sink, and a
    terminal prompt for approvals. While the prompt waits, console output pauses, so
    other nodes' lines do not break into it. Returns the outcome, or an exit code
    after reporting a usage error, an error that stopped the run, or an abort."""
    cancel = threading.Event()
    console = ConsoleSink(out, err, args.verbose, getattr(args, "max_parallel", 1) > 1)
    quiet = threading.Lock()

    def sink(e: Event) -> None:
        with quiet:
            console(e)

    def approve(artifact: Artifact, role: Role) -> bool:
        with quiet:
            return tty_approve(artifact, role)

    def on_sigint(sig: int, frame: FrameType | None) -> None:
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        err.write("\ntimu: stopping after the current step; Ctrl-C again to abort\n")

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        outcome = call(cancel, sink, approve)
    except api.UsageError as e:
        err.write(printable(f"timu: {e}") + "\n")
        return USAGE_ERROR
    except KeyboardInterrupt:
        err.write("timu: aborted\n")
        return EXIT["cancelled"]
    finally:
        signal.signal(signal.SIGINT, previous)
    if outcome.result is None:  # an error stopped the run after it spent
        err.write(printable(f"timu: {outcome.summary}") + "\n")
        _spent(outcome.usage, outcome.trace, err)
        return EXIT["failed"]
    return outcome


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
    resume = _trace_path(args.resume) if args.resume else None
    if args.dry_run:
        return _dry_run(args, graph, resume, out, err)
    loaded = _load(args, roles, provider_for, err)
    if isinstance(loaded, int):
        return loaded
    config, providers = loaded
    outcome = _interactive(
        args,
        out,
        err,
        lambda cancel, sink, approve: api.run_graph(
            graph,
            config=config,
            sink=sink,
            approve=approve,
            gate=args.approve_untrusted,
            max_cost=args.max_cost,
            max_parallel=args.max_parallel,
            resume=resume,
            cancel=cancel,
            providers=providers,
            unsafe_no_sandbox=args.unsafe_no_sandbox,
        ),
    )
    if isinstance(outcome, int):
        return outcome
    assert isinstance(outcome.result, GraphResult) and outcome.projects is not None
    for nid, r in outcome.result.nodes.items():
        reused = " (reused)" if r.reused else ""
        err.write(
            printable(f"timu: node {nid}: {r.status}{reused}: {r.summary}") + "\n"
        )
        if r.head != r.base:
            ref = f"{r.branch}:{r.branch}"
            err.write(
                f"timu:   git -C {outcome.projects[nid]} fetch {r.workdir} {ref}\n"
            )
    err.write(f"timu: {outcome.status}; trace {outcome.trace}\n")
    return EXIT[outcome.status]


def _dry_run(
    args: argparse.Namespace,
    graph: Graph,
    resume: Path | None,
    out: TextIO,
    err: TextIO,
) -> int:
    """`timu graph run --dry-run`: print the plan without building providers."""
    try:
        config = load_config(args.config)
        projects = find_projects(graph, config.project_roots)
        prior: dict[str, NodeResult] = {}
        why_not: dict[str, str] = {}
        if resume is not None:
            prior = resumable(load(resume), graph, projects, why_not)
    except (ConfigError, GraphError, TraceError) as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    if resume is not None:
        err.writelines(resume_lines(resume.stem, list(prior), why_not))
    out.write(plan(graph, projects, config, prior, args.max_cost, args.max_parallel))
    return 0


def resume_lines(run: str, nodes: Sequence[str], again: Mapping[str, str]) -> list[str]:
    """What a resume reuses, and why each other node runs again."""
    lines = [f"timu: from {run}, reusing {', '.join(nodes) or 'no nodes'}\n"]
    lines += [f"timu: running {nid} again: {why}\n" for nid, why in again.items()]
    return lines


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
    total = graph.budget or WORKFLOW_BUDGET
    lines.append(
        f"graph limits: {total.turns} turns, {total.tool_calls} tool calls, "
        f"{total.tokens} tokens, {total.wall_seconds:g} s"
    )
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
    roles: tuple[str, ...],
    provider_for: Callable[[Role], Provider] | None,
    err: TextIO,
) -> tuple[Config, api.Providers] | int:
    """The config and checked providers, or a usage error code after reporting it.
    A given provider_for serves every role and model."""
    try:
        config = load_config(args.config)
        if provider_for is not None:
            return config, api.Providers(provider_for)
        return config, api.check_providers(config, roles)
    except (ConfigError, api.UsageError) as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR


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
        out.write(printable(render(load(path))) + "\n")
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


def clean_state(days: int, delete: bool, out: TextIO) -> int:
    """List, or with delete remove, the copies, traces and quarantined entries last
    changed more than days ago. Only entries named like a run are touched."""
    cutoff = time.time() - days * 86400
    state = trace_dir().parent
    found = [
        p
        for p in (
            *sorted((state / "work").glob("*")),
            *sorted((state / "runs").glob("*.jsonl")),
            *sorted((state / "quarantine").glob("*")),
        )
        if RUN_ID.fullmatch(p.name.removesuffix(".jsonl"))
        and p.lstat().st_mtime < cutoff
    ]
    for p in found:
        out.write(f"{'removed' if delete else 'would remove'} {p}\n")
        if not delete:
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    if found and not delete:
        out.write("copies may hold branches not yet fetched; pass --yes to delete\n")
    return 0


def _spent(u: Usage, trace: Path, err: TextIO) -> None:
    cost = f", ${u.cost_usd:.4f}" if u.cost_usd else ""
    err.write(
        f"timu: {u.turns} turns, {u.tool_calls} tool calls, "
        f"{u.tokens} tokens{cache_note(u)}{cost}\n"
    )
    err.write(f"timu: trace {trace}\n")


if __name__ == "__main__":
    sys.exit(main())
