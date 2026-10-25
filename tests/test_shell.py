"""Tests for the shell tool and the macOS sandbox (design 3, 4.3)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from timu import Agent, Capability, Context, Role, RoleError, Task, ToolOutput
from timu.provider.fake import FakeProvider, call, calls, text
from timu.role import make_context
from timu.sandbox import MacSandbox, NoSandbox, Policy, Sandbox, detect, mac_profile
from timu.skills import find
from timu.tools.fs import EDIT, READ, WRITE
from timu.tools.shell import SHELL, Ran, refusal, run_sh

mac_only = pytest.mark.skipif(
    not isinstance(detect(), MacSandbox), reason="needs macOS sandbox-exec"
)


def _online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
    except OSError:
        return False
    return True


needs_network = pytest.mark.skipif(
    not _online(), reason="no network: a sandbox denial would be indistinguishable"
)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(workspace, private tmp, outside), all resolved."""
    real = Path(os.path.realpath(tmp_path))
    for d in ("ws", "tmp", "outside"):
        (real / d).mkdir()
    return real / "ws", real / "tmp", real / "outside"


def ctx_for(
    dirs: tuple[Path, Path, Path],
    sandbox: Sandbox | None = None,
    writable: bool = True,
    **kw: Any,
) -> Context:
    ws, tmp, _ = dirs
    return Context(
        workdir=ws,
        cancel=kw.pop("cancel", threading.Event()),
        read_roots=(ws,),
        write_roots=(ws,) if writable else (),
        sandbox=sandbox or detect() or NoSandbox(),
        tmp=tmp,
        **kw,
    )


def sh(ctx: Context, command: str, **args: Any) -> ToolOutput:
    return SHELL.run(ctx, {"command": command, **args})


# ---- the tool, on any backend ----


def test_new_git_and_timu_entries_are_removed(dirs: tuple[Path, Path, Path]) -> None:
    ws, tmp, _ = dirs
    (ws / "vendor" / ".git").mkdir(parents=True)  # existing: kept
    ctx = ctx_for(dirs, NoSandbox())
    out = sh(ctx, "git init -q sub && echo x > timu.toml && mkdir .timu && echo ok")
    assert out.is_error
    first, rest = out.text.split("\n", 1)
    assert first == (
        "refused: commands may create .git only under $TMPDIR, and never "
        "timu.toml or .timu; created timu.toml, .timu, sub/.git; removed"
    )
    assert rest == "ok\n[exit 0]"
    assert not (ws / "timu.toml").exists() and not (ws / ".timu").exists()
    assert not (ws / "sub" / ".git").exists() and (ws / "sub").is_dir()
    assert (ws / "vendor" / ".git").is_dir()
    assert not sh(ctx, 'git init -q "$TMPDIR/clone"').is_error  # scratch
    assert (tmp / "clone" / ".git").is_dir()


def test_a_hidden_directory_is_still_scanned(dirs: tuple[Path, Path, Path]) -> None:
    ws = dirs[0]
    (ws / "own").mkdir(mode=0o300)  # the user's, unreadable before the command
    out = sh(ctx_for(dirs, NoSandbox()), "git init -q a/b && chmod 000 a/b a")
    assert out.is_error and "created a/b/.git; removed" in out.text
    assert (ws / "own").stat().st_mode & 0o777 == 0o300  # restored
    assert (ws / "a").stat().st_mode & 0o777 == 0
    for d in ("a", "a/b", "own"):  # so stat and pytest's cleanup can enter
        os.chmod(ws / d, 0o700)
    assert not (ws / "a" / "b" / ".git").exists()


