"""Tests for workflow graphs: the graph file, projects, copies, outputs and the engine
(docs/dev/graph.md). Fixture repos are made and committed in pytest tmp dirs only."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest

from timu import Budget, Event
from timu.cli import main
from timu.graph import (
    Graph,
    GraphError,
    Ref,
    Template,
    _no_drivers,
    chains,
    commit,
    diff,
    extract,
    find_project,
    find_projects,
    parse_graph,
    provision,
    resumable,
    run_graph,
    serial_order,
)
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.role import Role
from timu.sandbox import NoSandbox
from timu.workflow import WORKFLOW_BUDGET, Options, Session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The user's git config (hooks, signing) does not reach the tests."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def project(root: Path, name: str, **files: str) -> Path:
    """A git repo with files committed on main."""
    p = root / name
    p.mkdir(parents=True)
    for rel, content in {"README.md": "x\n", **files}.items():
        (p / rel).write_text(content)
    git(p, "init", "-q", "-b", "main")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init")
    return p


def graph(source: str) -> Graph:
    return parse_graph(tomllib.loads(source))


# ---- the graph file ----

GOOD = """
[defaults]
budget = { cost_usd = 2.0 }

[nodes.a]
repo = "org/a"
workflow = "fix-review"
objective = "bump"
outputs = { rounds = "toml:pyproject.toml#tool.demo.rounds", notes = "changelog:latest" }

