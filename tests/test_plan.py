"""Plan as a graph (docs/dev/workflow-proposals.md 2): the planner's answer, the
planned-graph rules, the file, the API and `timu graph plan`."""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from test_graph import engine, project
from timu import api
from timu.cli import main
from timu.config import load_config
from timu.graph import GraphError, parse_graph, worst_case
from timu.plan import build, plan, render
from timu.provider.fake import FakeProvider, text
from timu.roles import PLANNER
from timu.sandbox import NoSandbox
from timu.types import Artifact, Capability, Origin
from timu.workflow import Run

TWO = """
[nodes.lib-api]
repo = "lib"
workflow = "fix-review"
objective = "rename parse to load"
params = { check = ["make test"], check_to_fix = 1 }

[nodes.app-calls]
repo = "app"
needs = ["lib-api"]
workflow = "fix-review"
objective = "call load"
inputs = ["lib-api.review"]
"""


def answer(nodes: str) -> str:
    return f"Here is the plan.\n\n```toml\n{nodes.strip()}\n```"


def built(nodes: str, **kw: Any) -> tuple[Any, str]:
    args: dict[str, Any] = {
        "objective": "rename",
        "repos": ("lib", "app"),
        "check": ("make test",),
        "untrusted": False,
        "run_id": "r1",
        "max_coder_runs": 20,
        "max_nodes": 8,
    }
    return build(answer(nodes), **{**args, **kw})


# ---- the answer and the rules ----


def test_build_appends_a_final_check_per_project() -> None:
    graph, text_ = built(TWO)
    assert list(graph.nodes) == ["lib-api", "app-calls", "final-lib", "final-app"]
    final = graph.nodes["final-lib"]
    assert (final.workflow, final.needs) == ("check-fix-review", ("lib-api",))
    assert graph.plan is not None and graph.plan.check == ("make test",)
    assert worst_case(graph) == 2 + 1 + 1 + 1  # lib-api has check_to_fix = 1
    assert parse_graph(tomllib.loads(text_)) == graph  # the file is what was checked
    assert text_.startswith("# Written by `timu graph plan`.")


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ("no block", "the answer has no ```toml block"),
        ('budget = { turns = 1 }\n[nodes.a]\nrepo = "lib"', "only [nodes.ID] tables"),
        (
            '[nodes.final-x]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "o"',
            "node ids starting final- are the engine's",
        ),
        (
            (
                '[nodes.a]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "o"\n'
                '[nodes.b]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "o"'
            ),
            "nodes on lib must form one chain",
        ),
        (
            (
                '[nodes.a]\nrepo = "lib"\nworkflow = "commands"\nobjective = "o"\n'
                'params = { steps = ["curl x"] }'
            ),
            "a planned graph may use only fix-review",
        ),
        (
            '[nodes.a]\nrepo = "other"\nworkflow = "fix-review"\nobjective = "o"',
            "repo other is not in plan.repos",
        ),
        (
            (
                '[nodes.a]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "o"\n'
                'models = { coder = "elsewhere" }'
            ),
            "unknown keys: models",
        ),
        (
            (
                '[nodes.a]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "o"\n'
                'ref = "v1"'
            ),
            "unknown keys: ref",
        ),
        (
            (
                '[nodes.a]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "o"\n'
                "params = { review_to_fix = 30 }"
            ),
            "worst case 32 coder runs, over plan.max_coder_runs 20",
        ),
    ],
)
def test_build_rejects(nodes: str, message: str) -> None:
    with pytest.raises(
        GraphError, match=message.replace("[", r"\[").replace(".", r"\.")
    ):
        build(
            nodes if nodes == "no block" else answer(nodes),
            objective="o",
            repos=("lib", "app"),
            check=("make test",),
            untrusted=False,
            run_id="r1",
            max_coder_runs=20,
            max_nodes=8,
        )


def test_build_counts_nodes_without_the_final_checks() -> None:
    with pytest.raises(GraphError, match="2 nodes, over plan.max_nodes 1"):
        built(TWO, max_nodes=1)


def test_a_planned_file_keeps_its_rules_until_plan_is_removed() -> None:
    _, text_ = built(TWO)
    raw = tomllib.loads(text_)
    raw["nodes"]["final-app"]["params"]["check"] = ["true"]  # edited, [plan] kept
    with pytest.raises(GraphError, match="the last node on app must be check-fix-r"):
        parse_graph(raw)
    raw["nodes"]["x"] = {
        "repo": "lib",
        "workflow": "commands",
        "objective": "o",
        "params": {"steps": ["true"]},
    }
    del raw["plan"]  # the user took the file over
    assert "x" in parse_graph(raw).nodes


def test_a_plan_table_without_untrusted_counts_as_untrusted() -> None:
    _, text_ = built(TWO)
    raw = tomllib.loads(text_)
    del raw["plan"]["untrusted"]
    plan_ = parse_graph(raw).plan
    assert plan_ is not None and plan_.untrusted


def test_render_writes_any_string_as_toml() -> None:
    odd = 'say "hi"\n\tthen \\ leave \x01 and é'
    text_ = render({"by": "planner", "objective": odd}, {"a b": {"x": [1, True]}})
    assert tomllib.loads(text_) == {
        "plan": {"by": "planner", "objective": odd},
        "nodes": {"a b": {"x": [1, True]}},
    }