def test_existing_git_entries_are_read_only(dirs: tuple[Path, Path, Path]) -> None:
    ws, tmp, _ = dirs
    (ws / ".git").mkdir()
    (ws / "vendor" / ".git").mkdir(parents=True)
    (ws / "sub").mkdir()
    (ws / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
    policies: list[Policy] = []

    class Recorder:
        def wrap(self, argv: Sequence[str], policy: Policy) -> list[str]:
            policies.append(policy)
            return list(argv)

    run_sh(
        "true",
        cwd=ws,
        sandbox=Recorder(),
        policy=Policy((ws,), (ws, tmp), (ws / "timu.toml",), scratch=(tmp,)),
        env={},
        timeout=5,
        cancel=threading.Event(),
        max_output=1024,
    )
    assert policies[0].deny_write == (
        ws / "timu.toml",
        ws / ".git",
        ws / "sub" / ".git",
        ws / "vendor" / ".git",
    )


def test_refusal_names_what_was_left() -> None:
    ran = Ran("", 0, created=(Path("/w/.timu"),), left=(Path("/w/.timu"),))
    assert refusal(ran, Path("/w")).endswith(
        "could not remove .timu, delete it before running git or timu"
    )


def test_output_and_exit_status(dirs: tuple[Path, Path, Path]) -> None:
    ctx = ctx_for(dirs)
    out = sh(ctx, "echo out; echo err >&2")
    assert (out.text, out.is_error) == ("out\nerr\n[exit 0]", False)
    out = sh(ctx, "exit 3")
    assert (out.text, out.is_error) == ("[exit 3]", True)
    assert sh(ctx, "printf abc").text == "abc\n[exit 0]"


def test_signal_exit_status(dirs: tuple[Path, Path, Path]) -> None:
    assert sh(ctx_for(dirs), "kill -TERM $$").text == "[exit 143]"


def test_bad_arguments(dirs: tuple[Path, Path, Path]) -> None:
    ctx = ctx_for(dirs)
    assert sh(ctx, "  ").is_error
    assert SHELL.run(ctx, {"command": 1}).is_error


def test_no_sandbox_means_no_shell(dirs: tuple[Path, Path, Path]) -> None:
    ctx = Context(workdir=dirs[0], cancel=threading.Event())
    assert "no sandbox" in sh(ctx, "true").text


def test_output_is_bounded(dirs: tuple[Path, Path, Path]) -> None:
    out = sh(ctx_for(dirs, max_output=100), "seq 1 100000").text
    assert out.startswith("1\n2\n")
    assert "bytes omitted" in out
    assert out.endswith("99999\n100000\n[exit 0]")
    assert len(out) < 200


def test_env_is_scrubbed(
    dirs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIMU_TEST_API_KEY", "sk-secret")
    ctx = ctx_for(dirs)
    out = sh(ctx, "env").text
    assert "sk-secret" not in out
    assert f"HOME={dirs[1]}" in out
    assert f"TMPDIR={dirs[1]}/" in out


def test_read_only_workspace_redirects_caches(dirs: tuple[Path, Path, Path]) -> None:
    out = sh(ctx_for(dirs, writable=False), "env").text
    assert f"PYTHONPYCACHEPREFIX={dirs[1]}/pycache" in out
    assert "PYTEST_ADDOPTS=-p no:cacheprovider" in out
    assert "PYTEST_ADDOPTS" not in sh(ctx_for(dirs), "env").text


def test_timeout_kills_grandchildren(dirs: tuple[Path, Path, Path]) -> None:
    sleeper = _unique_sleep()
    start = time.monotonic()
    out = sh(ctx_for(dirs), f"sh -c '{sleeper} & wait'", timeout=1)
    assert time.monotonic() - start < 4
    assert out.is_error
    assert "timed out after 1s; killed" in out.text
    _assert_gone(sleeper)


def test_background_job_does_not_block(dirs: tuple[Path, Path, Path]) -> None:
    sleeper = _unique_sleep()
    start = time.monotonic()
    out = sh(ctx_for(dirs), f"{sleeper} & echo hi")
    assert time.monotonic() - start < 3
    assert out.text == "hi\n[exit 0]"
    _assert_gone(sleeper)


def test_work_after_closing_the_output_still_runs(
    dirs: tuple[Path, Path, Path],
) -> None:
    """EOF on the pipe does not mean sh has exited."""
    out = sh(ctx_for(dirs), "exec >/dev/null 2>&1; sleep 0.3; touch done; exit 3")
    assert out.text == "[exit 3]"
    assert (dirs[0] / "done").exists()


def test_cancel_stops_within_a_second(dirs: tuple[Path, Path, Path]) -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    out = sh(ctx_for(dirs, cancel=cancel), "sleep 30")
    assert time.monotonic() - start < 1.3
    assert "cancelled; killed" in out.text


def test_timeout_argument(dirs: tuple[Path, Path, Path]) -> None:
    ctx = ctx_for(dirs, shell_timeout=1)
    assert "timed out after 1s" in sh(ctx, "sleep 5").text
    assert "timed out after 1s" in sh(ctx, "sleep 5", timeout="x").text
    assert "timed out after 1s" in sh(ctx, "sleep 5", timeout=0).text


def _unique_sleep() -> str:
    return f"sleep 30.{uuid.uuid4().int % 10**9}"


def _assert_gone(cmdline: str) -> None:
    # Match by command line: under bwrap --unshare-pid, $! is a namespace pid.
    for _ in range(20):  # the kernel may take a moment to reap
        if subprocess.run(
            ["pgrep", "-f", cmdline], capture_output=True, check=False
        ).returncode:
            return
        time.sleep(0.05)
    pytest.fail(f"{cmdline!r} survived")


# ---- the macOS sandbox ----


@mac_only
@needs_network
@pytest.mark.parametrize(
    "command",
    [
        "curl -s -m 5 https://example.com",
        f"{sys.executable} -c 'import socket; socket.create_connection((\"1.1.1.1\", 53), 3)'",
        f"{sys.executable} -c 'import socket; socket.getaddrinfo(\"example.com\", 80)'",
    ],
)
def test_sandbox_denies_network(dirs: tuple[Path, Path, Path], command: str) -> None:
    assert sh(ctx_for(dirs, MacSandbox(extra_read=_python_paths())), command).is_error


@mac_only
def test_sandbox_writes(dirs: tuple[Path, Path, Path]) -> None:
    ws, _, outside = dirs
    (ws / ".git").mkdir()
    ctx = ctx_for(dirs)
    assert not sh(ctx, "echo x > a.txt && mkdir -p sub && echo y > sub/b.txt").is_error
    assert not sh(ctx, 'echo x > "$TMPDIR/t"').is_error
    assert sh(ctx, f"echo x > {outside}/o.txt").is_error
    assert sh(ctx, "echo x > .git/hooks-pre-commit").is_error
    assert not (outside / "o.txt").exists()
    assert not (ws / ".git" / "hooks-pre-commit").exists()
    for cmd in ("echo x > timu.toml", "echo x > TIMU.toml", "mkdir -p .timu/skills"):
        assert sh(ctx, cmd).is_error, cmd
    assert not (ws / "timu.toml").exists()
    assert not (ws / ".timu").exists()


@mac_only
def test_sandbox_reviewer_cannot_write_workspace_or_report(
    dirs: tuple[Path, Path, Path],
) -> None:
    ws = dirs[0]
    (ws / "REVIEW.md").write_text("old\n")
    ctx = ctx_for(dirs, writable=False, write_files=(ws / "REVIEW.md",))
    assert sh(ctx, "echo x > REVIEW.md").is_error
    assert sh(ctx, "echo x > new.txt").is_error
    assert not sh(ctx, 'echo x > "$TMPDIR/scratch" && cat REVIEW.md').is_error
    assert (ws / "REVIEW.md").read_text() == "old\n"
    assert not (ws / "new.txt").exists()


@mac_only
def test_sandbox_hides_home(dirs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    home = Path(os.path.realpath(tmp_path)) / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY\n")
    (home / "proj").mkdir()
    (home / "proj" / "f.txt").write_text("visible\n")
    ctx = Context(
        workdir=home / "proj",
        cancel=threading.Event(),
        read_roots=(home / "proj",),
        write_roots=(home / "proj",),
        sandbox=MacSandbox(home=home),
        tmp=dirs[1],
    )
    assert sh(ctx, f"cat {home}/.ssh/id_ed25519").is_error
    assert sh(ctx, f"ls {home}").is_error
    assert sh(ctx, "cat f.txt && pwd").text == f"visible\n{home}/proj\n[exit 0]"


@mac_only
def test_pytest_in_reviewer_sandbox_leaves_workspace_clean(
    dirs: tuple[Path, Path, Path],
) -> None:
    ws = dirs[0]
    (ws / "test_x.py").write_text("def test_x():\n    assert 1 + 1 == 2\n")
    ctx = ctx_for(dirs, MacSandbox(extra_read=_python_paths()), writable=False)
    out = sh(ctx, f"{sys.executable} -m pytest -q test_x.py", timeout=60)
    assert not out.is_error, out.text
    assert "1 passed" in out.text
    assert sorted(p.name for p in ws.iterdir()) == ["test_x.py"]


def _python_paths() -> tuple[Path, ...]:
    """This interpreter's install and venv, which may live under HOME."""
    return tuple({Path(os.path.realpath(p)) for p in (sys.prefix, sys.base_prefix)})


def test_profile_rule_order() -> None:
    """In SBPL the last matching rule wins: each deny must precede its allows."""
    home = Path("/Users/u")
    policy = Policy((home / "p",), (home / "p", Path("/tmp/t")))
    lines = mac_profile(policy, home).splitlines()
    assert lines.index("(deny file-write*)") < next(
        i for i, s in enumerate(lines) if s.startswith("(allow file-write*")
    )
    deny_home = lines.index('(deny file-read* (subpath "/Users/u"))')
    assert deny_home < next(
        i for i, s in enumerate(lines) if s.startswith("(allow file-read* ")
    )
    assert lines.index('(allow file-read-metadata (subpath "/Users/u"))') > deny_home
    assert "file-read" not in mac_profile(policy, None)  # no home, no read rules


def test_profile_denies_after_allowing_writes() -> None:
    policy = Policy((), (Path("/w"),), (Path("/w/timu.toml"),))
    lines = mac_profile(policy, None).splitlines()
    assert lines[-1] == '(deny file-write* (subpath "/w/timu.toml"))'


def test_profile_allows_git_only_in_scratch() -> None:
    policy = Policy(
        (), (Path("/w"), Path("/t")), (Path("/w/.git"),), scratch=(Path("/t"),)
    )
    lines = mac_profile(policy, None).splitlines()
    deny_git = lines.index('(deny file-write* (regex #"/\\.git(/|$)"))')
    assert lines[deny_git + 1] == '(allow file-write* (subpath "/t"))'
    assert lines[deny_git + 2] == '(deny file-write* (subpath "/w/.git"))'


def test_profile_quotes_paths() -> None:
    profile = mac_profile(Policy((), (Path('/x"y'),)), None)
    assert '(subpath "/x\\"y")' in profile


# ---- roles and the agent ----

R, W, X = Capability.FS_READ, Capability.FS_WRITE, Capability.EXEC


def test_exec_role_without_backend_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("timu.role.detect", lambda: None)
    role = Role("coder", "", (SHELL,), frozenset({X}))
    with pytest.raises(RoleError, match="exec needs a sandbox"):
        make_context(role, tmp_path, threading.Event())
    assert isinstance(
        make_context(role, tmp_path, threading.Event(), NoSandbox()).sandbox, NoSandbox
    )


def test_sandbox_dropped_without_exec(tmp_path: Path) -> None:
    role = Role("lead", "", (READ,), frozenset({R}))
    assert make_context(role, tmp_path, threading.Event(), NoSandbox()).sandbox is None


def test_agent_warns_without_sandbox(tmp_path: Path) -> None:
    role = Role("coder", "", (SHELL,), frozenset({X}))
    events: list[Any] = []
    provider = FakeProvider([calls(call("shell", command="echo $TMPDIR")), text("ok")])
    Agent(role, provider, events.append, tmp_path, sandbox=NoSandbox()).run(Task("x"))
    assert [e.kind for e in events][:2] == ["start", "warning"]
    tmp = provider.requests[-1].messages[3].content.split("\n")[0].rstrip("/")
    assert "timu-" in tmp
    assert not Path(tmp).exists()  # removed when the run ended


@mac_only
@needs_network
def test_coder_runs_make_test_without_network(tmp_path: Path) -> None:
    """Phase 3 exit: a coder runs `make test` in a temp repo through the sandbox,
    and cannot reach the network."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("test:\n\tpython3 -m unittest -q\n")
    (repo / "test_calc.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(1 + 1, 2)\n"
    )
    role = Role(
        "coder",
        "",
        (READ, WRITE, EDIT, SHELL),
        frozenset({R, W, X}),
        write_roots=(".",),
    )
    provider = FakeProvider(
        [
            calls(call("shell", id="c1", command="make test")),
            calls(call("shell", id="c2", command="curl -s -m 5 https://example.com")),
            text("done"),
        ]
    )
    # python3 on PATH may be a venv under HOME; a real config lists it the same way.
    sandbox = MacSandbox(extra_read=_python_paths())
    result = Agent(role, provider, lambda e: None, repo, sandbox=sandbox).run(
        Task("run the tests")
    )
    assert result.status == "done"
    tool_msgs = [m for m in provider.requests[-1].messages if m.role == "tool"]
    assert not tool_msgs[0].is_error, tool_msgs[0].content
    assert "OK" in tool_msgs[0].content
    assert "error" not in tool_msgs[0].content  # no xcrun cache noise
    assert tool_msgs[1].is_error


@mac_only
def test_skill_directories_are_readable(
    dirs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A skill under a denied home stays readable, so its scripts/ can run."""
    home = Path(os.path.realpath(tmp_path)) / "home"
    skill_dir = home / ".config" / "timu" / "skills" / "tool"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: tool\ndescription: d\n---\n")
    (skill_dir / "scripts" / "run.sh").write_text("echo from skill\n")
    (home / "private.txt").write_text("no\n")
    ctx = replace(
        ctx_for(dirs, MacSandbox(home=home)), skills=find(["tool"], [skill_dir.parent])
    )
    assert sh(ctx, f"sh {skill_dir}/scripts/run.sh").text == "from skill\n[exit 0]"
    assert sh(ctx, f"cat {home}/private.txt").is_error


@mac_only
def test_here_documents_work(dirs: tuple[Path, Path, Path]) -> None:
    """bash 3.2 writes here-documents to /tmp/sh-thd*, ignoring TMPDIR."""
    ctx = ctx_for(dirs)
    assert (
        sh(ctx, "cat <<'EOF'\nfrom a heredoc\nEOF").text == "from a heredoc\n[exit 0]"
    )
    assert sh(ctx, "echo x > /private/tmp/timu-not-allowed").is_error
    assert not Path("/private/tmp/timu-not-allowed").exists()


@mac_only
def test_sandbox_cannot_launch_apps(dirs: tuple[Path, Path, Path]) -> None:
    """launchd would run an app started through LaunchServices unsandboxed."""
    ws, _, outside = dirs
    exe = ws / "x.app" / "Contents" / "MacOS" / "x"
    exe.parent.mkdir(parents=True)
    exe.write_text(f"#!/bin/sh\necho x > {outside}/escaped\n")
    exe.chmod(0o755)
    (ws / "x.app" / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>x</string>"
        "<key>CFBundleIdentifier</key><string>test.timu.escape</string>"
        "</dict></plist>"
    )
    assert sh(ctx_for(dirs), "/usr/bin/open -g -j ./x.app").is_error
    time.sleep(2)  # a launched app would write asynchronously
    assert not (outside / "escaped").exists()


@mac_only
def test_sandbox_signals_only_its_own_processes(dirs: tuple[Path, Path, Path]) -> None:
    ctx = ctx_for(dirs)
    assert sh(ctx, f"kill -0 {os.getpid()}").is_error
    out = sh(ctx, "sleep 5 & kill $!; wait $!; echo killed")
    assert out.text.endswith("killed\n[exit 0]"), out.text
