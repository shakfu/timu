"""Regressions from the 2026-11-01 review: each test holds one finding's reproducer."""

from __future__ import annotations

import email.message
import io
import json
import os
import shlex
import subprocess
import threading
import time
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from timu import Agent, Budget, Event, Role, Task
from timu.cli import ConsoleSink, main, printable
from timu.graph import (
    bare_repos,
    extract,
    head,
    parse_graph,
    provision,
    resumable,
    seal,
    spec,
)
from timu.provider import openai
from timu.provider.base import Message
from timu.provider.fake import FakeProvider, call, calls, text
from timu.provider.openai import OpenAIProvider
from timu.sandbox import MacSandbox, NoSandbox, Policy, detect
from timu.tool import PROTECTED, Context
from timu.tools.fs import READ, SEARCH
from timu.tools.shell import refusal, run_sh, scrubbed_env
from timu.tools.web import WebError, _get
from timu.workflow import WORKFLOW_BUDGET, Session, check_files, check_fix_review

mac_only = pytest.mark.skipif(
    not isinstance(detect(), MacSandbox), reason="needs macOS sandbox-exec"
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "a.txt").write_text("one\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "first")
    return Path(os.path.realpath(path))


def sh(ws: Path, cmd: str, sandbox: Any = None) -> Any:
    tmp = ws.parent / f"{ws.name}-tmp"
    tmp.mkdir(exist_ok=True)
    return run_sh(
        cmd,
        cwd=ws,
        sandbox=sandbox or NoSandbox(),
        policy=Policy(
            (ws,), (ws, tmp), tuple(ws / p for p in PROTECTED), scratch=(tmp,)
        ),
        env={**os.environ, **scrubbed_env(tmp)},
        timeout=10,
        cancel=threading.Event(),
        max_output=4096,
    )


# ---- the copy (F1, F2, F3) ----


def run_cli(tmp_path: Path, argv: list[str], replies: list[Any]) -> tuple[int, str]:
    toml = tmp_path / "timu.toml"
    toml.write_text('[provider]\nmodel = "m"\napi_key_env = ""\n')
    provider = FakeProvider(replies)
    err = io.StringIO()
    code = main(
        ["run", "--config", str(toml), "--unsafe-no-sandbox", *argv],
        provider_for=lambda role: provider,
        out=io.StringIO(),
        err=err,
    )
    return code, err.getvalue()


WRITE_THEN_APPROVE = [
    calls(call("write", path="a.txt", content="two\n")),
    text("wrote it"),
    text("VERDICT: APPROVE\nfine"),
]


def test_run_leaves_the_checkout_alone_and_offers_a_branch(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    code, err = run_cli(tmp_path, ["-C", str(r), "change a"], WRITE_THEN_APPROVE)
    assert code == 0, err
    assert (r / "a.txt").read_text() == "one\n"
    assert not (r / "REVIEW.md").exists()
    fetch = next(x for x in err.splitlines() if " fetch " in x).removeprefix("timu: ")
    subprocess.run(shlex.split(fetch), check=True, capture_output=True)
    branch = fetch.rsplit(":", 1)[1]
    assert git(r, "show", f"{branch}:a.txt") == "two\n"
    report = next(x for x in err.splitlines() if x.startswith("timu: report "))
    assert Path(report.removeprefix("timu: report ")).read_text().startswith("VERDICT")
    assert "REVIEW.md" not in git(r, "ls-tree", "-r", "--name-only", branch)


def test_run_in_a_subdirectory_keeps_its_place(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    (r / "sub").mkdir()
    (r / "sub" / "b.txt").write_text("b\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "sub")
    replies = [
        calls(call("write", path="b.txt", content="c\n")),
        text("wrote it"),
        text("VERDICT: APPROVE\nfine"),
    ]
    code, err = run_cli(tmp_path, ["-C", str(r / "sub"), "change b"], replies)
    assert code == 0, err
    fetch = next(x for x in err.splitlines() if " fetch " in x).removeprefix("timu: ")
    subprocess.run(shlex.split(fetch), check=True, capture_output=True)
    branch = fetch.rsplit(":", 1)[1]
    names = git(r, "ls-tree", "-r", "--name-only", branch).split()
    assert names == ["a.txt", "sub/b.txt"]  # the report stays out of the commit
    assert git(r, "show", f"{branch}:sub/b.txt") == "c\n"


def test_run_outside_a_repo_needs_in_place(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    code, err = run_cli(tmp_path, ["-C", str(d), "x"], [])
    assert code == 2
    assert "not in a git repo; use --in-place" in err


def test_run_warns_that_uncommitted_changes_stay_behind(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    (r / "a.txt").write_text("dirty\n")
    code, err = run_cli(tmp_path, ["-C", str(r), "x"], WRITE_THEN_APPROVE)
    assert code == 0, err
    assert "uncommitted changes" in err
    assert (r / "a.txt").read_text() == "dirty\n"


def test_in_place_still_edits_the_checkout(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    code, err = run_cli(tmp_path, ["--in-place", "-C", str(r), "x"], WRITE_THEN_APPROVE)
    assert code == 0, err
    assert (r / "a.txt").read_text() == "two\n"


def session(tmp_path: Path) -> Session:
    return Session(lambda role: FakeProvider([]), lambda e: None)


def test_seal_refuses_a_bare_repo_layout(tmp_path: Path) -> None:
    src = repo(tmp_path / "src")
    ws = tmp_path / "ws"
    base, _ = provision(src, ws, "HEAD", "b")
    git(ws, "init", "-q", "--bare", "vendor")
    assert bare_repos(ws) == ["vendor"]
    with pytest.raises(ValueError, match="bare git repos at vendor"):
        seal(session(tmp_path).at(ws), ws, base, "b", "m", (), "failed")
    assert head(ws) == base


def test_moving_a_nested_repo_keeps_its_history(tmp_path: Path) -> None:
    ws = repo(tmp_path / "ws")
    repo(ws / "sub")
    ran = sh(ws, "mv sub sub2")
    assert ran.code == 0
    assert not ran.created
    assert [p.name for p in ran.moved] == [".git"]
    assert "first" in git(ws / "sub2", "log", "--format=%s")


def test_a_new_git_dir_is_quarantined_with_its_history(tmp_path: Path) -> None:
    ws = repo(tmp_path / "ws")
    ran = sh(
        ws,
        "git init -q fresh && cd fresh && touch f && git add f && "
        "git -c user.name=t -c user.email=t@e commit -qm work",
    )
    assert [p.parent.name for p in ran.created] == ["fresh"]
    assert not (ws / "fresh" / ".git").exists()
    assert not ran.left
    assert ran.quarantine is not None
    assert ran.quarantine.parent == tmp_path / "state" / "timu" / "quarantine"
    assert ran.quarantine.stat().st_mode & 0o777 == 0o700
    kept = ran.quarantine / (ws / "fresh" / ".git").relative_to("/")
    assert "work" in git(kept, "log", "--format=%s")


def test_no_quarantine_inside_a_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = repo(tmp_path / "ws")
    monkeypatch.setenv("XDG_STATE_HOME", str(ws / "state"))
    ran = sh(ws, "git init -q fresh")
    assert ran.quarantine is None
    assert ran.left == ran.created
    assert "could not move fresh/.git" in refusal(ran, ws)


# ---- review snapshot (F4) ----


@mac_only
def test_detached_writes_after_review_fail_the_seal(tmp_path: Path) -> None:
    src = repo(tmp_path / "src")
    ws = tmp_path / "ws"
    base, _ = provision(src, ws, "HEAD", "b")
    run = session(tmp_path).at(ws)
    perl = (
        "use POSIX; exit if fork; POSIX::setsid(); sleep 1; "
        'open(F,">late.txt"); print F "late"; close F;'
    )
    sh(run.workdir, f"perl -e {shlex.quote(perl)} </dev/null >/dev/null 2>&1")
    run.mark_reviewed()
    time.sleep(2)
    assert (ws / "late.txt").exists()
    with pytest.raises(ValueError, match="changed after the review"):
        seal(run, run.workdir, base, "b", "m", (), "done")
    assert head(ws) == base


def test_the_report_is_not_part_of_the_reviewed_tree(tmp_path: Path) -> None:
    src = repo(tmp_path / "src")
    ws = tmp_path / "ws"
    base, _ = provision(src, ws, "HEAD", "b")
    run = session(tmp_path).at(ws)
    run.mark_reviewed("REVIEW.md")
    (ws / "REVIEW.md").write_text("VERDICT: APPROVE\n")
    assert not run.changed_after_review()
    (ws / "a.txt").write_text("changed\n")
    assert run.changed_after_review()
    assert seal(run, run.workdir, base, "b", "m", ("REVIEW.md",), "failed") != base


# ---- sandbox (F5) ----


@mac_only
def test_clipboard_is_denied(tmp_path: Path) -> None:
    ws = repo(tmp_path / "ws")
    home = Path(os.path.realpath(os.path.expanduser("~")))
    ran = sh(ws, "pbpaste | wc -c", MacSandbox(home))
    assert ran.text.split()[-1] == "0"


# ---- graph (F6, F7, F8) ----


def test_a_non_utf8_diff_does_not_raise(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    base = head(r)
    (r / "a.txt").write_bytes(b"caf\xe9\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "latin-1")
    assert "caf" in extract("git:diff", r, base)


NODE = '[nodes.a]\nrepo = "a"\nworkflow = "commands"\nobjective = "o"\n'
NODE += 'params = { steps = ["true"] }\n'


def test_a_graph_can_set_its_total_budget(tmp_path: Path) -> None:
    import tomllib

    g = parse_graph(tomllib.loads("[budget]\ntokens = 50000000\nturns = 5000\n" + NODE))
    assert g.budget is not None
    assert (g.budget.tokens, g.budget.turns) == (50_000_000, 5000)
    assert g.budget.tool_calls == WORKFLOW_BUDGET.tool_calls


def test_resume_reruns_a_node_whose_project_moved_on(tmp_path: Path) -> None:
    import tomllib

    r = repo(tmp_path / "a")
    g = parse_graph(tomllib.loads(NODE))
    copy = tmp_path / "copy"
    base, _ = provision(r, copy, "HEAD", "b")
    data = {
        "node": "a",
        "status": "done",
        "summary": "s",
        "branch": "b",
        "base": base,
        "head": base,
        "workdir": str(copy),
        "files": "",
        "spec": spec(g.nodes["a"], r),
        "outputs": [],
    }
    events = [Event("node_result", "r", "", "workflow", 0.0, data)]
    assert list(resumable(events, g, {"a": r})) == ["a"]
    git(r, "commit", "--allow-empty", "-qm", "newer")
    assert list(resumable(events, g, {"a": r})) == []


# ---- check files (F9) ----


@pytest.mark.parametrize("command", ["python check.py", "sh check.py"])
def test_a_script_named_without_a_slash_counts(tmp_path: Path, command: str) -> None:
    (tmp_path / "check.py").write_text("")
    workdir = Path(os.path.realpath(tmp_path))
    assert check_files([command], workdir) == ("check.py",)


def test_cargo_and_go_have_check_files(tmp_path: Path) -> None:
    assert "Cargo.toml" in check_files(["cargo test"], tmp_path)
    assert "go.mod" in check_files(["go test ./..."], tmp_path)


# ---- credentials over redirects (F10) ----


@pytest.fixture
def servers() -> Iterator[tuple[int, dict[str, Any]]]:
    """A server that redirects to another origin, and what that origin received."""
    seen: dict[str, Any] = {}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.update({k.lower(): v for k, v in self.headers.items()})
            seen["hit"] = True
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def _go(self) -> None:
            self.send_response(302)
            self.send_header("location", f"http://localhost:{target.server_port}/x")
            self.end_headers()

        do_GET = _go
        do_POST = _go

        def log_message(self, *args: Any) -> None:
            pass

    redirect = HTTPServer(("127.0.0.1", 0), Redirect)
    for s in (target, redirect):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield redirect.server_port, seen
    target.shutdown()
    redirect.shutdown()


def test_provider_does_not_follow_redirects(
    servers: tuple[int, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, seen = servers
    p = OpenAIProvider(f"http://127.0.0.1:{port}/v1", "m", "SECRET", retries=0)
    reply = p.complete([Message("user", "hi")], [])
    assert reply.stop == "error"
    assert "http 302" in reply.error
    assert "hit" not in seen


def test_provider_survives_a_reset_while_reading_an_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reset(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise ConnectionResetError(54, "Connection reset by peer")

    def open_(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            req.full_url, 503, "busy", email.message.Message(), Reset()
        )

    monkeypatch.setattr(openai._OPENER, "open", open_)
    p = OpenAIProvider("http://127.0.0.1:1/v1", "m", "SECRET", retries=0)
    reply = p.complete([Message("user", "hi")], [])
    assert reply.stop == "error"
    assert reply.error.startswith("http 503")


def test_web_drops_credentials_on_a_cross_origin_redirect(
    servers: tuple[int, dict[str, Any]],
) -> None:
    port, seen = servers
    _get(f"http://127.0.0.1:{port}/", {"x-subscription-token": "SECRET"}, True)
    assert seen["hit"]
    assert "x-subscription-token" not in seen


# ---- console (F11, F12) ----


def test_a_lone_surrogate_reaches_a_strict_stream() -> None:
    buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    delta = Event("model_delta", "a", "", "coder", 0.0, {"text": "\ud83d"})
    ConsoleSink(buf, io.StringIO())(delta)
    buf.flush()
    assert printable("\ud83d") == "\\ud83d"


def test_trace_output_escapes_control_characters(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    event = {
        "kind": "workflow_result",
        "agent_id": "r",
        "parent_id": "",
        "role": "workflow",
        "ts": 0.0,
        "data": {"status": "done", "summary": "x\x1b[2Jy"},
    }
    trace.write_text(json.dumps(event) + "\n")
    out = io.StringIO()
    assert main(["trace", str(trace)], out=out, err=io.StringIO()) == 0
    assert "\x1b" not in out.getvalue()
    assert "\\x1b" in out.getvalue()


# ---- rate limits (F13) ----


def test_a_rate_limit_with_token_wording_is_retried() -> None:
    class Limit(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = b'{"message":"Too many tokens, please wait"}'
            self.send_response(429)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Limit)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    naps: list[float] = []
    p = OpenAIProvider(
        f"http://127.0.0.1:{server.server_port}/v1", "m", retries=2, sleep=naps.append
    )
    reply = p.complete([Message("user", "hi")], [])
    server.shutdown()
    assert len(naps) == 2
    assert "http 429" in reply.error


# ---- engine-side limits (F14) ----


def fs_ctx(root: Path, cancel: threading.Event | None = None) -> Context:
    real = Path(os.path.realpath(root))
    return Context(real, cancel or threading.Event(), read_roots=(real,))


def test_read_refuses_a_huge_file(tmp_path: Path) -> None:
    with open(tmp_path / "big", "wb") as f:
        f.truncate(17 * 1024 * 1024)
        f.seek(0)
        f.write(b"a")
    out = READ.run(fs_ctx(tmp_path), {"path": "big"})
    assert out.is_error
    assert "larger than 16 MB" in out.text


def test_a_slow_regex_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("timu.tools.fs.SEARCH_TIMEOUT", 0.3)
    (tmp_path / "f.txt").write_text("a" * 40 + "!\n")
    start = time.monotonic()
    out = SEARCH.run(fs_ctx(tmp_path), {"pattern": r"^(a+)+$"})
    assert out.is_error
    assert "timed out" in out.text
    assert time.monotonic() - start < 5


def test_cancel_stops_a_slow_regex(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a" * 40 + "!\n")
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    out = SEARCH.run(fs_ctx(tmp_path, cancel), {"pattern": r"^(a+)+$"})
    assert out.is_error
    assert "cancelled" in out.text


def test_search_does_not_import_workspace_modules(tmp_path: Path) -> None:
    marker = tmp_path / "imported"
    (tmp_path / "json.py").write_text(f"open({str(marker)!r}, 'w')\n")
    (tmp_path / "re.py").write_text(f"open({str(marker)!r}, 'w')\n")
    monkey = SEARCH.run(fs_ctx(tmp_path), {"pattern": "open"})
    assert not monkey.is_error
    assert not marker.exists()


# ---- state (F15) ----


def test_state_directory_is_private(tmp_path: Path) -> None:
    r = repo(tmp_path / "r")
    code, err = run_cli(tmp_path, ["-C", str(r), "x"], WRITE_THEN_APPROVE)
    assert code == 0, err
    assert (tmp_path / "state" / "timu").stat().st_mode & 0o777 == 0o700


def test_clean_lists_until_told_to_delete(tmp_path: Path) -> None:
    state = tmp_path / "state" / "timu"
    run = "20200101-000000-abcd"
    (state / "work" / run / "run").mkdir(parents=True)
    (state / "runs").mkdir()
    (state / "quarantine" / run / "ws" / ".git").mkdir(parents=True)
    trace = state / "runs" / f"{run}.jsonl"
    trace.write_text("")
    keep = state / "runs" / "keep-me.jsonl"  # not named like a run
    keep.write_text("")
    fresh = state / "runs" / "20990101-000000-abcd.jsonl"
    fresh.write_text("")
    old = time.time() - 40 * 86400
    for p in (state / "work" / run, trace, keep, state / "quarantine" / run):
        os.utime(p, (old, old))

    out = io.StringIO()
    assert main(["clean", "--older-than", "30"], out=out) == 0
    assert f"would remove {trace}" in out.getvalue()
    assert trace.exists()

    out = io.StringIO()
    assert main(["clean", "--older-than", "30", "--yes"], out=out) == 0
    assert not trace.exists()
    assert not (state / "work" / run).exists()
    assert not (state / "quarantine" / run).exists()
    assert keep.exists()
    assert fresh.exists()


# ---- smaller gaps: resume reasons, check_paths, deadlines ----


def test_resume_says_why_a_node_runs_again(tmp_path: Path) -> None:
    import tomllib

    r = repo(tmp_path / "a")
    g = parse_graph(tomllib.loads(NODE))
    why: dict[str, str] = {}
    assert resumable([], g, {"a": r}, why) == {}
    assert why == {"a": "it did not finish done in that run"}


def test_check_paths_fail_a_fix_that_edits_a_test(tmp_path: Path) -> None:
    ws = repo(tmp_path / "ws")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_a.py").write_text("x = 1\n")
    replies = [
        calls(call("write", path="tests/test_a.py", content="x = 2\n")),
        text("edited the test"),
        text("VERDICT: APPROVE\nfine"),
    ]
    provider = FakeProvider(replies)
    ses = Session(lambda role: provider, lambda e: None, sandbox=NoSandbox())
    result = check_fix_review(
        ses.at(ws), "o", check=["false"], check_paths=("tests/*",)
    )
    assert result.status == "failed"
    assert "tests/test_a.py" in result.summary


class Stalled:
    """A provider whose reply never arrives."""

    model = "m"

    def complete(self, messages: Any, tools: Any, on_text: Any = None) -> Any:
        threading.Event().wait()


def test_cancel_interrupts_a_stalled_model_call(tmp_path: Path) -> None:
    cancel = threading.Event()
    agent = Agent(Role("r", "p"), Stalled(), lambda e: None, tmp_path, cancel=cancel)
    threading.Timer(0.1, cancel.set).start()
    assert agent.run(Task("g")).status == "cancelled"


def test_wall_limit_interrupts_a_stalled_model_call(tmp_path: Path) -> None:
    agent = Agent(Role("r", "p"), Stalled(), lambda e: None, tmp_path)
    result = agent.run(Task("g", budget=Budget(wall_seconds=0.2)))
    assert result.status == "budget"
    assert "wall_seconds" in result.summary


def test_a_provider_error_still_reaches_the_caller(tmp_path: Path) -> None:
    class Broken:
        model = "m"

        def complete(self, messages: Any, tools: Any, on_text: Any = None) -> Any:
            raise RuntimeError("boom")

    agent = Agent(Role("r", "p"), Broken(), lambda e: None, tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        agent.run(Task("g"))


def test_a_trickling_response_hits_the_total_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            for _ in range(200):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.05)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr("timu.tools.web.TOTAL_TIMEOUT", 0.2)
    start = time.monotonic()
    with pytest.raises(WebError, match="took over"):
        _get(f"http://127.0.0.1:{server.server_port}/", {}, True)
    assert time.monotonic() - start < 5
    server.shutdown()
