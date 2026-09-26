"""Workflow graphs (docs/dev/graph.md): a DAG of workflow instances in one Session,
each on its own copy of one local project. Nodes run one at a time, in file order
among those whose needs are done. The engine commits each node's changes on a branch
in its copy, never in the user's repo; agents may not commit (graph.md 7.2.1).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from functools import partial
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from timu.report import ReportError
from timu.role import RoleError
from timu.types import Artifact, Budget, Origin
from timu.workflow import WORKFLOW_BUDGET, WORKFLOWS, Commands, Options, Session

ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
REPO = re.compile(r"(?:([A-Za-z0-9][A-Za-z0-9-]*)/)?([A-Za-z0-9_][A-Za-z0-9_.-]*)")
REF = re.compile(r"\$\{([^.}]+)\.([^}]+)\}")
TOKEN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+!~-]*"
)  # a version or name; no shell syntax
GITHUB = re.compile(
    r"(?:https://|ssh://git@|git@)github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?"
)
NODE_KEYS = frozenset(
    {"repo", "workflow", "objective", "needs", "ref", "params", "inputs", "outputs"}
    | {"budget"}
)
SEVERITY = ("done", "skipped", "refused", "failed", "budget", "cancelled")
MAX_OUTPUT = 256 * 1024  # bytes, for one extracted output
COMMITTER = (  # the user's identity, signing and hooks play no part in a copy
    *("-c", "user.name=timu", "-c", "user.email=timu@localhost"),
    *("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"),
)
GIT_TIMEOUT = 120  # seconds


class GraphError(ValueError):
    """An invalid graph, a missing project, or an output that cannot be extracted."""


@dataclass(frozen=True)
class Ref:
    """A param bound to an upstream node's declared output: ${node.output}."""

    node: str
    output: str


@dataclass(frozen=True)
class Template:
    """Commands with ${node.output} in them, bound when the node runs (graph.md 7.7).
    ${NAME} without a dot is shell syntax and is left alone."""

    commands: tuple[str, ...]


@dataclass(frozen=True)
class Node:
    id: str
    repo: str
    workflow: str
    objective: str
    needs: tuple[str, ...] = ()
    ref: str = "HEAD"
    params: Mapping[str, Any] = field(default_factory=dict)  # converted, Ref, Template
    inputs: tuple[tuple[str, str], ...] = ()  # (node, output)
    outputs: Mapping[str, str] = field(default_factory=dict)  # name -> extractor
    budget: Budget | None = None


@dataclass(frozen=True)
class Graph:
    nodes: Mapping[str, Node]  # in file order


@dataclass(frozen=True)
class NodeResult:
    status: str  # a Status, or "skipped"
    summary: str
    outputs: Mapping[str, Artifact] = field(default_factory=dict)
    workdir: Path | None = None  # the node's copy, if it was made
    branch: str = ""
    base: str = ""  # the commit the node started from
    head: str = ""  # the branch's last commit; == base if nothing was committed


@dataclass(frozen=True)
class GraphResult:
    status: str  # the most severe node status
    nodes: Mapping[str, NodeResult]


# ---- the graph file ----


