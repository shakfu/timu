"""Tests for scripts/sim.py: the fixtures' oracles, and one scored trial end to end
with a fake model."""

from __future__ import annotations

import importlib.util
import io
import shutil
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from timu.cli import main
from timu.provider.base import Reply
from timu.provider.fake import FakeProvider, call, calls, text
from timu.sandbox import NoSandbox

SCRIPT = Path(__file__).parents[1] / "scripts" / "sim.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sim", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sim"] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


sim = _load()
TASKS = sim.load_tasks()


@pytest.fixture(autouse=True)
def no_user_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))


def tree(task: Any, dest: Path, solved: bool) -> Path:
    sim.materialize(task, dest, solved)
    shutil.copytree(task.path / "oracle", dest, dirs_exist_ok=True)
    return dest


def hidden_only(task: Any) -> Any:
    """task with an oracle that runs only its hidden tests. A base task's full oracle
    adds the commit's own suite; test_base_suite_passes checks that once per commit."""
    if not task.base:
        return task
    files = " ".join(
        str(f.relative_to(task.path / "oracle")) for f in task.files("oracle")
    )
    cmd = f"PYTHONPATH=src {{python}} -m pytest -q -p no:cacheprovider {files}"
    return sim.Task(**{**task.__dict__, "oracle": cmd})


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_oracle_separates_seed_from_solution(task: Any, tmp_path: Path) -> None:
    """The oracle fails on the seeded repo and passes with the reference solution. A
    control task has no solution: its seed is already correct."""
    check = hidden_only(task)
    seed = sim.oracle(check, tree(task, tmp_path / "seed", False), NoSandbox())
    if not task.base and not (task.path / "solution").is_dir():
        assert seed[0] == "pass", seed[1]
        assert task.protected, "a control task must protect its code"
        return
    assert seed[0] == "fail", seed[1]
    fixed = sim.oracle(check, tree(task, tmp_path / "fixed", True), NoSandbox())
    assert fixed[0] == "pass", fixed[1]


def test_visible_tests_pass_with_the_solution(tmp_path: Path) -> None:
    """A correct fix must not need a protected file changed."""
    for task in TASKS:
        if not (task.path / "solution").is_dir() or not any(
            (task.path / "repo").glob("test_*.py")
        ):
            continue
        t = sim.Task(**{**task.__dict__, "oracle": "{python} -m unittest -q"})
        got = sim.oracle(t, tree(task, tmp_path / task.name, True), NoSandbox())
        assert got[0] == "pass", (task.name, got[1])


@pytest.mark.parametrize(
    "task",
    list({t.base: t for t in TASKS if t.base}.values()),
    ids=lambda t: t.base,
)
def test_base_suite_passes(task: Any, tmp_path: Path) -> None:
    """The suite a base task's oracle runs passes at the unbroken commit, with the
    hidden tests, so the oracle passes when the fix is restored."""
    got = sim.oracle(task, tree(task, tmp_path / "base", True), NoSandbox())
    assert got[0] == "pass", got[1]


def test_base_seed_hides_the_fix(tmp_path: Path) -> None:
    """A base task's seed holds neither its hidden tests nor any task's oracle."""
    for task in (t for t in TASKS if t.base):
        seed = tmp_path / task.name
        sim.materialize(task, seed)
        assert not (seed / "scripts" / "sim").exists()
        for f in task.files("oracle"):
            rel = f.relative_to(task.path / "oracle")
            assert not (seed / rel).exists(), rel
            names = [
                ln for ln in f.read_text().splitlines() if ln.startswith("def test")
            ]
            visible = "".join(p.read_text() for p in (seed / "tests").glob("test_*.py"))
            assert names and not any(n in visible for n in names), (task.name, names)


