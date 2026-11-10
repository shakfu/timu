"""Tests for timu.api: runs without a terminal, as a UI would start them."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from timu import api
from timu.config import Config
from timu.events import Event
from timu.graph import GraphResult
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text

FIX = [
    calls(call("read", path="calc.py")),
    calls(call("edit", path="calc.py", old_string="a - b", new_string="a + b")),
    text("Fixed add()."),
    text("VERDICT: APPROVE\nadd() adds."),
]
CONFIG = Config(model="m", api_key_env="")


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / ".gitignore").write_text("REVIEW.md\n")
    git(r, "init", "-q")
    git(r, "add", ".")
    git(r, "commit", "-qm", "seed")
    return r


def fake(replies: list[Reply]) -> tuple[api.Providers, FakeProvider]:
    provider = FakeProvider(replies)
    return api.Providers(lambda role: provider), provider


def start(workdir: Path, replies: list[Reply], **kw: Any) -> api.Outcome:
    providers, _ = fake(replies)
    return api.run(
        "fix add()",
        workdir,
        config=CONFIG,
        providers=providers,
        unsafe_no_sandbox=True,
        **kw,
    )


def test_a_run_on_a_copy(repo: Path) -> None:
    events: list[Event] = []
    out = start(repo, FIX, sink=events.append)
    assert (out.status, out.summary) == ("done", "approved in round 1")
    assert out.repo == Path(repo).resolve() and out.copy is not None
    assert out.branch == f"timu/{out.run_id}/run"
    assert git(out.copy, "show", f"{out.branch}:calc.py").endswith("a + b\n")
    assert "a - b" in (repo / "calc.py").read_text()  # the checkout is unchanged
    assert out.report == out.copy / "REVIEW.md" and out.report.is_file()
    assert out.trace.is_file() and out.trace.stem == out.run_id
    assert out.usage.turns == 4
    kinds = [e.kind for e in events]
    assert kinds[0] == "config" and "workflow_result" in kinds


def test_a_run_in_place(repo: Path) -> None:
    out = start(repo, FIX, in_place=True)
    assert out.status == "done" and out.copy is None and out.branch is None
    assert "a + b" in (repo / "calc.py").read_text()


def test_a_run_that_changes_nothing_has_no_branch(repo: Path) -> None:
    out = start(repo, [text("Nothing to do."), text("VERDICT: APPROVE\nfine")])
    assert out.status == "done" and out.branch is None and out.copy is not None


def test_params_reach_the_workflow(repo: Path) -> None:
    changes = text("VERDICT: CHANGES\n- more")
    out = start(repo, [*FIX[:3], changes], params={"review_to_fix": 0})
    assert out.status == "failed" and out.summary == "not approved after 1 rounds"


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"workflow": "nope"}, "unknown workflow 'nope'; workflows: fix-review"),
        ({"workflow": "commands"}, "unknown workflow 'commands'"),  # graph only
        ({"params": {"steps": ["x"]}}, "steps does not apply to fix-review"),
        ({"params": {"review_to_fix": -1}}, "review_to_fix must be a non-negative"),
    ],
)
def test_usage_errors(repo: Path, kw: dict[str, Any], message: str) -> None:
    with pytest.raises(api.UsageError, match=message):
        start(repo, [], **kw)


def test_usage_errors_for_the_workdir(tmp_path: Path) -> None:
    with pytest.raises(api.UsageError, match="is not a directory"):
        start(tmp_path / "none", [])
    (tmp_path / "plain").mkdir()
    with pytest.raises(api.UsageError, match="is not in a git repo"):
        start(tmp_path / "plain", [])


def test_a_missing_key_fails_before_the_run(repo: Path) -> None:
    with pytest.raises(api.UsageError, match="SIM_KEY is not set"):
        api.run("x", repo, config=Config(model="m", api_key_env="SIM_KEY"))


def test_without_approve_untrusted_content_is_refused(tmp_path: Path) -> None:
    """gate on and no approve: the coder never sees web content."""
    providers, provider = fake([text("facts\nSources:\nhttps://a.example")])
    events: list[Event] = []
    out = api.run(
        "x",
        tmp_path,
        workflow="research-fix-review",
        config=CONFIG,
        providers=providers,
        gate=True,
        in_place=True,
        unsafe_no_sandbox=True,
        sink=events.append,
    )
    assert out.status == "refused"
    assert "not approved for coder" in out.summary
    assert len(provider.requests) == 1
    assert [e.data["approved"] for e in events if e.kind == "approval"] == [False]


def test_approve_is_asked(tmp_path: Path) -> None:
    asked: list[tuple[str, str]] = []
    providers, _ = fake([text("facts\nSources:\nhttps://a.example")])

    def ask(a: Any, r: Any) -> bool:
        asked.append((a.name, r.name))
        return False

    api.run(
        "x",
        tmp_path,
        workflow="research-fix-review",
        config=CONFIG,
        providers=providers,
        gate=True,
        approve=ask,
        in_place=True,
        unsafe_no_sandbox=True,
    )
    assert asked == [("research", "coder")]


def test_cancel_before_the_first_step(repo: Path) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    out = start(repo, FIX, cancel=cancel)
    assert out.status == "cancelled"


def test_a_graph(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "a"
    project.mkdir(parents=True)
    git(project, "init", "-q")
    git(project, "commit", "-q", "--allow-empty", "-m", "first")
    graph = tmp_path / "graph.toml"
    graph.write_text(
        '[nodes.a]\nrepo = "a"\nworkflow = "commands"\nobjective = "o"\n'
        'params = { steps = ["true"] }\n'
    )
    config = Config(project_roots=(tmp_path / "projects",))
    providers, _ = fake([])
    out = api.run_graph(
        graph, config=config, providers=providers, unsafe_no_sandbox=True
    )
    assert (out.status, out.summary) == ("done", "a done")
    assert isinstance(out.result, GraphResult)
    assert out.projects == {"a": project.resolve()}


def test_graph_usage_errors(tmp_path: Path) -> None:
    providers, _ = fake([])
    with pytest.raises(api.UsageError):
        api.run_graph(tmp_path / "none.toml", config=CONFIG, providers=providers)


def test_the_default_report_stays_out_of_the_commit(tmp_path: Path) -> None:
    """Without report_path, the run still knows the report and leaves it uncommitted,
    in a repo that does not ignore it."""
    r = tmp_path / "plain-repo"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(r, "init", "-q")
    git(r, "add", ".")
    git(r, "commit", "-qm", "seed")
    out = start(r, FIX)
    assert out.report is not None and out.report.is_file()
    assert out.copy is not None and out.branch is not None
    files = git(out.copy, "show", "--name-only", "--format=", out.branch).split()
    assert files == ["calc.py"]


# ---- approvals from another thread ----


def test_approvals_answered_from_another_thread() -> None:
    import threading

    from timu.roles import CODER
    from timu.types import Artifact

    seen: list[api.ApprovalRequest] = []
    approvals = api.Approvals(on_request=seen.append)
    got: list[bool] = []
    worker = threading.Thread(
        target=lambda: got.append(approvals(Artifact("research", "c"), CODER))
    )
    worker.start()
    while not approvals.pending():
        threading.Event().wait(0.01)
    (request,) = approvals.pending()
    assert (request.artifact.name, request.role) == ("research", "coder")
    assert seen == [request]
    assert approvals.answer(request.id, True)
    worker.join(5)
    assert got == [True] and approvals.pending() == []
    assert not approvals.answer(request.id, False)  # already answered


def test_approvals_say_no_on_cancel_or_timeout() -> None:
    import threading

    from timu.roles import CODER
    from timu.types import Artifact

    cancel = threading.Event()
    cancel.set()
    assert api.Approvals(cancel)(Artifact("a", "c"), CODER) is False
    timed = api.Approvals(timeout=0.1, poll=0.01)
    assert timed(Artifact("a", "c"), CODER) is False
    assert timed.pending() == []


def test_a_waiting_approval_does_not_block_other_events(tmp_path: Path) -> None:
    """Events from other threads flow while a question waits for the UI."""
    import threading

    from timu.roles import CODER
    from timu.types import Artifact, Origin, Task
    from timu.workflow import Session

    events: list[Event] = []
    asked = threading.Event()
    approvals = api.Approvals(on_request=lambda r: asked.set())
    session = Session(lambda role: FakeProvider([]), events.append, approve=approvals)
    run = session.at(tmp_path)
    web = Artifact("w", "c", origin=Origin.NET)
    worker = threading.Thread(target=lambda: run.run_agent(CODER, Task("x", (web,))))
    worker.start()
    assert asked.wait(5)
    session.emit("warning", message="still flowing")  # would block on a held lock
    assert events[-1].data == {"message": "still flowing"}
    (request,) = approvals.pending()
    approvals.answer(request.id, False)
    worker.join(5)
    assert [e.data["approved"] for e in events if e.kind == "approval"] == [False]
