"""Plan as a graph (docs/dev/workflow-proposals.md 2). A planner reads the named
projects and answers with graph nodes. The engine checks them, appends a final
check-fix-review per project, and renders the file with its [plan] table, which
makes `timu graph run` apply the planned-graph rules. Nothing runs here."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from timu.graph import (
    ID,
    PLANNED_NODE_KEYS,
    PLANNED_WORKFLOWS,
    Graph,
    GraphError,
    _ancestors,
    parse_graph,
    worst_case,
)
from timu.roles import PLANNER
from timu.types import Artifact, Origin, Status, Task
from timu.workflow import WORKFLOWS, Run

TOML_BLOCK = re.compile(r"```toml[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
FINAL = "final-"  # node ids the engine appends


@dataclass(frozen=True)
class Planned:
    status: Status
    summary: str
    graph: Graph | None  # parsed and checked; None unless done
    text: str | None  # the file, [plan] table included
    worst_case: int | None  # coder runs (workflow-proposals.md 2.4)
    errors: tuple[str, ...]  # why each rejected answer was rejected


def plan(
    run: Run,
    objective: str,
    repos: Mapping[str, Path],
    *,
    check: Sequence[str],
    inputs: Sequence[Artifact] = (),
    max_coder_runs: int = 20,
    max_nodes: int = 8,
    parse_to_plan: int = 1,
) -> Planned:
    """Ask a planner for a graph over repos (name -> path), which it may read. A
    rejected answer goes back to a new planner with the reason, at most parse_to_plan
    times. run's workdir is where relative paths in the planner's tools resolve."""
    role = replace(PLANNER, read_roots=tuple(str(p) for p in repos.values()))
    goal = _goal(objective, repos, check, max_coder_runs, max_nodes)
    errors: list[str] = []
    feedback: tuple[Artifact, ...] = ()
    for n in range(1, parse_to_plan + 2):
        run.emit("round", n=n, stage="plan")
        again = "\n\nYour last answer was rejected; the plan-error input says why."
        r = run.run_agent(
            role, Task(goal + (again if feedback else ""), (*inputs, *feedback))
        )
        if r.status != "done":
            return Planned(
                r.status,
                f"planner stopped: {r.summary}",
                None,
                None,
                None,
                tuple(errors),
            )
        untrusted = r.untrusted or any(a.tainted for a in inputs)
        try:
            graph, text = build(
                r.summary,
                objective=objective,
                repos=tuple(repos),
                check=tuple(check),
                untrusted=untrusted,
                run_id=run.run_id,
                max_coder_runs=max_coder_runs,
                max_nodes=max_nodes,
            )
        except GraphError as e:
            errors.append(str(e))
            feedback = (
                Artifact(
                    "plan-error",
                    f"{e}\n\nYour answer was:\n\n{r.summary}",
                    "log",
                    Origin.AGENT,
                    r.trace_id,
                    r.untrusted,
                ),
            )
            continue
        finals = sum(nid.startswith(FINAL) for nid in graph.nodes)
        runs = worst_case(graph)
        summary = (
            f"{len(graph.nodes) - finals} nodes + {finals} checks, "
            f"worst case {runs} coder runs"
        )
        return Planned("done", summary, graph, text, runs, tuple(errors))
    return Planned(
        "failed",
        f"no valid plan after {parse_to_plan + 1} answers: {errors[-1]}",
        None,
        None,
        None,
        tuple(errors),
    )


def build(
    answer: str,
    *,
    objective: str,
    repos: tuple[str, ...],
    check: tuple[str, ...],
    untrusted: bool,
    run_id: str,
    max_coder_runs: int,
    max_nodes: int,
) -> tuple[Graph, str]:
    """The checked graph and file text from a planner's answer. Raises GraphError,
    whose message goes back to the planner."""
    blocks = TOML_BLOCK.findall(answer)
    if not blocks:
        raise GraphError("the answer has no ```toml block")
    try:
        raw = tomllib.loads(blocks[-1])
    except tomllib.TOMLDecodeError as e:
        raise GraphError(f"invalid TOML: {e}") from None
    if extra := sorted(set(raw) - {"nodes"}):
        raise GraphError(f"the block may hold only [nodes.ID] tables, not {extra}")
    nodes = raw.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise GraphError("the block has no [nodes.ID] tables")
    if reserved := sorted(k for k in nodes if k.startswith(FINAL)):
        raise GraphError(f"node ids starting {FINAL} are the engine's: {reserved}")
    draft = parse_graph(raw, "plan")  # structure first; the [plan] rules come next
    ancestors = _ancestors(draft)
    finals: dict[str, dict[str, Any]] = {}
    for repo in dict.fromkeys(n.repo for n in draft.nodes.values()):
        on = [n.id for n in draft.nodes.values() if n.repo == repo]
        tips = [a for a in on if not any(a in ancestors[b] for b in on)]
        if len(tips) != 1:
            raise GraphError(
                f"nodes on {repo} must form one chain, each needing the last; "
                f"{', '.join(tips)} all end it"
            )
        fid = FINAL + re.sub(r"[^a-z0-9_-]", "-", repo.lower())
        while fid in nodes or fid in finals or not ID.fullmatch(fid):
            fid += "-x"
        finals[fid] = {
            "repo": repo,
            "needs": [tips[0]],
            "workflow": "check-fix-review",
            "objective": "The combined change on this project passes the checks",
            "params": {"check": list(check)},
        }
    header = {
        "by": "planner",
        "objective": objective,
        "repos": list(repos),
        "check": list(check),
        "untrusted": untrusted,
        "run_id": run_id,
        "max_coder_runs": max_coder_runs,
        "max_nodes": max_nodes,
    }
    text = render(header, {**nodes, **finals})
    return parse_graph(tomllib.loads(text), "plan"), text


def render(header: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]) -> str:
    """A graph file: header as [plan], then each node. Values are re-emitted from
    parsed TOML, so the file holds only what the checks saw. Raises GraphError."""
    lines = [
        "# Written by `timu graph plan`. While [plan] is here, `timu graph run` applies",
        "# the planned-graph rules. Delete [plan] to take the file over.",
        "",
        "[plan]",
        *(f"{_key(k)} = {_value(v)}" for k, v in header.items()),
    ]
    for nid, t in nodes.items():
        if not isinstance(t, dict):
            raise GraphError(f"nodes.{nid} must be a table")
        lines += ["", f"[nodes.{_key(nid)}]"]
        lines += [f"{_key(k)} = {_value(v)}" for k, v in t.items()]
    return "\n".join(lines) + "\n"


def _key(k: str) -> str:
    return k if BARE_KEY.fullmatch(k) else json.dumps(k)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # a JSON string is a TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(map(_value, v)) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise GraphError(f"cannot write a {type(v).__name__} value")


def _goal(
    objective: str,
    repos: Mapping[str, Path],
    check: Sequence[str],
    max_coder_runs: int,
    max_nodes: int,
) -> str:
    """The planner's task: the objective, the projects, and the node format, built
    from the workflow registry so it cannot drift from the parser."""
    projects = "\n".join(f"- {name}: {path}" for name, path in repos.items())
    workflows = []
    for name in PLANNED_WORKFLOWS:
        wf = WORKFLOWS[name]
        params = ", ".join(
            f"{k} (required)" if k in wf.required else k for k in wf.params
        )
        workflows.append(f"- {name}: {wf.help}. params: {params}")
    checks = json.dumps(list(check))
    return f"""Plan this objective as a graph of workflow nodes. Change nothing.