[nodes.b]
repo = "b"
needs = ["a"]
workflow = "fix-review"
objective = "update"
ref = "main"
params = { review_to_fix = "${a.rounds}", report_path = "R.md" }
inputs = ["a.notes", "a.review"]
budget = { turns = 5 }
"""


def test_parse() -> None:
    g = graph(GOOD)
    a, b = g.nodes["a"], g.nodes["b"]
    assert list(g.nodes) == ["a", "b"]
    assert a.budget == Budget(**{**WORKFLOW_BUDGET.__dict__, "cost_usd": 2.0})
    assert b.budget == Budget(**{**WORKFLOW_BUDGET.__dict__, "turns": 5})
    assert b.params == {"review_to_fix": Ref("a", "rounds"), "report_path": "R.md"}
    assert b.inputs == (("a", "notes"), ("a", "review"))
    assert (b.needs, b.ref) == (("a",), "main")


BASE = '[nodes.a]\nrepo = "a"\nworkflow = "fix-review"\nobjective = "o"\n'


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("[other]\n", "unknown keys: other"),
        ("[nodes.a.x]\n", "nodes.a: unknown keys: x"),
        ('[nodes.B]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n', "node id 'B'"),
        ('[nodes.b]\nrepo="b"\nworkflow="nope"\nobjective="o"\n', "workflow 'nope'"),
        ('[nodes.b]\nrepo="../b"\nworkflow="fix-review"\nobjective="o"\n', "repo"),
        (
            '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="use ${a.v}"\n',
            "allowed only in params",
        ),
        (
            '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nref="-x"\n',
            "ref",
        ),
        (
            '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["z"]\n',
            "needs 'z'",
        ),
        (
            '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\ninputs=["a.review"]\n',
            "must name a node in needs",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
                'inputs=["a.nope"]\n'
            ),
            "no output 'nope'",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                "params={ color = 1 }\n"
            ),
            "no param 'color'",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                "params={ review_to_fix = -1 }\n"
            ),
            "params.review_to_fix must be a non-negative integer",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
                'params={ report_path = "x-${a.review}" }\n'
            ),
            "must be the whole value",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
                'params={ report_path = "${a.review}" }\n'
            ),
            "declared output",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                'outputs={ v = "yaml:x" }\n'
            ),
            "is not toml:FILE#KEY",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                'outputs={ review = "git:diff" }\n'
            ),
            "hides the fix-review result",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                "budget={ turns = 1.5 }\n"
            ),
            "budget.turns must be a positive integer",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                "budget={ cost_usd = nan }\n"
            ),
            "budget.cost_usd must be a finite positive number",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
                "budget={ wall_seconds = inf }\n"
            ),
            "budget.wall_seconds must be a finite positive number",
        ),
        (
            '[nodes.b]\nrepo="b"\nworkflow="commands"\nobjective="o"\n',
            "commands needs params.steps",
        ),
        (
            '[nodes.b]\nrepo="b"\nworkflow="check-fix-review"\nobjective="o"\n',
            "check-fix-review needs params.check",
        ),
        (
            (
                '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["c"]\n'
                '[nodes.c]\nrepo="c"\nworkflow="fix-review"\nobjective="o"\nneeds=["b"]\n'
            ),
            "cycle",
        ),
    ],
)
def test_parse_errors(extra: str, message: str) -> None:
    with pytest.raises(GraphError, match=message.replace("$", r"\$")):
        graph(BASE + extra)


def test_parse_needs_nodes() -> None:
    with pytest.raises(GraphError, match="no nodes"):
        parse_graph({})


# ---- projects ----


def test_find_project(tmp_path: Path) -> None:
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    a = project(r1, "a")
    assert find_project("a", [r1, r2]) == a
    with pytest.raises(GraphError, match="not a git repo"):
        find_project("b", [r1, r2])
    project(r2, "a")
    with pytest.raises(GraphError, match="more than one root"):
        find_project("a", [r1, r2])


def test_find_project_checks_the_github_origin(tmp_path: Path) -> None:
    a = project(tmp_path, "a")
    with pytest.raises(GraphError, match="remote"):
        find_project("org/a", [tmp_path])  # no origin
    git(a, "remote", "add", "origin", "git@github.com:Org/a.git")
    assert find_project("org/a", [tmp_path]) == a
    with pytest.raises(GraphError, match="not github.com/other/a"):
        find_project("other/a", [tmp_path])


def test_chains(tmp_path: Path) -> None:
    project(tmp_path, "p")
    g = graph(
        BASE.replace('repo = "a"', 'repo = "p"')
        + '[nodes.b]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
        + '[nodes.c]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\nneeds=["b"]\n'
        + '[nodes.d]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\n'
    )
    projects = find_projects(g, [tmp_path])
    assert chains(g, projects) == {"a": None, "b": "a", "c": "b", "d": None}
    with pytest.raises(GraphError, match="no project roots"):
        find_projects(g, [])


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            (
                '[nodes.c]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\n'
                'needs=["a", "b"]\n'
            ),
            "a and b change .* separately",
        ),
        (
            (
                '[nodes.c]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\n'
                'needs=["a"]\nref="main"\n'
            ),
            "ref applies only to the first node",
        ),
    ],
)
def test_chain_errors(tmp_path: Path, extra: str, message: str) -> None:
    project(tmp_path, "p")
    g = graph(
        BASE.replace('repo = "a"', 'repo = "p"')
        + '[nodes.b]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\n'
        + extra
    )
    with pytest.raises(GraphError, match=message):
        find_projects(g, [tmp_path])


# ---- copies and outputs ----


def test_provision_copies_the_commit_and_leaves_the_source_alone(
    tmp_path: Path,
) -> None:
    src = project(tmp_path / "p", "a", **{"f.txt": "v1\n"})
    (src / "f.txt").write_text("uncommitted\n")
    before = git(src, "for-each-ref")
    ws = tmp_path / "work" / "a"
    base, dirty = provision(src, ws, "HEAD", "timu/r1/a")
    assert dirty
    assert (ws / "f.txt").read_text() == "v1\n"
    assert git(ws, "rev-parse", "HEAD").strip() == base
    assert git(ws, "branch", "--show-current").strip() == "timu/r1/a"
    assert git(src, "for-each-ref") == before  # no branch added to the source
    with pytest.raises(GraphError, match="is not a commit"):
        provision(src, tmp_path / "work" / "b", "nope", "x")


def test_commit_takes_new_files_and_leaves_out_the_report(tmp_path: Path) -> None:
    src = project(tmp_path / "p", "a", **{"f.txt": "v1\n"})
    ws = tmp_path / "work" / "a"
    base, _ = provision(src, ws, "HEAD", "timu/r1/a")
    (ws / "f.txt").write_text("v2\n")
    (ws / "new.txt").write_text("new\n")
    (ws / "REVIEW.md").write_text("VERDICT: APPROVE\n")
    new = commit(ws, "timu a: change", exclude=("REVIEW.md",))
    assert new != base
    assert git(ws, "log", "-1", "--format=%an <%ae>%n%s").split("\n")[:2] == [
        "timu <timu@localhost>",
        "timu a: change",
    ]
    patch = diff(ws, base)
    assert "f.txt" in patch and "new.txt" in patch and "REVIEW.md" not in patch
    assert commit(ws, "again", exclude=("REVIEW.md",)) == new  # nothing left


def test_commit_ignores_the_users_hooks_and_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n")
    src = project(tmp_path / "p", "a")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))  # after the fixture commit
    ws = tmp_path / "work" / "a"
    base, _ = provision(src, ws, "HEAD", "timu/r1/a")
    (ws / "f.txt").write_text("x\n")
    assert commit(ws, "m") != base


def test_git_runs_no_driver_the_workspace_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config drivers could run agent-written scripts on the host (F1)."""
    config = tmp_path / "gitconfig"
    config.write_text(
        '[filter "clean"]\n\tclean = sh clean.sh\n'
        '[filter "smudge"]\n\tsmudge = sh a_smudge.sh\n'
        '[diff "tc"]\n\ttextconv = sh textconv.sh\n'
    )
    src = project(tmp_path / "p", "a")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    ws = tmp_path / "work" / "a"
    base, _ = provision(src, ws, "HEAD", "timu/r1/a")
    marker = tmp_path / "marker"
    (ws / ".gitattributes").write_text(
        "c.txt filter=clean\ns.txt filter=smudge\nt.txt diff=tc\n"
    )
    for script in ("clean.sh", "a_smudge.sh", "textconv.sh"):
        (ws / script).write_text(f'echo {script} >> {marker}; cat "${{1:--}}"\n')
    for rel in ("c.txt", "s.txt", "t.txt"):
        (ws / rel).write_text("x\n")
    new = commit(ws, "m")
    diff(ws, base)
    provision(ws, tmp_path / "work" / "b", new, "timu/r1/b")  # a downstream node
    assert not marker.exists()
    assert git(ws, "show", f"{new}:c.txt") == "x\n"


def test_git_keeps_the_standard_lfs_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(
        '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tprocess = sh evil.sh\n'
        '[filter "x"]\n\tclean = sh x.sh\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    assert _no_drivers(tmp_path) == [
        *("-c", "filter.lfs.process="),
        *("-c", "filter.x.clean="),
    ]