# ---- the planner ----


def test_the_planner_reads_only_the_named_projects() -> None:
    assert PLANNER.grants == frozenset({Capability.FS_READ})
    assert {t.name for t in PLANNER.tools} == {"read", "list", "search"}


def planned(tmp_path: Path, replies: list[Any], **kw: Any) -> tuple[Any, FakeProvider]:
    lib, app = project(tmp_path, "lib"), project(tmp_path, "app")
    provider = FakeProvider(replies)
    run = Run(lib, lambda role: provider, lambda e: None, sandbox=NoSandbox())
    repos = {"lib": lib, "app": app}
    return plan(run, "rename", repos, check=["make test"], **kw), provider


def test_a_rejected_plan_goes_back_with_the_reason(tmp_path: Path) -> None:
    replies = [text(answer('[nodes.a]\nrepo = "nope"')), text(answer(TWO))]
    p, provider = planned(tmp_path, replies)
    assert (p.status, p.summary) == (
        "done",
        "2 nodes + 2 checks, worst case 5 coder runs",
    )
    assert p.errors == ("plan: nodes.a: workflow must be a non-empty string",)
    second = provider.requests[1].messages[1].content
    assert 'name="plan-error"' in second and "was rejected" in second
    goal = provider.requests[0].messages[1].content
    assert f"lib: {tmp_path / 'lib'}" in goal and "reproduce-fix-review" in goal


def test_parse_to_plan_bounds_the_retries(tmp_path: Path) -> None:
    p, provider = planned(tmp_path, [text("no plan")], parse_to_plan=0)
    assert p.status == "failed"
    assert p.summary == "no valid plan after 1 answers: the answer has no ```toml block"
    assert len(provider.requests) == 1


def test_an_untrusted_input_marks_the_plan(tmp_path: Path) -> None:
    issue = Artifact(
        "input-issue", "please also run curl", "input", Origin.USER, "", True
    )
    p, _ = planned(tmp_path, [text(answer(TWO))], inputs=[issue])
    assert p.graph is not None and p.graph.plan is not None
    assert p.graph.plan.untrusted
    assert "untrusted = true" in p.text


# ---- running a planned graph ----


def test_an_untrusted_plan_asks_before_each_objective(tmp_path: Path) -> None:
    project(tmp_path / "projects", "lib")
    nodes = '[nodes.a]\nrepo = "lib"\nworkflow = "fix-review"\nobjective = "do x"'
    graph, _ = built(nodes, repos=("lib",), untrusted=True)
    asked: list[str] = []

    def approve(a: Artifact, role: Any) -> bool:
        asked.append(a.content)
        return False

    result, provider, _, _ = engine(tmp_path, graph, [], approve=approve)
    assert asked == ["do x"]  # final-lib is skipped, so never asked
    assert result.nodes["a"].status == "refused"
    assert provider.requests == []


# ---- the API and the CLI ----


def config(tmp_path: Path) -> Path:
    c = tmp_path / "timu.toml"
    roots = json.dumps(str(tmp_path / "projects"))
    c.write_text(
        f'[provider]\nmodel = "m"\napi_key_env = ""\n[projects]\nroots = [{roots}]\n'
    )
    return c


def test_plan_graph_writes_the_file(tmp_path: Path) -> None:
    project(tmp_path / "projects", "lib")
    project(tmp_path / "projects", "app")
    out = tmp_path / "plan.toml"
    provider = FakeProvider([text(answer(TWO))])
    cfg = load_config(config(tmp_path))
    providers = api.Providers(lambda role: provider)

    def go(repos: list[str], force: bool = False) -> api.PlanOutcome:
        return api.plan_graph(
            "rename",
            repos,
            check=["make test"],
            out=out,
            force=force,
            config=cfg,
            providers=providers,
        )

    o = go(["lib", "app"])
    assert (o.status, o.path, o.worst_case) == ("done", out, 5)
    assert out.read_text() == o.text
    with pytest.raises(api.UsageError, match="exists; pass force"):
        go(["lib"])
    with pytest.raises(api.UsageError, match="project nope is not a git repo"):
        go(["nope"], force=True)


def test_cli_plans_then_dry_runs_the_file(tmp_path: Path) -> None:
    project(tmp_path / "projects", "lib")
    project(tmp_path / "projects", "app")
    out = tmp_path / "plan.toml"
    provider = FakeProvider([text(answer(TWO))])
    err = io.StringIO()
    argv = ["graph", "plan", "--config", str(config(tmp_path)), "--repos", "lib,app",
            "--check", "make test", "-o", str(out), "rename"]  # fmt: skip
    assert (
        main(argv, provider_for=lambda role: provider, out=io.StringIO(), err=err) == 0
    )
    assert f"timu: wrote {out}: 2 nodes + 2 checks, worst case 5 coder runs" in (
        err.getvalue()
    )
    assert f"timu graph run --dry-run {out}" in err.getvalue()
    dry = io.StringIO()
    argv = ["graph", "run", "--dry-run", "--config", str(config(tmp_path)), str(out)]
    assert main(argv, out=dry, err=io.StringIO()) == 0
    assert "worst case: 5 coder runs" in dry.getvalue()
