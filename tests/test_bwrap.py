"""Tests for the Linux bwrap sandbox (design 3). The integration tests run only where
bwrap can create namespaces; see why_none."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from timu.sandbox import (
    MASKED,
    BwrapSandbox,
    Policy,
    _bwrap_problem,
    bwrap_args,
    detect,
    mac_profile,
    why_none,
)

bwrap_only = pytest.mark.skipif(
    not isinstance(detect(), BwrapSandbox), reason=f"needs bwrap: {why_none()}"
)


def _online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        return True
    except OSError:
        return False


# ---- arguments ----


def test_args_order_and_namespaces(tmp_path: Path) -> None:
    home, ws, tmp = tmp_path / "home", tmp_path / "home" / "ws", tmp_path / "t"
    (ws / ".git").mkdir(parents=True)
    (ws / "timu.toml").write_text("")
    tmp.mkdir()
    toolchain = home / "uv"
    toolchain.mkdir()
    policy = Policy((ws,), (ws, tmp), (ws / "timu.toml", ws / ".timu"))
    args = bwrap_args(policy, home, (toolchain, home / "missing"))
    assert args[:3] == ["--ro-bind", "/", "/"]
    assert "--unshare-net" in args and "--new-session" in args
    s = " ".join(args)
    masks = [f"--tmpfs {p}" for p in MASKED if p.exists()]
    assert masks and all(m in s for m in masks)
    order = [
        *masks,
        f"--tmpfs {home}",
        f"--ro-bind {ws} {ws}",
        f"--ro-bind {toolchain} {toolchain}",
        f"--bind {ws} {ws}",
        f"--bind {tmp} {tmp}",
        f"--ro-bind {ws / '.git'} {ws / '.git'}",
        f"--ro-bind {ws / 'timu.toml'} {ws / 'timu.toml'}",
    ]
    assert [s.index(x) for x in order] == sorted(s.index(x) for x in order)
    assert "missing" not in s and ".timu " not in s + " "  # only existing paths


def test_network_policy(tmp_path: Path) -> None:
    policy = Policy((), (tmp_path,), network=True)
    assert "--unshare-net" not in bwrap_args(policy, None)
    assert "(deny network*)" not in mac_profile(policy, None)
    assert "(deny network*)" in mac_profile(Policy((), (tmp_path,)), None)


def test_resolv_conf_under_a_mask_is_kept_for_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stub = run_dir / "systemd" / "resolve" / "stub-resolv.conf"
    stub.parent.mkdir(parents=True)
    stub.write_text("nameserver 127.0.0.53\n")
    link = tmp_path / "resolv.conf"
    link.symlink_to(stub)
    monkeypatch.setattr("timu.sandbox.MASKED", (run_dir,))
    monkeypatch.setattr("timu.sandbox.RESOLV", link)
    bind = f"--ro-bind {stub} {stub}"
    online = " ".join(bwrap_args(Policy((), (), network=True), None))
    assert online.index(f"--tmpfs {run_dir}") < online.index(bind)
    assert bind not in " ".join(bwrap_args(Policy((), ()), None))


def test_wrap(tmp_path: Path) -> None:
    argv = BwrapSandbox(home=tmp_path, bwrap="/x/bwrap").wrap(
        ["/bin/sh", "-c", "true"], Policy((), (tmp_path,))
    )
    assert argv[0] == "/x/bwrap"
    assert argv[-4:] == ["--", "/bin/sh", "-c", "true"]


def test_probe_reports_a_failing_bwrap() -> None:
    assert _bwrap_problem("/bin/false") == "exit 1"


def test_why_none_without_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("timu.sandbox.sys.platform", "linux")
    monkeypatch.setattr("timu.sandbox.shutil.which", lambda name: None)
    assert why_none() == "bwrap is not installed"
    assert detect() is None


# ---- enforcement ----


def run(sandbox: BwrapSandbox, policy: Policy, cmd: str, cwd: Path) -> int:
    argv = sandbox.wrap(["/bin/sh", "-c", cmd], policy)
    return subprocess.run(argv, cwd=cwd, capture_output=True, check=False).returncode


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "home" / "ws"
    (w / ".git").mkdir(parents=True)
    (w / ".git" / "config").write_text("")
    return w


@bwrap_only
def test_writes(ws: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    box, policy = BwrapSandbox(home=home), Policy((ws,), (ws,), (ws / "timu.toml",))
    (ws / "timu.toml").write_text("")
    assert run(box, policy, "echo x > f.txt", ws) == 0
    assert (ws / "f.txt").read_text() == "x\n"
    run(box, policy, f"echo x > {tmp_path / 'outside.txt'}", ws)  # /tmp is a tmpfs
    assert run(box, policy, "echo x >> .git/config", ws) != 0
    assert run(box, policy, "echo x > timu.toml", ws) != 0
    assert not (tmp_path / "outside.txt").exists()
    assert (ws / ".git" / "config").read_text() == ""


@bwrap_only
def test_home_is_hidden(ws: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "secret.txt").write_text("s3cret\n")
    box, policy = BwrapSandbox(home=home), Policy((ws,), (ws,))
    assert run(box, policy, "cat ../secret.txt", ws) != 0
    assert run(box, policy, "ls ..", ws) == 0  # the tmpfs, holding only ws


@bwrap_only
@pytest.mark.skipif(not _online(), reason="no network to tell a denial apart")
def test_network(ws: Path, tmp_path: Path) -> None:
    box = BwrapSandbox(home=tmp_path / "home")
    probe = "exec 3<>/dev/tcp/1.1.1.1/53" if os.path.exists("/bin/bash") else "true"
    cmd = f"/bin/bash -c '{probe}'"
    assert run(box, Policy((ws,), (ws,)), cmd, ws) != 0
    assert run(box, Policy((ws,), (ws,), network=True), cmd, ws) == 0


@bwrap_only
def test_host_unix_sockets_are_hidden(ws: Path, tmp_path: Path) -> None:
    """A new network namespace does not isolate path-bound Unix sockets, such as the
    session bus or docker.sock; the MASKED tmpfs mounts hide them."""
    path = tmp_path / "host.sock"
    with socket.socket(socket.AF_UNIX) as srv:
        srv.bind(str(path))
        srv.listen(1)
        box = BwrapSandbox(home=tmp_path / "home")
        connect = f"import socket; socket.socket(socket.AF_UNIX).connect({str(path)!r})"
        cmd = f'python3 -c "{connect}"'
        assert run(box, Policy((ws,), (ws,)), cmd, ws) != 0
        assert run(box, Policy((ws, tmp_path), (ws,)), cmd, ws) == 0  # a read root
