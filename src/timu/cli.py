"""The timu command line. `timu run` starts a workflow and streams it to the terminal.

Traces go to $XDG_STATE_HOME/timu/runs/<run id>.jsonl (default ~/.local/state/...),
outside the workspace, so the agents being traced cannot read or change them.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, TypeVar

from timu.config import Config, ConfigError, load_config
from timu.events import Event, JsonlSink
from timu.graph import GraphError, GraphResult, find_projects, load_graph, run_graph
from timu.provider.base import Provider
from timu.report import ReportError
from timu.role import Role, RoleError
from timu.sandbox import NoSandbox, Sandbox, detect, why_none
from timu.tool import Tool
from timu.trace import TraceError, load, render
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


class ConsoleSink:
    """Model text to out; one line per tool call, warning and result to err."""

    def __init__(self, out: TextIO, err: TextIO, verbose: bool = False) -> None:
        self.out, self.err, self.verbose = out, err, verbose
        self._mid_line = False

    def __call__(self, e: Event) -> None:
        d, who = e.data, f"[{e.role} {e.agent_id}]"
        if e.kind == "model_delta":
            self.out.write(d["text"])
            self.out.flush()
            self._mid_line = not d["text"].endswith("\n")
            return
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
        self.err.write(text + "\n")
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


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="timu", description="A team of specialised agents."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--approve-untrusted",
        action=argparse.BooleanOptionalAction,
        help="ask on the terminal before web content, or a goal written after reading "
        "it, reaches the coder. Default: on for lead, off otherwise. With no terminal, "
        "the answer is no",
    )
    common.add_argument(
        "--config",
        type=Path,
        help="default: ~/.config/timu/timu.toml. A workspace timu.toml is not read",
    )
    common.add_argument(
        "--max-cost", type=float, help="stop the run at this cost in USD"
    )
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
    run.add_argument("--max-rounds", type=int, default=3)
    graph = sub.add_parser("graph", help="run a graph of workflows across projects")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    graph_run = graph_sub.add_parser(
        "run", parents=[common], help="run every node of a graph file"
    )
    graph_run.add_argument("file", type=Path, help="the graph file (docs/dev/graph.md)")
    show = sub.add_parser("trace", help="show a run's agent tree, status and cost")
    show.add_argument(
        "run", nargs="?", help="a run id or .jsonl path; default: the latest run"
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
    if args.command == "graph":
        return run_graph_file(args, provider_for, out, err)
    workflow = WORKFLOWS[args.workflow]
    loaded = _load(args, workflow.roles, provider_for, err)
    if isinstance(loaded, int):
        return loaded
    config, provider_for = loaded
    if not args.workdir.is_dir():
        err.write(f"timu: {args.workdir} is not a directory\n")
        return USAGE_ERROR
    if args.max_rounds < 1:
        err.write("timu: --max-rounds must be at least 1\n")
        return USAGE_ERROR

    def body(session: Session) -> Result:
        run = session.at(args.workdir)
        search = _search(session, config, workflow.roles)
        params = {
            "max_rounds": args.max_rounds,
            "report_path": args.report,
            "report_mode": args.report_mode,
        }
        options = Options(config.role_skills, search, params)
        return workflow.run(run, args.objective, options)

    done = _execute(
        args, config, provider_for, workflow.approve_untrusted, out, err, body
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
    """`timu graph run FILE`: check the graph and find its projects, then run it."""
    try:
        graph = load_graph(args.file)
    except GraphError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    workflows = [WORKFLOWS[n.workflow] for n in graph.nodes.values()]
    roles = tuple(dict.fromkeys(r for w in workflows for r in w.roles))
    loaded = _load(args, roles, provider_for, err)
    if isinstance(loaded, int):
        return loaded
    config, provider_for = loaded
    try:
        projects = find_projects(graph, config.project_roots)
    except GraphError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR

    def body(session: Session) -> GraphResult:
        options = Options(config.role_skills, _search(session, config, roles))
        work = trace_dir().parent / "work" / session.run_id
        return run_graph(session, graph, projects, work, options)

    gate = any(w.approve_untrusted for w in workflows)
    done = _execute(args, config, provider_for, gate, out, err, body)
    if isinstance(done, int):
        return done
    result, trace = done
    for nid, r in result.nodes.items():
        err.write(f"timu: node {nid}: {r.status}: {r.summary}\n")
        if r.head != r.base:
            ref = f"{r.branch}:{r.branch}"
            err.write(f"timu:   git -C {projects[nid]} fetch {r.workdir} {ref}\n")
    err.write(f"timu: {result.status}; trace {trace}\n")
    return EXIT[result.status]


def _load(
    args: argparse.Namespace,
    roles: Sequence[str],
    provider_for: Callable[[Role], Provider] | None,
    err: TextIO,
) -> tuple[Config, Callable[[Role], Provider]] | int:
    """The config and a provider for each role, checked before the run so it does not
    fail halfway. A usage error code instead, after reporting it."""
    try:
        config = load_config(args.config)
        if provider_for is None:
            for name in roles:
                config.provider(name)
            provider_for = lambda role: config.provider(role.name)
    except ConfigError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    return config, provider_for


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
    provider_for: Callable[[Role], Provider],
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
    console = ConsoleSink(out, err, args.verbose)

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
                provider_for,
                sink,
                replace(WORKFLOW_BUDGET, cost_usd=args.max_cost),
                run_id=run_id,
                cancel=cancel,
                approve=tty_approve if gate else None,
                sandbox=sandbox,
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
    """Show web content and ask on the terminal whether role may receive it. No
    terminal means no. A terminal is not seekable, so it is opened unbuffered."""
    try:
        f = open(tty, "r+b", buffering=0)  # noqa: SIM115 - closed by the with below
    except OSError:
        return False
    lines = artifact.content.splitlines()
    more = f"\n[... {len(lines) - 40} more lines]" if len(lines) > 40 else ""
    shown = "\n".join(lines[:40]) + more
    with f:
        f.write(
            f"\n--- untrusted {artifact.name} from {artifact.source} ---\n{shown}\n---\n"
            f"Pass this to the {role.name}? [y/N] ".encode()
        )
        answer = f.readline().decode(errors="replace")
    return answer.strip().lower() in ("y", "yes")


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
        path = Path(run) if run.endswith(".jsonl") else runs / f"{run}.jsonl"
    try:
        out.write(render(load(path)) + "\n")
    except TraceError as e:
        err.write(f"timu: {e}\n")
        return USAGE_ERROR
    out.write(f"trace: {path}\n")
    return 0


def _report(result: Result, trace: Path, err: TextIO) -> None:
    u = result.usage
    cost = f", ${u.cost_usd:.4f}" if u.cost_usd else ""
    err.write(f"\ntimu: {result.status}: {result.summary}\n")
    err.write(
        f"timu: {u.turns} turns, {u.tool_calls} tool calls, {u.tokens} tokens{cost}\n"
    )
    err.write(f"timu: trace {trace}\n")


if __name__ == "__main__":
    sys.exit(main())