def test_toml_dump_round_trips() -> None:
    data = {
        "provider": {
            "model": 'a/"b"\n',
            "timeout": 60.5,
            "extra_body": {"cache_control": {"type": "ephemeral"}},
        },
        "roles": {"reviewer": {"model": "r", "skills": ["x"]}},
        "sandbox": {"extra_read": ["/pé"]},
        "flag": True,
    }
    assert tomllib.loads(sim.toml_dump(data)) == data


def test_trial_config_sets_every_role_to_the_model() -> None:
    base: dict[str, Any] = {
        "provider": {"model": "old"},
        "roles": {"coder": {"model": "c", "skills": ["s"]}},
    }
    cfg = sim.trial_config(base, "new", {"reviewer": "rev"})
    assert cfg["provider"]["model"] == "new"
    assert cfg["roles"] == {"coder": {"skills": ["s"]}, "reviewer": {"model": "rev"}}
    assert set(sim.prefixes()) <= set(cfg["sandbox"]["extra_read"])
    assert base["roles"]["coder"]["model"] == "c"  # not mutated
    assert sim.trial_config(base, "", {})["roles"]["coder"]["model"] == "c"


def test_trial_config_without_a_model() -> None:
    """TIMU_MODEL replaces only the provider model, as in timu; no model is an error."""
    base: dict[str, Any] = {
        "provider": {"model": "p"},
        "roles": {"coder": {"model": "c"}},
    }
    cfg = sim.trial_config(base, "", {}, {"TIMU_MODEL": "env"})
    assert (
        sim.model_for(cfg, "coder") == "c" and sim.model_for(cfg, "reviewer") == "env"
    )
    with pytest.raises(ValueError, match="no model"):
        sim.trial_config({}, "", {})


def test_preflight_reports_a_missing_key(tmp_path: Path) -> None:
    cfg = sim.trial_config(
        {"provider": {"model": "m", "api_key_env": "SIM_KEY"}}, "", {}
    )
    assert "SIM_KEY is not set" in (sim.preflight(cfg, ["coder"], {}) or "")
    assert sim.preflight(cfg, ["coder"], {"SIM_KEY": "k"}) is None


def test_trial_env_keeps_base_url() -> None:
    env = sim.trial_env({"TIMU_MODEL": "a", "TIMU_BASE_URL": "u"}, Path("/s"))
    assert env == {"TIMU_BASE_URL": "u", "XDG_STATE_HOME": "/s"}


def test_label_names_every_model() -> None:
    cfg = {
        "provider": {"model": "p"},
        "roles": {"r": {"model": "x"}, "c": {"model": "p"}},
    }
    assert sim.label(cfg) == "p+r=x"
    assert sim.label({"provider": {"model": "p"}}) == "p"
    named = {
        "provider": {"model": "p"},
        "providers": {"a": {"model": "am"}},
        "roles": {"reviewer": {"provider": "a"}},
    }
    assert sim.model_for(named, "reviewer") == "am"
    assert sim.label(named) == "p+reviewer=am"
    cfg = sim.trial_config(named, "new", {})
    assert sim.model_for(cfg, "reviewer") == "new"  # --model moves every role


def test_wilson() -> None:
    lo, hi = sim.wilson(0, 10)
    assert lo == 0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = sim.wilson(10, 10)
    assert hi == 1 and lo == pytest.approx(0.7225, abs=1e-4)
    assert sim.wilson(0, 0) == (0.0, 1.0)


def fake_runner(replies: list[Reply], monkeypatch: pytest.MonkeyPatch) -> Any:
    """Runs timu in this process with scripted replies, without a sandbox."""

    def run(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
        monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
        provider = FakeProvider(replies)
        err = io.StringIO()
        code = main(
            [argv[0], "--unsafe-no-sandbox", *argv[1:]],
            provider_for=lambda role: provider,
            out=io.StringIO(),
            err=err,
        )
        return code, err.getvalue()

    return run


CALC = next(t for t in TASKS if t.name == "calc")
BASE = {"provider": {"model": "m", "api_key_env": ""}}
APPROVE = text("VERDICT: APPROVE\nLooks right.", cost=0.01)


def trial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coder: list[Reply]) -> Any:
    t = sim.Trial("calc", "fake", 1)
    return sim.run_trial(
        CALC,
        t,
        sim.trial_config(BASE, "", {}),
        tmp_path / "trial",
        NoSandbox(),
        fake_runner([*coder, APPROVE], monkeypatch),
    )