def load_graph(path: Path) -> Graph:
    """Read and check a graph file. Raises GraphError."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise GraphError(f"{path}: {e}") from None
    return parse_graph(raw, str(path))


def parse_graph(raw: Mapping[str, Any], src: str = "graph") -> Graph:
    """Check raw against graph.md section 4. Raises GraphError naming the key."""
    _only(raw, {"nodes", "defaults"}, src)
    defaults = _table(raw, "defaults", src)
    _only(defaults, {"budget"}, f"{src}: defaults")
    default_budget = _budget(defaults, f"{src}: defaults")
    tables = _table(raw, "nodes", src)
    if not tables:
        raise GraphError(f"{src}: no nodes")
    for nid, t in tables.items():
        if not ID.fullmatch(nid):
            raise GraphError(
                f"{src}: node id {nid!r} must be lowercase letters, digits, - or _"
            )
        if not isinstance(t, dict):
            raise GraphError(f"{src}: nodes.{nid} must be a table")
        _only(t, NODE_KEYS, f"{src}: nodes.{nid}")
    nodes = {
        nid: _node(nid, t, tables, default_budget, f"{src}: nodes.{nid}")
        for nid, t in tables.items()
    }
    try:
        TopologicalSorter({n.id: n.needs for n in nodes.values()}).prepare()
    except CycleError as e:
        raise GraphError(f"{src}: cycle: {' -> '.join(e.args[1])}") from None
    return Graph(nodes)


def _node(
    nid: str,
    t: Mapping[str, Any],
    tables: Mapping[str, Any],
    default_budget: Budget | None,
    where: str,
) -> Node:
    repo = _str(t, "repo", where)
    if not REPO.fullmatch(repo):
        raise GraphError(f"{where}: repo {repo!r} must be a name or owner/name")
    wf_name = _str(t, "workflow", where)
    if wf_name not in WORKFLOWS:
        raise GraphError(
            f"{where}: workflow {wf_name!r} is not one of {', '.join(WORKFLOWS)}"
        )
    wf = WORKFLOWS[wf_name]
    objective = _str(t, "objective", where)
    if "${" in objective:
        raise GraphError(f"{where}: ${{...}} is allowed only in params (graph.md 4.1)")
    ref = _str(t, "ref", where, "HEAD")
    if ref.startswith("-"):
        raise GraphError(f"{where}: ref must not start with -")
    needs = _strs(t, "needs", where)
    for d in needs:
        if d not in tables or d == nid:
            raise GraphError(f"{where}: needs {d!r}, which is not another node")

    def declared(d: str) -> Mapping[str, Any]:
        return _table(tables[d], "outputs", f"nodes.{d}")

    outputs = declared(nid)
    for name, spec in outputs.items():
        if not isinstance(spec, str):
            raise GraphError(f"{where}: outputs.{name} must be a string")
        if name in wf.artifacts:
            raise GraphError(f"{where}: outputs.{name} hides the {wf_name} result")
        _check_spec(spec, f"{where}: outputs.{name}")

    inputs = []
    for s in _strs(t, "inputs", where):
        d, _, out = s.partition(".")
        if d not in needs:
            raise GraphError(f"{where}: input {s!r} must name a node in needs")
        upstream = WORKFLOWS[_str(tables[d], "workflow", f"nodes.{d}")]
        if out not in declared(d) and out not in upstream.artifacts:
            raise GraphError(f"{where}: node {d} has no output {out!r}")
        inputs.append((d, out))

    params: dict[str, Any] = {}
    for k, v in _table(t, "params", where).items():
        if k not in wf.params:
            known = ", ".join(wf.params) or "none"
            raise GraphError(f"{where}: {wf_name} has no param {k!r}; params: {known}")
        if isinstance(wf.params[k], Commands):
            try:
                cmds = wf.params[k](v)
            except ValueError as e:
                raise GraphError(f"{where}: params.{k} {e}") from None
            for d, out in (m.groups() for c in cmds for m in REF.finditer(c)):
                if d not in needs or out not in declared(d):
                    raise GraphError(
                        f"{where}: params.{k}: ${{{d}.{out}}} must name a declared "
                        "output of a node in needs"
                    )
            params[k] = Template(cmds)
            continue
        if isinstance(v, str) and "${" in v:
            ref_m = REF.fullmatch(v)
            if not ref_m:
                raise GraphError(
                    f"{where}: params.{k}: ${{...}} must be the whole value"
                )
            d, out = ref_m.groups()
            if d not in needs or out not in declared(d):
                raise GraphError(
                    f"{where}: params.{k}: {v} must name a declared output of a node "
                    "in needs"
                )
            params[k] = Ref(d, out)
            continue
        try:
            params[k] = wf.params[k](v)
        except ValueError as e:
            raise GraphError(f"{where}: params.{k} {e}") from None

    budget = _budget(t, where) or default_budget
    return Node(
        nid,
        repo,
        wf_name,
        objective,
        needs,
        ref,
        params,
        tuple(inputs),
        outputs,
        budget,
    )


def _budget(t: Mapping[str, Any], where: str) -> Budget | None:
    """t's budget table over WORKFLOW_BUDGET, or None if t has none."""
    if "budget" not in t:
        return None
    b = _table(t, "budget", where)
    kinds = {f.name: f.type for f in fields(Budget)}
    _only(b, set(kinds), f"{where}: budget")
    for k, v in b.items():
        whole = kinds[k] == "int"
        if (
            isinstance(v, bool)
            or not isinstance(v, int if whole else int | float)
            or v <= 0
        ):
            kind = "positive integer" if whole else "positive number"
            raise GraphError(f"{where}: budget.{k} must be a {kind}")
    return replace(WORKFLOW_BUDGET, **b)