def test_the_users_repo_can_fetch_the_branch(tmp_path: Path) -> None:
    src = project(tmp_path / "p", "a")
    ws = tmp_path / "work" / "a"
    provision(src, ws, "HEAD", "timu/r1/a")
    (ws / "f.txt").write_text("x\n")
    commit(ws, "m")
    git(src, "fetch", "-q", str(ws), "timu/r1/a:timu/r1/a")
    assert git(src, "show", "timu/r1/a:f.txt") == "x\n"
    assert not (src / "f.txt").exists()  # the user's checkout is unchanged


def test_diff_does_not_follow_symlinks(tmp_path: Path) -> None:
    """A new symlink is committed as a link, not as its target's contents."""
    src = project(tmp_path / "p", "a")
    ws = tmp_path / "work" / "a"
    base, _ = provision(src, ws, "HEAD", "timu/r1/a")
    (tmp_path / "secret.txt").write_text("s3cret\n")
    (ws / "link").symlink_to(tmp_path / "secret.txt")
    commit(ws, "m")
    patch = diff(ws, base)
    assert "new file mode 120000" in patch and "s3cret" not in patch


CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n- wip\n\n## [1.2.0]\n\n- new\n\n## [1.1.0]\n\n- old\n"


def test_extract(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pyproject.toml").write_text('[project]\nversion = "1.2.0"\n[tool]\nn = 3\n')
    (ws / "CHANGELOG.md").write_text(CHANGELOG)
    assert extract("toml:pyproject.toml#project.version", ws, "") == "1.2.0"
    assert extract("toml:pyproject.toml#tool.n", ws, "") == "3"
    assert extract("changelog:latest", ws, "") == "## [1.2.0]\n\n- new"
    with pytest.raises(GraphError, match="no key project.name"):
        extract("toml:pyproject.toml#project.name", ws, "")
    with pytest.raises(GraphError, match="not a string or number"):
        extract("toml:pyproject.toml#project", ws, "")


def test_file_outputs_with_one_name_stay_apart(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    for d, body in (("a", "A\n"), ("b", "B\n")):
        (ws / d).mkdir(parents=True)
        (ws / d / "x.txt").write_text(body)
    files = tmp_path / "files"
    one, two = (extract(f"file:{d}/x.txt", ws, "", files) for d in ("a", "b"))
    assert one != two
    assert (Path(one).read_text(), Path(two).read_text()) == ("A\n", "B\n")


def test_extract_stays_in_the_workspace(tmp_path: Path) -> None:
    """An agent could point pyproject.toml at a secret; the engine must not read it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.toml").write_text('key = "s3cret"\n')
    (ws / "pyproject.toml").symlink_to(tmp_path / "secret.toml")
    with pytest.raises(GraphError, match="not a file in the workspace"):
        extract("toml:pyproject.toml#key", ws, "")
    with pytest.raises(GraphError, match="not a file in the workspace"):
        extract("toml:../secret.toml#key", ws, "")


# ---- the engine ----

A_WORK = [
    calls(
        call(
            "write",
            path="pyproject.toml",
            content='[project]\nversion = "1.1.0"\n[tool.demo]\nrounds = 2\n',
        )
    ),
    calls(call("write", id="c2", path="CHANGELOG.md", content=CHANGELOG)),
    text("bumped"),
    text("VERDICT: APPROVE\nlooks right"),
]
B_WORK = [text("updated"), text("VERDICT: APPROVE\nok")]


def two_projects(root: Path) -> tuple[Path, Path]:
    return (
        project(root, "a", **{"pyproject.toml": '[project]\nversion = "1.0.0"\n'}),
        project(root, "b"),
    )


def engine(
    tmp_path: Path,
    g: Graph,
    replies: list[Reply],
    run_kw: dict[str, Any] | None = None,
    work: str = "work",
    **kw: Any,
) -> tuple[Any, FakeProvider, list[Event], dict[str, Path]]:
    """Run g with replies under tmp_path/work; run_kw goes to run_graph, kw to the
    Session."""
    provider = FakeProvider(replies)
    events: list[Event] = []
    run_id = kw.pop("run_id", "r1")
    session = Session(
        lambda role: provider, events.append, run_id=run_id, sandbox=NoSandbox(), **kw
    )
    projects = find_projects(g, [tmp_path / "projects"])
    result = run_graph(
        session, g, projects, tmp_path / work, Options({}), **(run_kw or {})
    )
    return result, provider, events, projects


AB = """
[nodes.a]
repo = "a"
workflow = "fix-review"
objective = "bump"
outputs = { rounds = "toml:pyproject.toml#tool.demo.rounds", notes = "changelog:latest" }

[nodes.b]
repo = "b"
needs = ["a"]
workflow = "fix-review"
objective = "update"
params = { review_to_fix = "${a.rounds}" }
inputs = ["a.notes", "a.review"]
"""


def test_outputs_flow_to_the_next_node(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    result, provider, events, _ = engine(tmp_path, graph(AB), A_WORK + B_WORK)
    assert result.status == "done", result.nodes
    ra, rb = result.nodes["a"], result.nodes["b"]
    assert ra.outputs["rounds"].content == "2"
    assert ra.outputs["notes"].source == "a"
    b_coder = provider.requests[4].messages[1].content  # after a's 4 replies
    assert "a.notes" in b_coder and "- new" in b_coder
    assert "a.review" in b_coder and "looks right" in b_coder
    assert ra.head != ra.base and rb.head == rb.base  # b changed nothing
    assert ra.branch == "timu/r1/a"
    assert git(ra.workdir, "show", f"{ra.head}:pyproject.toml").endswith("rounds = 2\n")
    assert "REVIEW.md" not in git(ra.workdir, "show", "--name-only", ra.head)
    agents = {e.node for e in events if e.kind == "start"}
    assert agents == {"a", "b"}
    assert [e.data["node"] for e in events if e.kind == "node_result"] == ["a", "b"]
    assert events[-1].kind == "workflow_result"


def test_a_failure_skips_only_its_descendants(tmp_path: Path) -> None:
    project(tmp_path / "projects", "a")
    project(tmp_path / "projects", "b")
    project(tmp_path / "projects", "c")
    g = graph(
        BASE
        + '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
        + '[nodes.c]\nrepo="c"\nworkflow="fix-review"\nobjective="o"\n'
    )
    replies = [
        text("done"),
        text("no verdict here"),
        text("done"),
        text("VERDICT: APPROVE"),
    ]
    result, *_ = engine(tmp_path, g, replies)
    statuses = {n: r.status for n, r in result.nodes.items()}
    assert statuses == {"a": "failed", "b": "skipped", "c": "done"}
    assert result.nodes["b"].summary == "a did not finish"
    assert result.status == "failed"


def test_a_bad_bound_param_fails_the_node(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    bad = [
        calls(
            call(
                "write",
                path="pyproject.toml",
                content='[tool.demo]\nrounds = "many"\n',
            )
        ),
        calls(call("write", id="c2", path="CHANGELOG.md", content=CHANGELOG)),
        text("bumped"),
        text("VERDICT: APPROVE"),
    ]
    result, *_ = engine(tmp_path, graph(AB), bad)
    assert result.nodes["b"].status == "failed"
    assert "params.review_to_fix from a.rounds: must be a non-negative integer" in (
        result.nodes["b"].summary
    )


def test_a_missing_output_fails_the_node(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    result, *_ = engine(
        tmp_path, graph(AB), [text("did nothing"), text("VERDICT: APPROVE")]
    )
    assert result.nodes["a"].status == "failed"
    assert result.nodes["a"].summary.startswith(
        "output rounds: pyproject.toml has no key"
    )
    assert result.nodes["b"].status == "skipped"


def test_a_node_budget_caps_its_agents(tmp_path: Path) -> None:
    project(tmp_path / "projects", "a")
    project(tmp_path / "projects", "b")
    g = graph(
        BASE.replace('objective = "o"\n', 'objective = "o"\nbudget = { turns = 1 }\n')
        + '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\n'
    )
    replies = [calls(call("list", path=".")), text("done"), text("VERDICT: APPROVE")]
    result, *_ = engine(tmp_path, g, replies)
    assert result.nodes["a"].status == "budget"
    assert result.nodes["b"].status == "done"  # the session budget is not spent
    assert result.status == "budget"


def test_cancel_stops_before_the_next_node(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    g = graph(AB)
    cancel = threading.Event()
    cancel.set()
    result, *_ = engine(tmp_path, g, [], cancel=cancel)
    assert {n: r.status for n, r in result.nodes.items()} == {
        "a": "cancelled",
        "b": "cancelled",
    }
    assert result.status == "cancelled"


def test_a_later_node_on_the_same_project_starts_from_the_earlier_branch(
    tmp_path: Path,
) -> None:
    project(tmp_path / "projects", "p")
    g = graph(
        BASE.replace('repo = "a"', 'repo = "p"')
        + '[nodes.b]\nrepo="p"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
    )
    replies = [
        calls(call("write", path="one.txt", content="1\n")),
        text("done"),
        text("VERDICT: APPROVE"),
        calls(call("read", path="one.txt")),
        calls(call("write", id="w2", path="two.txt", content="2\n")),
        text("done"),
        text("VERDICT: APPROVE"),
    ]
    result, *_ = engine(tmp_path, g, replies)
    ra, rb = result.nodes["a"], result.nodes["b"]
    assert result.status == "done", result.nodes
    assert rb.base == ra.head
    assert git(rb.workdir, "log", "--format=%s", rb.head).split("\n")[:2] == [
        "timu b: o",
        "timu a: o",
    ]


def test_an_agent_commit_fails_the_node(tmp_path: Path) -> None:
    project(tmp_path / "projects", "a")
    agent_commit = (
        "git -c user.name=x -c user.email=x@x commit -q --allow-empty -m sneaky"
    )
    replies = [
        calls(call("shell", command=agent_commit)),
        text("done"),
        text("VERDICT: APPROVE"),
    ]
    result, *_ = engine(tmp_path, graph(BASE), replies)
    ra = result.nodes["a"]
    assert (ra.status, ra.summary) == (
        "failed",
        "HEAD moved during the workflow; agents may not commit",
    )
    assert ra.head == ra.base


# ---- command steps and file outputs ----

LIB_APP = """
[nodes.lib]
repo = "lib"
workflow = "commands"
objective = "build"
params = { steps = ["mkdir -p dist && echo built > dist/lib-1.0.whl"] }
outputs = { wheel = "file:dist/*.whl", version = "toml:pyproject.toml#project.version" }

[nodes.app]
repo = "app"
needs = ["lib"]
workflow = "commands"
objective = "install"
params = { steps = ["cat ${lib.wheel} > got.txt", "echo ${lib.version} ${HOME+x} > v.txt"] }
"""


def lib_and_app(root: Path) -> None:
    project(
        root,
        "lib",
        **{"pyproject.toml": '[project]\nversion = "1.0"\n', ".gitignore": "dist/\n"},
    )
    project(root, "app")


def test_parse_command_templates() -> None:
    g = graph(LIB_APP)
    assert g.nodes["app"].params["steps"] == Template(
        ("cat ${lib.wheel} > got.txt", "echo ${lib.version} ${HOME+x} > v.txt")
    )


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ('"true"', "must be a non-empty list of commands"),
        ('["cat ${lib.nope}"]', r"\$\{lib.nope\} must name a declared output"),
        ('["cat ${other.wheel}"]', r"\$\{other.wheel\} must name a declared output"),
    ],
)
def test_command_template_errors(steps: str, message: str) -> None:
    with pytest.raises(GraphError, match=message):
        graph(
            LIB_APP.replace(
                'params = { steps = ["cat ${lib.wheel}',
                f"params = {{ steps = {steps} }}\n#",
            )
        )


def test_file_outputs_must_be_relative() -> None:
    with pytest.raises(GraphError, match="is not toml:FILE#KEY, file:GLOB"):
        graph(LIB_APP.replace("file:dist/*.whl", "file:/etc/passwd"))


def test_a_file_output_reaches_a_command(tmp_path: Path) -> None:
    lib_and_app(tmp_path / "projects")
    result, provider, _, _ = engine(tmp_path, graph(LIB_APP), [])
    assert result.status == "done", result.nodes
    wheel = result.nodes["lib"].outputs["wheel"]
    assert wheel.kind == "file"
    assert wheel.content == str(
        tmp_path / "work" / "_files" / "lib" / "dist" / "lib-1.0.whl"
    )
    app = result.nodes["app"].workdir
    assert app is not None
    assert (app / "got.txt").read_text() == "built\n"
    assert (app / "v.txt").read_text() == "1.0 x\n"  # ${HOME+x} is shell syntax
    assert provider.requests == []  # no model in either node
    assert "dist" not in git(result.nodes["lib"].workdir, "show", "--name-only", "HEAD")


def test_a_node_may_be_named_files(tmp_path: Path) -> None:
    """File outputs have their own directory, which no node id can name."""
    lib_and_app(tmp_path / "projects")
    result, _, _, _ = engine(
        tmp_path, graph(LIB_APP.replace("nodes.app", "nodes.files")), []
    )
    assert result.status == "done", result.nodes
    copy = result.nodes["files"].workdir
    assert copy is not None
    assert (copy / "got.txt").read_text() == "built\n"
    assert not (copy / "lib").exists()  # no export landed in the node's copy


def test_free_text_may_not_enter_a_command(tmp_path: Path) -> None:
    lib_and_app(tmp_path / "projects")
    (tmp_path / "projects" / "lib" / "pyproject.toml").write_text(
        '[project]\nversion = "1; rm -rf ~"\n'
    )
    git(tmp_path / "projects" / "lib", "commit", "-qam", "evil")
    result, *_ = engine(tmp_path, graph(LIB_APP), [])
    app = result.nodes["app"]
    assert app.status == "failed"
    assert "lib.version may not enter a command" in app.summary


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("true", "dist/\\*.whl matches 0 files"),
        ("mkdir -p dist && touch dist/a.whl dist/b.whl", "matches 2 files"),
    ],
)
def test_a_file_output_needs_exactly_one_match(
    tmp_path: Path, step: str, message: str
) -> None:
    lib_and_app(tmp_path / "projects")
    g = graph(LIB_APP.replace("mkdir -p dist && echo built > dist/lib-1.0.whl", step))
    result, *_ = engine(tmp_path, g, [])
    assert result.nodes["lib"].status == "failed"
    assert re.search(message, result.nodes["lib"].summary)
    assert result.nodes["app"].status == "skipped"


def test_a_file_output_cannot_leave_the_workspace(tmp_path: Path) -> None:
    lib_and_app(tmp_path / "projects")
    (tmp_path / "secret").write_text("s3cret\n")
    step = f"mkdir -p dist && ln -s {tmp_path / 'secret'} dist/lib-1.0.whl"
    g = graph(LIB_APP.replace("mkdir -p dist && echo built > dist/lib-1.0.whl", step))
    result, *_ = engine(tmp_path, g, [])
    assert result.nodes["lib"].status == "failed"
    assert "is not a file in the workspace" in result.nodes["lib"].summary
    assert not (tmp_path / "work" / "_files" / "lib").exists()


# A model node, a model-free build of its output, and a node with network on.
NETWORK = BASE + (
    '[nodes.w]\nrepo = "w"\nneeds = ["a"]\nworkflow = "commands"\nobjective = "o"\n'
    'params = { steps = ["true"] }\n'
    '[nodes.n]\nrepo = "n"\nneeds = ["w"]\nworkflow = "commands"\nobjective = "o"\n'
    'params = { steps = ["true"], network = true }\n'
)


def test_network_downstream_of_a_model_is_refused() -> None:
    """a's coder could have written what n runs, through w's output (graph.md 7.7)."""
    with pytest.raises(GraphError, match="nodes.n: network is on, and a ran a model"):
        graph(NETWORK)
    assert graph(NETWORK + "trust_upstream = true\n").nodes["n"].trust_upstream
    no_model = NETWORK.replace(
        'workflow = "fix-review"',
        'workflow = "commands"\nparams = { steps = ["true"] }',
        1,
    )
    assert not graph(no_model).nodes["n"].trust_upstream


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("trust_upstream = 1\n", "trust_upstream must be true or false"),
        ("trust_upstream = true\n", "trust_upstream needs network = true"),
    ],
)
def test_trust_upstream_errors(extra: str, message: str) -> None:
    with pytest.raises(GraphError, match=message):
        graph(BASE + extra)


def test_trust_upstream_warns(tmp_path: Path) -> None:
    for name in ("a", "w", "n"):
        project(tmp_path / "projects", name)
    g = graph(NETWORK + "trust_upstream = true\n")
    replies = [text("done"), text("VERDICT: APPROVE")]
    result, _, events, _ = engine(tmp_path, g, replies)
    assert result.status == "done", result.nodes
    warnings = [e.data["message"] for e in events if e.kind == "warning"]
    assert "node n runs commands with network on code models changed" in warnings


def test_graph_only_workflows_are_not_offered_to_run(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--workflow", "commands", "x"], err=io.StringIO())


def test_a_missing_optional_result_fails_the_node(tmp_path: Path) -> None:
    """check-fix-review has a review only if it had to fix something."""
    project(tmp_path / "projects", "a")
    project(tmp_path / "projects", "b")
    g = graph(
        '[nodes.a]\nrepo="a"\nworkflow="check-fix-review"\nobjective="o"\n'
        'params={ check = ["true"] }\n'
        '[nodes.b]\nrepo="b"\nworkflow="fix-review"\nobjective="o"\nneeds=["a"]\n'
        'inputs=["a.review"]\n'
    )
    result, _, events, _ = engine(tmp_path, g, [])
    assert result.nodes["a"].status == "done"
    assert result.nodes["b"].summary == "no a.review to pass on"
    assert not [e for e in events if e.kind == "warning"]  # no coder ran in a


# ---- the CLI ----


def test_cli_graph_run(tmp_path: Path) -> None:
    a, _ = two_projects(tmp_path / "projects")
    config = tmp_path / "timu.toml"
    config.write_text(
        '[provider]\nmodel = "m"\napi_key_env = ""\n'
        f"[projects]\nroots = [{json.dumps(str(tmp_path / 'projects'))}]\n"
    )
    file = tmp_path / "graph.toml"
    file.write_text(AB)
    provider = FakeProvider(A_WORK + B_WORK)
    out, err = io.StringIO(), io.StringIO()
    argv = ["graph", "run", "--config", str(config), "--unsafe-no-sandbox", str(file)]
    code = main(argv, provider_for=lambda role: provider, out=out, err=err)
    assert code == 0, err.getvalue()
    log = err.getvalue()
    assert "== node a: a (fix-review)" in log
    assert "timu: node a: done: approved in round 1" in log
    assert f"git -C {a} fetch " in log and "timu/" in log
    trace_out = io.StringIO()
    assert main(["trace"], out=trace_out, err=io.StringIO()) == 0
    assert "[a] a1 coder done" in trace_out.getvalue()
    assert "[b] a3 coder done" in trace_out.getvalue()


def test_cli_graph_usage_errors(tmp_path: Path) -> None:
    config = tmp_path / "timu.toml"
    config.write_text('[provider]\nmodel = "m"\napi_key_env = ""\n')
    file = tmp_path / "graph.toml"
    file.write_text(BASE)
    err = io.StringIO()
    argv = ["graph", "run", "--config", str(config), str(file)]
    assert main(argv, provider_for=lambda role: FakeProvider([]), err=err) == 2
    assert "no project roots" in err.getvalue()
    file.write_text("[nodes.a]\n")
    err = io.StringIO()
    assert main(argv, provider_for=lambda role: FakeProvider([]), err=err) == 2
    assert "repo must be a non-empty string" in err.getvalue()


def test_a_network_output_reaches_the_approval_gate(tmp_path: Path) -> None:
    """A network node's extracted output is first-hand web content (F3)."""
    project(tmp_path / "projects", "n")
    project(tmp_path / "projects", "c")
    asked: list[tuple[str, str]] = []

    def deny(a: Any, role: Any) -> bool:
        asked.append((a.name, role.name))
        return False

    g = graph(
        '[nodes.n]\nrepo = "n"\nworkflow = "commands"\nobjective = "fetch"\n'
        'params = { steps = ["echo page > x.txt"], network = true }\n'
        'outputs = { x = "file:x.txt" }\n'
        '[nodes.c]\nrepo = "c"\nneeds = ["n"]\ninputs = ["n.x"]\n'
        'workflow = "fix-review"\nobjective = "apply"\n'
    )
    result, provider, _, _ = engine(tmp_path, g, [], approve=deny)
    assert asked == [("n.x", "coder")]
    assert result.nodes["c"].status == "refused"
    assert provider.requests == []


# ---- models, parallel nodes and resume ----


def test_models_pick_a_model_per_role(tmp_path: Path) -> None:
    project(tmp_path / "projects", "a")
    g = graph(BASE + 'models = { reviewer = "small" }\n')
    assert g.nodes["a"].models == {"reviewer": "small"}
    asked: list[tuple[str, str]] = []
    small = FakeProvider([text("VERDICT: APPROVE")])

    def model_provider(role: Role, model: str) -> FakeProvider:
        asked.append((role.name, model))
        return small

    result, provider, *_ = engine(
        tmp_path, g, [text("done")], model_provider=model_provider
    )
    assert result.status == "done", result.nodes
    assert asked == [("reviewer", "small")]
    assert (len(provider.requests), len(small.requests)) == (1, 1)


@pytest.mark.parametrize(
    ("models", "message"),
    [
        ('{ lead = "x" }', "fix-review runs no lead; roles: coder, reviewer"),
        ('{ coder = "" }', "models.coder must be a non-empty string"),
        ('"x"', "models must be a table"),
    ],
)
def test_models_errors(models: str, message: str) -> None:
    with pytest.raises(GraphError, match=message):
        graph(BASE + f"models = {models}\n")


class PerThread:
    """A FakeProvider per thread, so parallel nodes each get the whole script. The
    first call in each thread waits until parties threads have made one."""

    def __init__(self, script: list[Reply], parties: int) -> None:
        self.script, self.local = script, threading.local()
        self.barrier = threading.Barrier(parties, timeout=10)

    def complete(self, messages: Any, tools: Any, on_text: Any = None) -> Reply:
        if not hasattr(self.local, "fake"):
            self.local.fake = FakeProvider(self.script)
            self.barrier.wait()  # breaks, failing the node, unless both run at once
        fake: FakeProvider = self.local.fake
        return fake.complete(messages, tools, on_text)


PARALLEL = BASE + '[nodes.c]\nrepo = "c"\nworkflow = "fix-review"\nobjective = "o"\n'


def test_independent_nodes_run_at_once(tmp_path: Path) -> None:
    project(tmp_path / "projects", "a")
    project(tmp_path / "projects", "c")
    shared = PerThread([text("done"), text("VERDICT: APPROVE")], 2)
    events: list[Event] = []
    session = Session(
        lambda role: shared, events.append, run_id="r1", sandbox=NoSandbox()
    )
    g = graph(PARALLEL)
    projects = find_projects(g, [tmp_path / "projects"])
    result = run_graph(
        session, g, projects, tmp_path / "work", Options({}), max_parallel=2
    )
    assert {n: r.status for n, r in result.nodes.items()} == {"a": "done", "c": "done"}
    ids = [e.agent_id for e in events if e.kind == "start"]
    assert sorted(ids) == ["a1", "a2", "a3", "a4"]  # unique across threads
    assert events[-1].kind == "workflow_result"


def test_a_node_error_reaches_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project(tmp_path / "projects", "a")
    project(tmp_path / "projects", "c")

    def boom(*args: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("timu.graph._run_node", boom)
    cancel = threading.Event()
    with pytest.raises(RuntimeError, match="boom"):
        engine(tmp_path, graph(PARALLEL), [], {"max_parallel": 2}, cancel=cancel)
    assert cancel.is_set()  # the other node stops too


def test_serial_order_is_file_order_among_ready_nodes() -> None:
    g = graph(
        PARALLEL
        + '[nodes.b]\nrepo = "b"\nworkflow = "fix-review"\nobjective = "o"\n'
        + 'needs = ["a"]\n'
    )
    assert serial_order(g) == ["a", "c", "b"]


def test_a_node_whose_coder_changed_nothing_is_flagged(tmp_path: Path) -> None:
    project(tmp_path / "projects", "a")
    result, _, events, _ = engine(
        tmp_path, graph(BASE), [text("done"), text("VERDICT: APPROVE")]
    )
    assert result.status == "done"
    warnings = [e.data["message"] for e in events if e.kind == "warning"]
    assert "node a changed nothing" in warnings


B_FAILS = [text("updated"), text("no verdict")]


def test_resume_reuses_done_nodes_and_their_outputs(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    g = graph(AB)
    first, _, events, projects = engine(tmp_path, g, A_WORK + B_FAILS)
    assert {n: r.status for n, r in first.nodes.items()} == {"a": "done", "b": "failed"}
    prior = resumable(events, g, projects)
    assert list(prior) == ["a"]
    assert (
        prior["a"].outputs["notes"].content == first.nodes["a"].outputs["notes"].content
    )
    result, provider, again, _ = engine(
        tmp_path, g, B_WORK, run_kw={"prior": prior}, work="work2", run_id="r2"
    )
    assert result.status == "done", result.nodes
    assert result.nodes["a"].reused and result.nodes["a"].head == first.nodes["a"].head
    assert len(provider.requests) == 2  # only b ran
    b_coder = provider.requests[0].messages[1].content
    assert "a.notes" in b_coder and "- new" in b_coder
    record = next(e.data for e in again if e.kind == "node_result")
    assert (record["node"], record["reused"]) == ("a", True)
    assert list(resumable(again, g, projects)) == ["a", "b"]  # a resume of a resume


def test_resume_reruns_a_changed_node_and_what_needs_it(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    g = graph(AB)
    first, _, events, projects = engine(tmp_path, g, A_WORK + B_WORK)
    assert first.status == "done"
    changed = graph(AB.replace('objective = "bump"', 'objective = "bump it"'))
    assert resumable(events, changed, projects) == {}
    downstream = graph(AB.replace('objective = "update"', 'objective = "update it"'))
    assert list(resumable(events, downstream, projects)) == ["a"]


def test_resume_reruns_a_node_whose_copy_moved(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    g = graph(AB)
    first, _, events, projects = engine(tmp_path, g, A_WORK + B_WORK)
    git(first.nodes["a"].workdir, "commit", "-q", "--allow-empty", "-m", "later")
    assert list(resumable(events, g, projects)) == []
    shutil.rmtree(first.nodes["b"].workdir)
    assert list(resumable(events, graph(BASE), projects)) == []  # a moved; b absent


def cli_config(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "timu.toml"
    config.write_text(
        '[provider]\nmodel = "m"\napi_key_env = ""\n'
        f"[projects]\nroots = [{json.dumps(str(tmp_path / 'projects'))}]\n{extra}"
    )
    return config


def test_cli_dry_run_prints_the_plan_and_runs_nothing(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    config = tmp_path / "timu.toml"
    config.write_text(
        '[provider]\nmodel = "m"\napi_key_env = "TIMU_TEST_NO_SUCH_KEY"\n'
        '[roles.reviewer]\nmodel = "r"\n'
        f"[projects]\nroots = [{json.dumps(str(tmp_path / 'projects'))}]\n"
    )
    file = tmp_path / "graph.toml"
    file.write_text(
        AB.replace(
            'objective = "bump"', 'objective = "bump"\nbudget = { cost_usd = 1.5 }'
        ).replace(
            'objective = "update"', 'objective = "update"\nmodels = { coder = "c" }'
        )
    )
    out, err = io.StringIO(), io.StringIO()
    argv = ["graph", "run", "--dry-run", "--config", str(config), str(file)]
    assert main(argv, out=out, err=err) == 0, err.getvalue()
    projects = tmp_path / "projects"
    assert out.getvalue() == (
        "2 nodes, at most 1 at a time:\n"
        f"  1. a: fix-review on {projects / 'a'}\n"
        "       coder m, reviewer r\n"
        "       cost cap $1.50\n"
        f"  2. b: fix-review on {projects / 'b'} after a\n"
        "       coder c, reviewer r\n"
        "       cost cap none\n"
        "ceiling: none; b set no budget.cost_usd and --max-cost is not given\n"
    )
    assert not (tmp_path / "state").exists()  # no trace, no copies
    out = io.StringIO()
    argv[3:3] = ["--max-cost", "3", "--max-parallel", "2"]
    assert main(argv, out=out, err=err) == 0
    assert out.getvalue().startswith("2 nodes, at most 2 at a time:")
    assert out.getvalue().endswith("ceiling: $3.00, if the server reports cost\n")


def test_cli_resume_and_report(tmp_path: Path) -> None:
    two_projects(tmp_path / "projects")
    config = cli_config(tmp_path)
    file = tmp_path / "graph.toml"
    file.write_text(AB)
    argv = ["graph", "run", "--config", str(config), "--unsafe-no-sandbox", str(file)]
    provider = FakeProvider(A_WORK + B_FAILS)
    err = io.StringIO()
    assert main(argv, provider_for=lambda role: provider, err=err) == 1
    (trace,) = (tmp_path / "state" / "timu" / "runs").glob("*.jsonl")
    provider = FakeProvider(B_WORK)
    err = io.StringIO()
    resume = [*argv[:2], "--resume", trace.stem, *argv[2:]]
    assert main(resume, provider_for=lambda role: provider, err=err) == 0, (
        err.getvalue()
    )
    assert f"timu: from {trace.stem}, reusing a" in err.getvalue()
    assert "timu: node a: done (reused): approved in round 1" in err.getvalue()
    out = io.StringIO()
    assert main(["report"], out=out, err=io.StringIO()) == 0
    rows = [line.split() for line in out.getvalue().splitlines()]
    assert rows[0] == ["workflow", "runs", "done", "cost", "tokens", "time"]
    assert rows[1][:4] == ["fix-review", "3", "2", "(66%)"]  # a, b, b; a reused ran not
    out = io.StringIO()
    assert main(["report", "--by", "project"], out=out, err=io.StringIO()) == 0
    assert [r.split()[:2] for r in out.getvalue().splitlines()[1:]] == [
        ["a", "1"],
        ["b", "2"],
    ]


def test_cli_parallel_names_each_line_with_its_node(tmp_path: Path) -> None:
    for name in ("a", "c"):
        project(tmp_path / "projects", name)
    file = tmp_path / "graph.toml"
    file.write_text(PARALLEL)
    shared = PerThread([text("done\n"), text("VERDICT: APPROVE")], 2)
    out, err = io.StringIO(), io.StringIO()
    argv = [
        "graph",
        "run",
        "--max-parallel",
        "2",
        "--config",
        str(cli_config(tmp_path)),
    ]
    argv += ["--unsafe-no-sandbox", str(file)]
    assert main(argv, provider_for=lambda role: shared, out=out, err=err) == 0
    assert sorted(out.getvalue().splitlines()) == [
        "[a] VERDICT: APPROVE",
        "[a] done",
        "[c] VERDICT: APPROVE",
        "[c] done",
    ]
    log = err.getvalue().splitlines()
    assert "[a] == round 1 code" in log and "[c] == round 1 code" in log
    assert "== node a: done: approved in round 1" in log  # node lines name it already
