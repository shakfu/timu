"""Measure timu against real models on seeded tasks with hidden oracle tests.

Each task under scripts/sim/tasks/<name>/ holds:

    task.toml   workflow, objective, oracle command and checks (see Task)
    repo/       the files the agents see, committed to a fresh git repo
    oracle/     hidden tests, copied in only after the run
    solution/   a reference fix; tests check the oracle fails without it and passes with it

A task that sets `base` starts instead from that commit of this repo, without
scripts/sim, overlaid with repo/ and with break.patch applied. The commit is the
reference fix.

A trial runs `timu run` as a user would, on a copy, with its own XDG_STATE_HOME. The
harness then checks out the committed branch, adds the oracle files, and runs the
oracle in timu's sandbox: the tree holds model-written code. A trial is solved when
the oracle passes and no `protected` file changed.

    uv run python scripts/sim.py list
    uv run python scripts/sim.py run --model openai/gpt-6-luna --trials 3
    uv run python scripts/sim.py summary --out DIR --min-solved 0.8

Billed: every trial calls the configured model API.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from timu.config import ConfigError, load_config, user_config_path
from timu.graph import GraphError, _git
from timu.sandbox import NoSandbox, Policy, Sandbox, detect
from timu.tool import state_dir
from timu.tools.shell import run_sh, scrubbed_env
from timu.trace import TraceError, load
from timu.workflow import WORKFLOWS, verdict

TASKS = Path(__file__).parent / "sim" / "tasks"
ROOT = Path(__file__).resolve().parents[1]  # the repo whose commits `base` names
ORACLE_TIMEOUT = 120.0  # seconds
MAX_OUTPUT = 64 * 1024  # bytes of oracle output kept
GIT = ["git", "-c", "user.name=sim", "-c", "user.email=sim@localhost"]

Runner = Callable[[list[str], dict[str, str], float], tuple[int, str]]
"""runner(timu argv, env, timeout) -> (exit code, stderr)."""


@dataclass(frozen=True)
class Task:
    name: str
    path: Path
    workflow: str
    objective: str
    oracle: str  # shell command; {python} is the harness's interpreter
    args: tuple[str, ...] = ()  # extra `timu run` arguments
    protected: tuple[str, ...] = ()  # globs a correct run leaves unchanged
    max_cost: float = 1.0  # USD per trial, passed as --max-cost
    timeout: float = 1800.0  # wall seconds per trial
    base: str = ""  # a commit of ROOT to start from, broken by break.patch

    def files(self, part: str) -> list[Path]:
        root = self.path / part
        return (
            sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
        )


def load_task(path: Path) -> Task:
    """The task in path. Raises ValueError naming the file and key."""
    src = path / "task.toml"
    raw = tomllib.loads(src.read_text())
    known = {f for f in Task.__dataclass_fields__ if f not in ("name", "path")}
    if extra := sorted(set(raw) - known):
        raise ValueError(f"{src}: unknown keys: {', '.join(extra)}")
    for key in ("workflow", "objective", "oracle"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"{src}: {key} must be a non-empty string")
    for key in ("args", "protected"):
        v = raw.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{src}: {key} must be a list of strings")
        raw[key] = tuple(v)
    base = raw.get("base", "")
    if not isinstance(base, str) or (base and not (path / "break.patch").is_file()):
        raise ValueError(f"{src}: base must be a commit, with a break.patch beside it")
    if not base and not (path / "repo").is_dir():
        raise ValueError(f"{path}: no repo/ directory")
    return Task(name=path.name, path=path, **raw)


def load_tasks(root: Path = TASKS, only: Sequence[str] = ()) -> list[Task]:
    tasks = [
        load_task(p) for p in sorted(root.iterdir()) if (p / "task.toml").is_file()
    ]
    if only:
        missing = set(only) - {t.name for t in tasks}
        if missing:
            raise ValueError(f"unknown tasks: {', '.join(sorted(missing))}")
        tasks = [t for t in tasks if t.name in only]
    return tasks


# ---- config ----


def toml_dump(data: Mapping[str, Any]) -> str:
    """data as TOML: scalars and lists of scalars, nested tables as [a.b] headers."""
    lines: list[str] = []

    def key(k: str) -> str:
        bare = k and all(c.isalnum() or c in "-_" for c in k)
        return k if bare else json.dumps(k, ensure_ascii=False)

    def value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int | float):
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueError(f"cannot write {v} to TOML")
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        raise ValueError(f"cannot write {type(v).__name__} to TOML")

    def table(path: list[str], t: Mapping[str, Any]) -> None:
        scalars = {k: v for k, v in t.items() if not isinstance(v, Mapping)}
        if path and (scalars or not t):
            lines.append(f"[{'.'.join(key(p) for p in path)}]")
        lines.extend(f"{key(k)} = {value(v)}" for k, v in scalars.items())
        for k, v in t.items():
            if isinstance(v, Mapping):
                table([*path, k], v)

    table([], data)
    return "\n".join(lines) + "\n"


def trial_config(
    base: Mapping[str, Any],
    model: str,
    role_models: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """base with every role at model on the default provider, if given, then
    role_models on top, each on its role's provider. Without
    model, env's TIMU_MODEL replaces only the provider model, as in timu. The
    interpreter's prefixes join extra_read, so sandboxed commands can run Python.
    Raises ValueError if no model results."""
    cfg: dict[str, Any] = json.loads(json.dumps(base))  # a deep copy of TOML data
    if model:
        cfg.setdefault("provider", {})["model"] = model
        for r in cfg.get("roles", {}).values():
            r.pop("model", None)
            r.pop("provider", None)
    elif env and env.get("TIMU_MODEL"):
        cfg.setdefault("provider", {})["model"] = env["TIMU_MODEL"]
    for role, m in role_models.items():
        cfg.setdefault("roles", {}).setdefault(role, {})["model"] = m
    sandbox = cfg.setdefault("sandbox", {})
    reads = list(sandbox.get("extra_read", []))
    reads += [p for p in prefixes() if p not in reads]
    sandbox["extra_read"] = reads
    if not model_for(cfg, ""):
        raise ValueError("no model: pass --model, or set provider.model in the config")
    return cfg


def trial_env(env: Mapping[str, str], state: Path) -> dict[str, str]:
    """env for a trial: its own state dir, and no TIMU_MODEL, since the trial's
    config already holds the model it resolves to (trial_config)."""
    out = {k: v for k, v in env.items() if k != "TIMU_MODEL"}
    out["XDG_STATE_HOME"] = str(state)
    return out


def preflight(
    cfg: Mapping[str, Any], roles: Iterable[str], env: Mapping[str, str] = os.environ
) -> str | None:
    """Why timu would refuse cfg for roles, such as a missing API key, or None. The
    same check `timu run` makes before a run, so one problem does not fail every
    trial."""
    with tempfile.TemporaryDirectory(prefix="timu-sim-") as tmp:
        path = Path(tmp) / "timu.toml"
        path.write_text(toml_dump(cfg))
        try:
            config = load_config(path, trial_env(env, Path(tmp)))
            for role in roles:
                config.provider(role, trial_env(env, Path(tmp)))
        except ConfigError as e:
            return str(e).replace(str(path), "config")
    return None


def prefixes() -> list[str]:
    return sorted({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix)})


# ---- one trial ----


@dataclass
class Trial:
    task: str
    model: str  # the label: --model, or "config" for the user's own models
    n: int
    workflow: str = ""
    exit: int | None = None
    status: str = ""  # the workflow_result status
    summary: str = ""
    verdict: str | None = None
    rounds: int = 0
    committed: bool = False
    changed: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    protected_changed: list[str] = field(default_factory=list)
    oracle: str = ""  # pass, fail, timeout, skipped or error
    oracle_tail: str = ""
    solved: bool = False
    false_accept: bool = False  # timu exited 0 but the task is not solved
    false_reject: bool = False  # timu exited non-zero but the task is solved
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    model_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    by_role: dict[str, dict[str, float]] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)  # role -> model id
    run_id: str = ""
    error: str = ""  # a harness or usage error; the trial is not scored

    @property
    def key(self) -> str:
        return f"{self.task}/{self.model}/{self.n}"


def subprocess_runner(
    argv: list[str], env: dict[str, str], timeout: float
) -> tuple[int, str]:
    """Run timu as a user would. On timeout, SIGINT first, so timu stops its sandboxed
    commands and writes its trace, then SIGKILL."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(
            [sys.executable, "-m", "timu.cli", *argv],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )
        try:
            code = p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            code = 124  # as timeout(1)
        err.seek(0)
        return code, err.read().decode(errors="replace")