Objective:

{objective}

Projects you may read and plan for (name: path):

{projects}

Each node is a [nodes.ID] table. ID is lowercase letters, digits, - or _, and may
not start with {FINAL}. Allowed keys: {", ".join(sorted(PLANNED_NODE_KEYS))}.

- repo: one of the project names above.
- workflow: one of
{chr(10).join(workflows)}
- objective: what this node's coder must do. It must stand alone.
- needs: ids of nodes that must finish first. Nodes on one project form one chain:
  each needs the one before it on that project.
- params: an inline table of the workflow's params. Each *_to_* param allows that
  many extra rounds. A node's worst case is 1 coder run plus its *_to_* params
  (reproduce-fix-review: 2 plus). Command params such as check run offline.
- inputs: "ID.review" passes an upstream node's review to this node.

Limits: at most {max_nodes} nodes. All nodes together, plus one run for each
appended check, may make at most {max_coder_runs} coder runs.

After the last node on each project, timu appends a check-fix-review node that runs
{checks}. Do not add it yourself.

Example:

```toml
[nodes.lib-api]
repo = "lib"
workflow = "fix-review"
objective = "Rename parse() to load() in lib, keeping parse() as a deprecated alias"
params = {{ check = ["make test"], check_to_fix = 1 }}

[nodes.app-calls]
repo = "app"
needs = ["lib-api"]
workflow = "fix-review"
objective = "Call lib.load() instead of lib.parse() in app"
inputs = ["lib-api.review"]
```"""