def test_a_correct_fix_is_solved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = trial(
        tmp_path,
        monkeypatch,
        [
            calls(call("read", path="calc.py")),
            calls(call("edit", path="calc.py", old_string="a - b", new_string="a + b")),
            text("Fixed add().", cost=0.02),
        ],
    )
    assert not t.error, t.error
    assert (t.exit, t.status, t.verdict, t.committed) == (0, "done", "APPROVE", True)
    assert t.changed == ["calc.py"] and (t.lines_added, t.lines_removed) == (1, 1)
    assert (t.oracle, t.solved, t.false_accept, t.false_reject) == (
        "pass",
        True,
        False,
        False,
    )
    assert t.cost_usd == pytest.approx(0.03)
    assert set(t.by_role) == {"coder", "reviewer"}
    assert t.models == {}  # FakeProvider has no model attribute


def test_editing_the_test_is_a_false_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = trial(
        tmp_path,
        monkeypatch,
        [
            calls(call("read", path="test_calc.py")),
            calls(call("edit", path="test_calc.py", old_string="5)", new_string="-1)")),
            text("Tests pass."),
        ],
    )
    assert t.exit == 0 and t.protected_changed == ["test_calc.py"]
    assert t.oracle == "fail" and not t.solved and t.false_accept


def test_a_provider_error_is_not_scored() -> None:
    summary = "coder stopped in round 1: provider error: stream error: 502"
    t = sim.score_outcome(
        sim.Trial("calc", "m", 1, exit=1, oracle="pass", summary=summary)
    )
    assert t.error == summary
    assert not sim.score_outcome(sim.Trial("calc", "m", 1, exit=1, summary="x")).error


def test_summary_and_gate(tmp_path: Path) -> None:
    trials = [
        sim.Trial("calc", "m", 1, exit=0, solved=True, cost_usd=0.1),
        sim.Trial(
            "calc",
            "m",
            2,
            exit=0,
            false_accept=True,
            protected_changed=["t"],
            model_errors=2,
        ),
        sim.Trial("calc", "m", 3, error="usage"),
    ]
    text_, data = sim.summarize(trials)
    assert data["m/ALL"]["n"] == 2 and data["m/ALL"]["solved"] == 0.5
    assert data["m/calc"]["false_accept"] == 1 and data["m/calc"]["protected"] == 1
    assert data["m/calc"]["model_errors"] == 1
    assert "1 trials not scored" in text_
    assert sim.gate(data, 0.4) == [] and sim.gate(data, 0.6) == ["m: 50% < 60%"]


def test_run_resumes_and_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run` skips trials already in results.jsonl, and --min-solved sets the exit."""
    out = tmp_path / "out"
    fix = [
        calls(call("read", path="calc.py")),
        calls(call("edit", path="calc.py", old_string="a - b", new_string="a + b")),
        text("Fixed."),
        APPROVE,
    ]
    cfg = tmp_path / "base.toml"
    cfg.write_text(sim.toml_dump(BASE))
    argv = [
        "run",
        "--out",
        str(out),
        "--task",
        "calc",
        "--trials",
        "1",
        "--config",
        str(cfg),
    ]
    args = sim.parser().parse_args([*argv, "--min-solved", "1"])
    args.unsafe_oracle = True
    assert sim.cmd_run(args, fake_runner(fix, monkeypatch)) == 0
    assert sim.cmd_run(args, fake_runner([], monkeypatch)) == 0  # nothing to rerun
    assert len((out / "results.jsonl").read_text().splitlines()) == 1
    assert "| m | calc | 1 | 1/1 |" in (out / "summary.md").read_text()