def materialize(task: Task, dest: Path, solved: bool = False) -> None:
    """Write task's starting tree to dest, which must not exist, or with solved, the
    tree with its reference fix."""
    if not task.base:
        shutil.copytree(task.path / "repo", dest)
        if solved:
            shutil.copytree(task.path / "solution", dest, dirs_exist_ok=True)
        return
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", task.base, "--", ".", ":!scripts/sim"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    if (task.path / "repo").is_dir():
        shutil.copytree(task.path / "repo", dest, dirs_exist_ok=True)
    if not solved:
        subprocess.run(
            ["git", "apply", str(task.path / "break.patch")],
            cwd=dest,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(dest.parent)},
            check=True,
            capture_output=True,
        )  # outside any repo, git apply patches paths relative to dest


def seed_repo(task: Task, dest: Path) -> str:
    """task's starting tree committed to a fresh git repo at dest. Returns the commit."""
    materialize(task, dest)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run([*GIT, *args], cwd=dest, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dest,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def run_trial(
    task: Task,
    trial: Trial,
    config: Mapping[str, Any],
    root: Path,
    sandbox: Sandbox | None,
    runner: Runner = subprocess_runner,
    env: Mapping[str, str] = os.environ,
) -> Trial:
    """Run one trial in root, which must not exist, and score it."""
    trial.workflow = task.workflow
    root.mkdir(parents=True)
    repo, state = root / "repo", root / "state"
    base = seed_repo(task, repo)
    cfg = root / "timu.toml"
    cfg.write_text(toml_dump(config))
    run_env = trial_env(env, state)
    argv = [
        "run",
        "--config", str(cfg),
        "-C", str(repo),
        "--workflow", task.workflow,
        "--no-approve-untrusted",
        "--max-cost", str(task.max_cost),
        *task.args,
        task.objective,
    ]  # fmt: skip
    trial.exit, stderr = runner(argv, run_env, task.timeout)
    (root / "stderr.txt").write_text(stderr)
    traces = sorted((state / "timu" / "runs").glob("*.jsonl"))
    if len(traces) != 1:
        trial.error = (
            f"{len(traces)} traces; stderr: {' '.join(tail(stderr, 600).split())}"
        )
        return trial
    try:
        score_trace(trial, load(traces[0]))
    except TraceError as e:
        trial.error = str(e)
        return trial
    trial.run_id = traces[0].stem
    copy = state / "timu" / "work" / trial.run_id / "run"
    head = base
    if copy.is_dir():
        branch = f"timu/{trial.run_id}/run"
        head = _git(copy, "rev-parse", "--verify", "--quiet", branch).strip() or base
        if (report := copy / "REVIEW.md").is_file():
            trial.verdict = verdict(report.read_text(errors="replace"))
    trial.committed = head != base
    src = copy if trial.committed else repo
    if trial.committed:
        score_diff(trial, src, base, head, task.protected)
    score_oracle(trial, task, src, head, root / "eval", sandbox)
    return score_outcome(trial)


def score_outcome(trial: Trial) -> Trial:
    """solved, false accept and false reject. A run a provider error stopped measures
    the provider, so it becomes an unscored error, and `run` retries it on resume."""
    trial.solved = trial.oracle == "pass" and not trial.protected_changed
    trial.false_accept = trial.exit == 0 and not trial.solved
    trial.false_reject = trial.exit != 0 and trial.solved
    if trial.exit != 0 and "provider error:" in trial.summary:
        trial.error = trial.summary
    return trial


def score_trace(trial: Trial, events: Sequence[Any]) -> None:
    """Status, usage per role, errors and wall time from a run's events."""
    if not events:
        return
    for e in events:
        d = e.data
        if e.kind == "workflow_result" and not e.node:
            trial.status, trial.summary = (
                str(d.get("status", "")),
                str(d.get("summary", "")),
            )
        elif e.kind == "round":
            trial.rounds = max(trial.rounds, int(d.get("n", 0)))
        elif e.kind == "start" and d.get("model"):
            trial.models.setdefault(e.role, str(d["model"]))
        elif e.kind == "tool_result" and d.get("is_error") is True:
            trial.tool_errors += 1
        elif e.kind == "model_reply" and d.get("error"):
            trial.model_errors += 1
        elif e.kind == "result" and isinstance(u := d.get("usage"), dict):
            role = trial.by_role.setdefault(e.role, {})
            for k in (
                "turns",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "cost_usd",
            ):
                v = u.get(k, 0)
                if isinstance(v, int | float) and not isinstance(v, bool):
                    role[k] = role.get(k, 0) + v
                    setattr(trial, k, getattr(trial, k) + v)
    trial.wall_seconds = round(events[-1].ts - events[0].ts, 1)


def score_diff(
    trial: Trial, copy: Path, base: str, head: str, protected: Sequence[str]
) -> None:
    trial.changed = _git(copy, "diff", "--name-only", "-z", base, head).split("\0")[:-1]
    for line in _git(copy, "diff", "--numstat", base, head).splitlines():
        added, removed, _ = line.split("\t", 2)
        trial.lines_added += int(added) if added.isdigit() else 0
        trial.lines_removed += int(removed) if removed.isdigit() else 0
    trial.protected_changed = [
        p for p in trial.changed if any(fnmatch.fnmatchcase(p, g) for g in protected)
    ]


def score_oracle(
    trial: Trial, task: Task, src: Path, head: str, dest: Path, sandbox: Sandbox | None
) -> None:
    """Check out head from src into dest without .git, add the oracle files and run
    the oracle in sandbox. Without one, the oracle is skipped."""
    if sandbox is None:
        trial.oracle = "skipped"
        trial.oracle_tail = "no sandbox; pass --unsafe-oracle to run it unconfined"
        return
    try:
        _git(
            dest.parent, "clone", "--quiet", "--no-checkout", "--", str(src), str(dest)
        )
        _git(dest, "checkout", "--quiet", "--detach", head)
    except GraphError as e:
        trial.oracle, trial.oracle_tail = "error", str(e)
        return
    shutil.rmtree(dest / ".git")
    for f in task.files("oracle"):
        target = dest / f.relative_to(task.path / "oracle")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
    trial.oracle, trial.oracle_tail = oracle(task, dest, sandbox)


def oracle(task: Task, tree: Path, sandbox: Sandbox) -> tuple[str, str]:
    """Run task's oracle in tree. Returns (pass|fail|timeout|error, output tail)."""
    cmd = task.oracle.replace("{python}", sys.executable)
    with tempfile.TemporaryDirectory(prefix="timu-sim-") as tmp:
        home = Path(os.path.realpath(tmp))
        tree = Path(os.path.realpath(tree))
        policy = Policy((tree,), (tree, home), scratch=(home,))
        ran = run_sh(
            cmd,
            cwd=tree,
            sandbox=sandbox,
            policy=policy,
            env=scrubbed_env(home),
            timeout=ORACLE_TIMEOUT,
            cancel=threading.Event(),
            max_output=MAX_OUTPUT,
        )
    if ran.stopped:
        return "timeout", tail(ran.text)
    if ran.code is None:
        return "error", tail(ran.text)
    return ("pass" if ran.code == 0 else "fail"), tail(ran.text)


def tail(text: str, n: int = 2000) -> str:
    return text if len(text) <= n else "..." + text[-n:]


# ---- statistics ----


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for k successes in n trials, 95% by default."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def read_results(path: Path) -> list[Trial]:
    if not path.is_file():
        return []
    names = set(Trial.__dataclass_fields__)
    return [
        Trial(**{k: v for k, v in json.loads(line).items() if k in names})
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def summarize(trials: Iterable[Trial]) -> tuple[str, dict[str, Any]]:
    """A markdown table per (model, task) and per model, and the same as data."""
    scored = [t for t in trials if not t.error]
    groups: dict[tuple[str, str], list[Trial]] = {}
    for t in scored:
        groups.setdefault((t.model, t.task), []).append(t)
        groups.setdefault((t.model, "ALL"), []).append(t)
    head = (
        "model", "task", "n", "solved", "95% CI", "exit 0", "false accept",
        "false reject", "protected", "model errors", "cost/trial", "median s", "turns", "tokens",
    )  # fmt: skip
    rows, data = [head], {}
    for (model, task), ts in sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1] == "ALL", kv[0][1])
    ):
        n = len(ts)
        k = sum(t.solved for t in ts)
        lo, hi = wilson(k, n)
        row = {
            "n": n,
            "solved": k / n,
            "ci": [lo, hi],
            "exit0": sum(t.exit == 0 for t in ts) / n,
            "false_accept": sum(t.false_accept for t in ts),
            "false_reject": sum(t.false_reject for t in ts),
            "protected": sum(bool(t.protected_changed) for t in ts),
            "model_errors": sum(t.model_errors > 0 for t in ts),
            "cost": statistics.fmean(t.cost_usd for t in ts),
            "median_s": statistics.median(t.wall_seconds for t in ts),
            "turns": statistics.fmean(t.turns for t in ts),
            "tokens": statistics.fmean(t.input_tokens + t.output_tokens for t in ts),
        }
        data[f"{model}/{task}"] = row
        rows.append((
            model, task, str(n), f"{k}/{n}", f"{lo:.2f}-{hi:.2f}",
            f"{row['exit0']:.0%}", str(row["false_accept"]), str(row["false_reject"]),
            str(row["protected"]), str(row["model_errors"]), f"${row['cost']:.3f}", f"{row['median_s']:.0f}",
            f"{row['turns']:.1f}", f"{row['tokens']:.0f}",
        ))  # fmt: skip
    errors = [t for t in trials if t.error]
    lines = ["| " + " | ".join(r) + " |" for r in rows]
    lines.insert(1, "|" + "-|" * len(head))
    if errors:
        lines.append(f"\n{len(errors)} trials not scored (harness or usage errors):")
        lines.extend(f"- {t.key}: {t.error[:200]}" for t in errors)
    return "\n".join(lines) + "\n", data