def _only(t: Mapping[str, Any], allowed: set[str] | frozenset[str], where: str) -> None:
    if extra := sorted(set(t) - allowed):
        raise GraphError(f"{where}: unknown keys: {', '.join(extra)}")


def _table(t: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    v = t.get(key, {})
    if not isinstance(v, dict):
        raise GraphError(f"{where}: {key} must be a table")
    return v


def _str(t: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    v = t.get(key, default)
    if not isinstance(v, str) or not v:
        raise GraphError(f"{where}: {key} must be a non-empty string")
    return v


def _strs(t: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    v = t.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise GraphError(f"{where}: {key} must be a list of strings")
    return tuple(v)


# ---- projects and copies ----


def find_projects(graph: Graph, roots: Sequence[Path]) -> dict[str, Path]:
    """Each node's project under roots (graph.md 7.1), after checking that nodes on
    one project can be chained (chains). Raises GraphError."""
    if not roots:
        raise GraphError("no project roots; set [projects] roots in the user config")
    paths = {nid: find_project(n.repo, roots) for nid, n in graph.nodes.items()}
    chains(graph, paths)
    return paths


def chains(graph: Graph, projects: Mapping[str, Path]) -> dict[str, str | None]:
    """Each node's nearest ancestor on the same project, whose branch it starts from,
    or None. Nodes on one project that neither needs get separate branches. Raises
    GraphError if a node has two such ancestors that do not need each other, or sets
    ref while starting from an ancestor."""
    ancestors: dict[str, set[str]] = {}
    deps = {n.id: n.needs for n in graph.nodes.values()}
    for nid in TopologicalSorter(deps).static_order():
        ancestors[nid] = set().union(*({d} | ancestors[d] for d in deps[nid]))
    parents: dict[str, str | None] = {}
    for nid, node in graph.nodes.items():
        same = {a for a in ancestors[nid] if projects[a] == projects[nid]}
        nearest = sorted(a for a in same if not any(a in ancestors[b] for b in same))
        if len(nearest) > 1:
            raise GraphError(
                f"node {nid}: {' and '.join(nearest)} change {projects[nid]} "
                "separately; make one need the other"
            )
        if nearest and node.ref != "HEAD":
            raise GraphError(
                f"node {nid}: ref applies only to the first node on {projects[nid]}"
            )
        parents[nid] = nearest[0] if nearest else None
    return parents


def find_project(repo: str, roots: Sequence[Path]) -> Path:
    """<root>/<name> for repo "name" or "owner/name". With an owner, the project's
    origin must be github.com/owner/name. Raises GraphError."""
    m = REPO.fullmatch(repo)
    if not m:
        raise GraphError(f"repo {repo!r} must be a name or owner/name")
    owner, name = m.groups()
    found = [r / name for r in roots if (r / name / ".git").exists()]
    if not found:
        where = ", ".join(map(str, roots))
        raise GraphError(f"project {name} is not a git repo in {where}")
    if len(found) > 1:
        raise GraphError(
            f"project {name} is in more than one root: {', '.join(map(str, found))}"
        )
    path = Path(os.path.realpath(found[0]))
    if owner:
        url = _git(path, "remote", "get-url", "origin").strip()
        gm = GITHUB.fullmatch(url)
        if not gm or gm.group(1).lower() != repo.lower():
            raise GraphError(f"{path}: origin is {url}, not github.com/{repo}")
    return path


def provision(src: Path, dest: Path, ref: str, branch: str) -> tuple[str, bool]:
    """Clone src to dest with `git clone --local`, on a new branch at ref. Returns the
    base commit, and whether src has uncommitted changes (they are not copied).
    src may be an upstream node's copy."""
    try:
        base = _git(
            src, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
        ).strip()
    except GraphError:
        raise GraphError(f"{src}: ref {ref!r} is not a commit") from None
    dirty = bool(_git(src, "status", "--porcelain").strip())
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(
        dest.parent,
        "clone",
        "--local",
        "--no-checkout",
        "--quiet",
        "--",
        str(src),
        str(dest),
    )
    _git(dest, "checkout", "--quiet", "-b", branch, base)
    return base, dirty


def commit(ws: Path, message: str, exclude: Sequence[str] = ()) -> str:
    """Commit every change in ws that is not ignored or excluded, as timu. Returns
    HEAD, unchanged if there was nothing to commit. Only the engine calls this."""
    skip = [f":(exclude,literal){p}" for p in exclude]
    _git(ws, "add", "--all", "--", ".", *skip)
    if _git(ws, "diff", "--cached", "--name-only"):
        _git(ws, *COMMITTER, "commit", "--quiet", "--no-verify", "-m", message)
    return head(ws)


def head(ws: Path) -> str:
    return _git(ws, "rev-parse", "HEAD").strip()


def diff(ws: Path, base: str) -> str:
    """The commits on ws's branch since base, as a patch."""
    return _git(ws, "diff", "--binary", base, "HEAD")


def _git(cwd: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    try:
        r = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GraphError(f"git {args[0]} in {cwd}: {e}") from None
    if r.returncode not in ok:
        raise GraphError(f"git {args[0]} in {cwd}: {r.stderr.strip() or r.returncode}")
    return r.stdout


# ---- outputs ----


def _check_spec(spec: str, where: str) -> None:
    kind, _, arg = spec.partition(":")
    if kind == "toml":
        file, _, key = arg.partition("#")
        if file and key:
            return
    elif (
        spec in ("changelog:latest", "git:diff")
        or kind == "file"
        and arg
        and not arg.startswith("/")
    ):
        return
    raise GraphError(
        f"{where}: {spec!r} is not toml:FILE#KEY, file:GLOB, changelog:latest or "
        "git:diff"
    )


def extract(spec: str, ws: Path, base: str, files: Path | None = None) -> str:
    """An output of the workspace ws (graph.md 4.2), read after the engine commits.
    A file output is copied into files, and its value is the copy's path. Raises
    GraphError."""
    kind, _, arg = spec.partition(":")
    if kind == "file":
        if files is None:
            raise GraphError("file outputs need a directory to copy into")
        return str(_export(ws, arg, files))
    if kind == "toml":
        file, _, key = arg.partition("#")
        try:
            value: Any = tomllib.loads(_read(ws, file))
        except tomllib.TOMLDecodeError as e:
            raise GraphError(f"{file}: {e}") from None
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise GraphError(f"{file} has no key {key}")
            value = value[part]
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise GraphError(f"{file}: {key} is not a string or number")
        return str(value)
    if spec == "changelog:latest":
        return _latest_release(_read(ws, "CHANGELOG.md"))
    text = diff(ws, base)
    if len(text.encode()) > MAX_OUTPUT:
        raise GraphError(f"the diff is larger than {MAX_OUTPUT} bytes")
    return text


def _read(ws: Path, rel: str) -> str:
    """A file inside ws, after resolving symlinks, so an agent cannot point an output
    at a file outside its workspace."""
    target = Path(os.path.realpath(ws / rel))
    if not target.is_relative_to(ws) or not target.is_file():
        raise GraphError(f"{rel} is not a file in the workspace")
    if target.stat().st_size > MAX_OUTPUT:
        raise GraphError(f"{rel} is larger than {MAX_OUTPUT} bytes")
    return target.read_text(encoding="utf-8", errors="replace")


def _export(ws: Path, pattern: str, files: Path) -> Path:
    """Copy the one file in ws that matches pattern into files. Symlinks are
    resolved first, so a match outside ws is refused."""
    try:
        found = sorted({Path(os.path.realpath(p)) for p in ws.glob(pattern)})
    except (ValueError, NotImplementedError) as e:
        raise GraphError(f"{pattern}: {e}") from None
    if len(found) != 1:
        names = ", ".join(
            str(p.relative_to(ws)) if p.is_relative_to(ws) else str(p) for p in found
        )
        raise GraphError(
            f"{pattern} matches {len(found)} files, not 1: {names or 'none'}"
        )
    src = found[0]
    if not src.is_relative_to(ws) or not src.is_file():
        raise GraphError(f"{pattern} is not a file in the workspace")
    files.mkdir(parents=True, exist_ok=True)
    dest = files / src.name
    shutil.copyfile(src, dest)
    return dest


def _latest_release(changelog: str) -> str:
    """The first `## ` section that is not Unreleased, heading included."""
    lines = changelog.splitlines()
    start = None
    for i, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        if start is not None:
            return "\n".join(lines[start:i]).strip()
        if "unreleased" not in line.lower():
            start = i
    if start is None:
        raise GraphError("CHANGELOG.md has no released section")
    return "\n".join(lines[start:]).strip()


# ---- the engine ----


def run_graph(
    session: Session,
    graph: Graph,
    projects: Mapping[str, Path],
    work: Path,
    options: Options,
) -> GraphResult:
    """Run every node on a copy of its project under work. A node runs once its needs
    are done; if one is not, the node is skipped. options.params and inputs are
    replaced per node."""
    order = list(graph.nodes)
    parents = chains(graph, projects)
    ts = TopologicalSorter({n.id: n.needs for n in graph.nodes.values()})
    ts.prepare()
    results: dict[str, NodeResult] = {}
    while ts.is_active():
        for nid in sorted(ts.get_ready(), key=order.index):
            node = graph.nodes[nid]
            parent = results[p] if (p := parents[nid]) else None
            src = projects[nid]
            r = _run_node(session, node, results, src, parent, work, options)
            results[nid] = r
            session.emit(
                "node_result",
                node=nid,
                status=r.status,
                summary=r.summary,
                branch=r.branch,
                head=r.head,
            )
            ts.done(nid)
    status = max((r.status for r in results.values()), key=SEVERITY.index)
    done = sum(r.status == "done" for r in results.values())
    summary = f"{done} of {len(results)} nodes done"
    session.emit("workflow_result", status=status, summary=summary)
    return GraphResult(status, results)


def _run_node(
    session: Session,
    node: Node,
    results: Mapping[str, NodeResult],
    project: Path,
    parent: NodeResult | None,
    work: Path,
    options: Options,
) -> NodeResult:
    """Run node on a copy of project, or of parent's copy if parent is set."""
    if session.cancel.is_set():
        return NodeResult("cancelled", "not started")
    if blocked := [d for d in node.needs if results[d].status != "done"]:
        return NodeResult("skipped", f"{', '.join(blocked)} did not finish")
    wf = WORKFLOWS[node.workflow]
    ws, branch = work / node.id, f"timu/{session.run_id}/{node.id}"
    src, ref = (
        (parent.workdir, parent.head)
        if parent and parent.workdir
        else (project, node.ref)
    )
    try:
        params = _bind(node, results)
        base, dirty = provision(src, ws, ref, branch)
    except GraphError as e:
        return NodeResult("failed", str(e))
    files = work / "files"
    reads = tuple(sorted({files / d for d in node.needs if (files / d).is_dir()}))
    run = session.at(ws, node=node.id, budget=node.budget, reads=reads)
    run.emit("node", repo=node.repo, project=str(project), workflow=node.workflow)
    if dirty and not parent:
        message = f"{src} has uncommitted changes; node {node.id} starts from {ref}"
        run.emit("warning", message=message)
    if missing := [f"{d}.{o}" for d, o in node.inputs if o not in results[d].outputs]:
        return NodeResult(
            "failed",
            f"no {', '.join(missing)} to pass on",
            workdir=run.workdir,
            branch=branch,
            base=base,
            head=base,
        )
    inputs = tuple(results[d].outputs[out] for d, out in node.inputs)
    exclude = (
        (params.get("report_path", "REVIEW.md"),) if "report_path" in wf.params else ()
    )
    copy = partial(NodeResult, workdir=run.workdir, branch=branch, base=base)
    try:
        r = wf.run(run, node.objective, replace(options, params=params, inputs=inputs))
        status, summary = r.status, r.summary
    except (RoleError, ReportError) as e:
        status, summary = "failed", str(e)
    try:
        if _git(
            run.workdir, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD"
        ).split() != [
            base,
            f"refs/heads/{branch}",
        ]:
            return copy(
                "failed",
                "HEAD moved during the workflow; agents may not commit",
                head=base,
            )
        message = f"timu {node.id}: {node.objective.splitlines()[0][:60]}\n\n"
        message += f"{node.workflow} {status}: {summary}\n\nrun {session.run_id}"
        new_head = commit(run.workdir, message, exclude)
    except GraphError as e:
        return copy("failed", f"cannot commit: {e}", head=base)
    outputs: dict[str, Artifact] = {}
    if status == "done":
        outputs = {a.name: replace(a, name=f"{node.id}.{a.name}") for a in r.artifacts}
        for name, spec in node.outputs.items():
            try:
                content = extract(spec, run.workdir, base, files / node.id)
            except (GraphError, OSError) as e:
                status, summary = "failed", f"output {name}: {e}"
                break
            outputs[name] = Artifact(
                f"{node.id}.{name}",
                content,
                "file" if spec.startswith("file:") else "output",
                Origin.AGENT,
                node.id,
                r.untrusted,
            )
    return copy(status, summary, outputs, head=new_head)


def _bind(node: Node, results: Mapping[str, NodeResult]) -> dict[str, Any]:
    """node.params with each Ref replaced by its output, converted by the schema."""
    schema = WORKFLOWS[node.workflow].params
    params = {}
    for k, v in node.params.items():
        if isinstance(v, Template):
            v = schema[k]([_fill(c, results) for c in v.commands])
        elif isinstance(v, Ref):
            content = results[v.node].outputs[v.output].content.strip()
            try:
                v = schema[k](content)
            except ValueError as e:
                raise GraphError(f"params.{k} from {v.node}.{v.output}: {e}") from None
        params[k] = v
    return params


def _fill(command: str, results: Mapping[str, NodeResult]) -> str:
    """command with each ${node.output} replaced by its shell-quoted value. Only a
    file output's path or a TOKEN may enter a command. Raises GraphError."""

    def value(m: re.Match[str]) -> str:
        d, out = m.groups()
        a = results[d].outputs[out]
        text = a.content.strip()
        if a.kind != "file" and not TOKEN.fullmatch(text):
            raise GraphError(
                f"{d}.{out} may not enter a command: only a file path or a version-like "
                f"token may, and it is {text[:40]!r}"
            )
        return shlex.quote(text)

    return REF.sub(value, command)