def gate(data: Mapping[str, Any], min_solved: float | None) -> list[str]:
    """The models whose overall solve rate is below min_solved."""
    if min_solved is None:
        return []
    return [
        f"{k.removesuffix('/ALL')}: {v['solved']:.0%} < {min_solved:.0%}"
        for k, v in data.items()
        if k.endswith("/ALL") and v["solved"] < min_solved
    ]


# ---- commands ----


def cmd_list(args: argparse.Namespace) -> int:
    for t in load_tasks(args.tasks_dir):
        print(f"{t.name:20} {t.workflow:20} {t.objective.splitlines()[0][:70]}")
    return 0


def cmd_run(args: argparse.Namespace, runner: Runner = subprocess_runner) -> int:
    tasks = load_tasks(args.tasks_dir, args.task)
    base_path = args.config or user_config_path()
    base = tomllib.loads(base_path.read_text()) if base_path else {}
    role_models = dict(parse_role_model(s) for s in args.role_model)
    lineups = {
        label(c): c
        for c in (
            trial_config(base, m, role_models, os.environ) for m in args.model or [""]
        )
    }
    out: Path = args.out
    plan = [
        (t, m, n) for m in lineups for t in tasks for n in range(1, args.trials + 1)
    ]
    results_path = out / "results.jsonl"
    done = {t.key for t in read_results(results_path) if not t.error}
    todo = [(t, m, n) for t, m, n in plan if f"{t.name}/{m}/{n}" not in done]
    ceiling = sum(t.max_cost for t, _, _ in todo)
    print(
        f"sim: {len(plan)} trials, {len(plan) - len(todo)} already in {results_path}; "
        f"running {len(todo)}, at most ${ceiling:.2f} if the server reports cost"
    )
    roles = dict.fromkeys(r for t in tasks for r in WORKFLOWS[t.workflow].roles)
    for name, cfg in lineups.items():
        print(f"  {name}: " + ", ".join(f"{r} {model_for(cfg, r)}" for r in roles))
        if problem := preflight(cfg, roles):
            print(f"sim: {name}: {problem}", file=sys.stderr)
            return 2
    if args.dry_run:
        for task in tasks:
            n = sum(t is task for t, _, _ in todo)
            print(
                f"  {task.name}: {task.workflow}, {n} trials, max ${task.max_cost:.2f} each"
            )
        return 0
    sandbox = (
        NoSandbox()
        if args.unsafe_oracle
        else detect(tuple(Path(p) for p in prefixes()))
    )
    if sandbox is None:
        print("sim: no sandbox here; oracles will be skipped (see --unsafe-oracle)")
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)  # trials hold copies of the workspace and traces
    lock = threading.Lock()

    def one(item: tuple[Task, str, int]) -> Trial:
        task, name, n = item
        trial = Trial(task.name, name, n)
        root = out / "trials" / f"{task.name}-{name.replace('/', '_')}-{n}"
        if root.exists():
            shutil.rmtree(root)  # an earlier, unscored attempt
        try:
            run_trial(
                task,
                trial,
                lineups[name],
                root,
                sandbox,
                runner,
            )
        except (OSError, subprocess.CalledProcessError, GraphError, ValueError) as e:
            trial.error = f"{type(e).__name__}: {e}"
        if trial.exit == 130:
            trial.error = "interrupted"
        with lock:
            with results_path.open("a") as f:
                f.write(json.dumps(asdict(trial)) + "\n")
            mark = (
                "solved"
                if trial.solved
                else trial.error[:200] or f"oracle {trial.oracle}"
            )
            print(
                f"sim: {trial.key}: exit {trial.exit}, {mark}, ${trial.cost_usd:.3f}, "
                f"{trial.wall_seconds:.0f} s",
                flush=True,
            )
        return trial

    if args.jobs == 1:  # the main thread, which timu's Ctrl-C handler needs in-process
        for item in todo:
            one(item)
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(one, todo))
    return cmd_summary(args)


def cmd_summary(args: argparse.Namespace) -> int:
    trials = read_results(args.out / "results.jsonl")
    if not trials:
        print(f"sim: no results in {args.out}", file=sys.stderr)
        return 2
    latest = {t.key: t for t in trials}  # a rerun replaces an earlier line
    text, data = summarize(latest.values())
    (args.out / "summary.md").write_text(text)
    (args.out / "summary.json").write_text(json.dumps(data, indent=2) + "\n")
    print(text, end="")
    print(f"sim: wrote {args.out / 'summary.md'}")
    if failed := gate(data, args.min_solved):
        print("sim: below --min-solved: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


def model_for(cfg: Mapping[str, Any], role: str) -> str:
    """role's model in cfg, as timu.config.Config.model_for resolves it."""
    table = cfg.get("roles", {}).get(role, {})
    if name := table.get("provider"):
        named = cfg.get("providers", {}).get(name, {})
        return str(table.get("model") or named.get("model", ""))
    return str(table.get("model") or cfg.get("provider", {}).get("model", ""))


def label(cfg: Mapping[str, Any]) -> str:
    """The provider model, then each role model that differs from it, so results from
    different lineups never share a label."""
    model = model_for(cfg, "")
    extra = sorted(
        f"{r}={m}"
        for r in cfg.get("roles", {})
        if (m := model_for(cfg, r)) and m != model
    )
    return "+".join([model, *extra])


def parse_role_model(s: str) -> tuple[str, str]:
    role, sep, model = s.partition("=")
    if not sep or not role or not model:
        raise argparse.ArgumentTypeError(f"{s!r} is not ROLE=MODEL")
    return role, model


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tasks-dir", type=Path, default=TASKS, help=f"default: {TASKS}")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the tasks")
    default_out = state_dir() / "sim" / time.strftime("%Y%m%d-%H%M%S")
    for name in ("run", "summary"):
        p = sub.add_parser(
            name,
            help={"run": "run trials, then summarize", "summary": "summarize results"}[
                name
            ],
        )
        p.add_argument(
            "--out",
            type=Path,
            default=default_out,
            help="results directory; `run` resumes into an existing one "
            "(default: $XDG_STATE_HOME/timu/sim/<time>)",
        )
        p.add_argument(
            "--min-solved",
            type=float,
            metavar="RATE",
            help="exit 1 if any model's overall solve rate is below RATE (0-1)",
        )
    run = sub.choices["run"]
    run.add_argument(
        "--model",
        action="append",
        default=[],
        help="model id for every role; repeat to compare models. "
        "Default: the models in the config",
    )
    run.add_argument(
        "--role-model",
        action="append",
        default=[],
        metavar="ROLE=MODEL",
        help="a role's model, over --model; repeatable",
    )
    run.add_argument(
        "--task", action="append", default=[], help="run only this task; repeatable"
    )
    run.add_argument(
        "--trials", type=int, default=3, help="trials per task and model (default: 3)"
    )
    run.add_argument("--jobs", type=int, default=1, help="trials at once (default: 1)")
    run.add_argument(
        "--config",
        type=Path,
        help="base timu.toml (default: the user's); models are overridden per trial",
    )
    run.add_argument(
        "--dry-run", action="store_true", help="print the trials; run nothing"
    )
    run.add_argument(
        "--unsafe-oracle",
        action="store_true",
        help="run oracles unconfined where no sandbox exists; they run model-written code",
    )
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "run":
            if args.trials < 1 or args.jobs < 1:
                raise ValueError("--trials and --jobs must be positive")
            for s in args.role_model:
                parse_role_model(s)
            return cmd_run(args)
        return cmd_summary(args)
    except (
        ValueError,
        OSError,
        tomllib.TOMLDecodeError,
        argparse.ArgumentTypeError,
    ) as e:
        print(f"sim: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
