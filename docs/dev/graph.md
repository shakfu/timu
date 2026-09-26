# Workflow graphs

Status: phases 1 to 4 built (CHANGELOG, Unreleased); the rest is a proposal. It extends timu from one workflow on one workspace to a directed acyclic graph (DAG) of workflows across repos.

## 1. State before phase 1

- One workspace per run. `Run` resolves one `workdir` (`src/timu/workflow.py`). Every agent, fs tool and sandbox policy uses it as the read and write root.
- One workflow per invocation. `cli.py` takes one `-C` and one `--workflow`.
- No structured output. A `Result` holds a status, a free-text summary and artifacts. Nothing like a version number comes back as checked data.
- No effects. timu never commits, pushes or publishes.

Two runs can be chained in the shell today (`timu run -C A ... && timu run -C B ...`). The chain then has two traces and two budgets, and `--max-cost` covers only one run.

## 2. Goals

1. Run a DAG of nodes. Each node is one workflow instance on one GitHub repo.
2. Pass named outputs from a node to its dependents.
3. Keep today's security model inside each node: an agent sees one workspace, and the untrusted mark survives every hand-off.
4. Share one budget, one trace and one cancel flag across the graph.

Non-goals: discovering dependents automatically; waiting days for PR review or CI (section 9, question 1).

## 3. Model

| Concept | Definition |
|-|-|
| Graph | A named set of nodes with dependency edges. Cycles are rejected at load time. |
| Node | One workflow instance on one repo at one ref. Fields: `id`, `repo`, `ref`, `workflow`, `objective`, `params`, `inputs`, `outputs`, `needs`, `budget`. |
| Session | State shared by every node: run id, trace sink, total budget, cancel flag, approval gate. |
| Run | A per-node view: the session plus one workdir. Existing workflows take a `Run` and do not change. |
| Output | A named `Artifact` that the engine extracts from the node's workspace after the workflow succeeds. |
| Effect | An outward action: push a branch, open a PR, publish a package. Only the engine runs effects. |

Agents still see only one repo. The engine is trusted code, and all cross-repo logic lives there.

### 3.1 Relation to the team model

- A node runs a team, as a workflow does today. Roles, sandboxes and hand-offs inside a node do not change.
- The graph repeats design 7's two workflow kinds one level up. The static graph (sections 4-5) is a pipeline: code starts teams in a fixed order. The delegating graph (section 10) is delegation: a role picks the order.
- Teams in different nodes cooperate as agents do today: through artifacts with provenance, never through shared context. The untrusted mark crosses repos unchanged.
- Coordination that needs no judgment is code. `fix_review` already parses the verdict in code; the graph applies the same rule to ordering.
- The engine runs no project code. A step that runs project code with network access is a role (6.5), so the capability rule in design 6 holds at every level.

## 4. Graph file

```toml
[nodes.a]
repo = "org/a"
workflow = "fix-review"
objective = "Drop Python 3.10 support; bump the minor version"
outputs = { version = "toml:pyproject.toml#project.version", notes = "changelog:latest" }

[nodes.b]
repo = "org/b"
needs = ["a"]
workflow = "bump-dep"
params = { package = "a", version = "${a.version}" }
inputs = ["a.notes"]

[nodes.c]
repo = "org/c"
needs = ["a", "b"]
workflow = "bump-dep"
params = { package = "b", version = "${b.version}" }
```

`extensibility.md` 5.3 rejects a declarative pipeline format because `fix-review` needs loops and branches. A graph has no loops; loops stay inside each node's workflow. The graph file lives outside every repo, so workspace-trust rules (`extensibility.md` 7) do not apply.

### 4.1 Substitution rule

`${node.output}` may appear only in `params`. The workflow declares a schema for each param, and the engine checks the bound value against it. For example, `version` must parse as a PEP 440 version (https://peps.python.org/pep-0440/).

Free text, such as `notes`, reaches a node only as an `inputs` artifact. Substituting it into `objective` would paste agent output into a goal, and the untrusted mark would be lost (design 6).

### 4.2 Output extractors

An extractor reads a file in the node's workspace after the workflow returns `done`. Initial set:

| Extractor | Reads |
|-|-|
| `toml:FILE#KEY` | a dotted key in a TOML file, via `tomllib` |
| `changelog:latest` | the first released section of `CHANGELOG.md` |
| `git:diff` | the diff from the node's base ref |

Outputs are never parsed from an agent's summary, because a summary is model output. Each output artifact records `source=<node id>` and inherits the node result's `untrusted` flag.

### 4.3 Node results

Every node also exposes its workflow's final artifacts as outputs, without declaration. `Workflow.artifacts` names them: `a.review` for `fix-review`, and also `a.research` and `a.sources` for `research-fix-review`. Each keeps its own origin and `untrusted` flag, so web content from one node still reaches the approval gate in the next. Extracted outputs say what changed. These say why, which a downstream coder needs when a change breaks it.

Node results may appear only in `inputs`, never in `params`. They are free text.

## 5. Engine

The scheduler is stdlib `graphlib.TopologicalSorter` (https://docs.python.org/3/library/graphlib.html). Its `get_ready()` and `done()` calls allow parallel nodes later without a rewrite.

```python
def run_graph(session: Session, graph: Graph) -> GraphResult:
    ts = TopologicalSorter({n.id: set(n.needs) for n in graph.nodes})
    ts.prepare()
    results: dict[str, NodeResult] = {}
    while ts.is_active():
        for nid in ts.get_ready():               # sequential first
            node = graph.nodes[nid]
            if any(results[d].status != "done" for d in node.needs):
                results[nid] = NodeResult("skipped")
            else:
                ws = provision(node, results)    # engine-side; has network
                run = session.at(ws, node=nid, budget=node.budget)
                params = bind(node.params, results)
                inputs = tuple(results[d].outputs[k] for d, k in node.inputs)
                r = WORKFLOWS[node.workflow].run(run, node.objective, params, inputs)
                outs = extract(node.outputs, ws, r.untrusted) if r.status == "done" else {}
                results[nid] = NodeResult(r.status, r, outs, ws)
            ts.done(nid)
    return GraphResult(results)
```

### 5.1 Statuses and exit code

A node's status is a `Status` from `types.py`, or `skipped`. After Ctrl-C, every node not yet started is `cancelled`. Planned:

- `noop`: the node's spec and input values match a previous successful run (section 8, phase 7).

A failure skips only that node's descendants, so independent branches still run. The graph's exit code is the worst node status, mapped through `EXIT` in `cli.py`.

### 5.2 Budgets

Each node gets the smaller of its own `budget` and what the graph has left. Agents in the node then get the smaller of their role's budget and the node's remainder. This is the rule `Run._child_budget` applies to agents today, one level up. Without a node cap, one runaway node would spend the budget of every node after it.

A node with no `budget` takes `[defaults] budget`, or else has no node cap. A node budget fills unset limits from the workflow budget. A node that exhausts its budget ends with status `budget`, and its descendants are skipped.

## 6. Changes to existing code

1. Split `Run` into `Session` and a per-node view. `Usage` is frozen, so `self.used += delta` rebinds the field, and a shallow copy would stop sharing it. Shared counters live on the `Session`. Parallel nodes (phase 7) will charge them from several threads, so that phase adds a lock. The view will charge both its node's ledger and the session's (5.2).
2. Build the workflow registry from `extensibility.md` 5.1. Nodes name workflows and pass `params`, so the graph depends on it.
3. Add a `node` field to `Event`. A graph run writes one trace, and `timu trace` prefixes each top-level agent with its node. Traces outside a graph omit the field.
4. Let a node's sandbox read the files an upstream node exported (7.5), read-only. Superseded: read access to the whole upstream workspace.
5. Superseded by 7.7: an `installer` role and a `bump-dep` workflow. A generic `bump-dep` cannot find a pin kept in a build script (7.6).
   - The engine must not relock itself. Resolving can build source distributions, which runs build-backend code from the repo. The coder may have just edited that code, so running it with network access and credentials would give coder-written code both. Design 6 forbids that combination for any role.
6. CLI: `timu graph run FILE.toml`, and `--allow-effects` for anything outward-facing.

## 7. Workspaces and GitHub

### 7.1 Projects roots

The user file lists the directories that hold projects:

```toml
[projects]
roots = ["~/projects"]
```

A node's `repo` is a project name, or `owner/name` for GitHub. The engine resolves it before the run starts:

- `repo = "a"` resolves to `<root>/a`, which must be a git repo.
- `repo = "org/a"` resolves the same way, by `a`. The local repo's `origin` must point at `github.com/org/a`, or the graph is rejected. With no local match, the engine clones from GitHub (7.2).
- A name found under two roots is an error that lists both paths. Picking the first match silently could run a node on the wrong repo.

The graph file then names projects only. Paths stay in the user file, so one graph works on any machine with the same projects.

### 7.2 Provisioning

`provision` creates a copy for the node under `$XDG_STATE_HOME/timu/work/<run id>/<node>/`, on branch `timu/<run id>/<node>`, at the node's `ref` (default `HEAD`).

- From a local project: `git clone --local` (https://git-scm.com/docs/git-clone#Documentation/git-clone.txt---local). It links objects and writes nothing to the source repo. Uncommitted changes in the user's checkout do not reach the node. The engine warns when the checkout has any, since the node then differs from what the user sees.
- From GitHub: a plain clone.
- Results stay in the copy, committed on the node's branch (7.2.1). The run prints `git -C <project> fetch <copy> <branch>:<branch>` for each node that committed. The commit leaves out ignored files and the report file.
- A `--local` copy's `origin` is the local path. Effects (7.4) push to the project's GitHub remote instead.

Rejected: `git worktree add` from the user's repo. It needs no copy, but it writes worktree metadata and a branch into the user's `.git`.

### 7.2.1 Commit policy

Decided: the engine may commit in its own copy. No agent may commit.

- After a node's workflow ends, the engine commits the copy's changes on the node's branch. The author is a fixed timu identity, with hooks and signing off. The user's git identity and hooks play no part.
- Agents already cannot commit: the sandbox and the fs tools refuse writes inside `.git`, and a commit writes there.
- The engine never commits in the user's repo. The user decides whether to fetch.
- With commits, a descendant on the same project clones from its ancestor's branch. This lifts the one-node-per-project rule in 7.3.
- After the workflow, `HEAD` must still be the node's start commit on its branch. Otherwise an agent committed, for example with `--unsafe-no-sandbox`, and the node fails without an engine commit.
- A node commits whatever its status, so a failed node's work can be fetched and inspected. Only a `done` node's outputs pass on.

### 7.3 Credentials and conflicts

- Credentials (`gh`, SSH keys) stay in the engine. The sandbox already scrubs the environment, so agents never see tokens.
- A node starts from the branch of its nearest ancestor on the same project, and `ref` applies only to the first node on a project. Two same-project ancestors that do not need each other would give two starting points, so the graph is rejected. Nodes on one project that do not need each other get separate branches from `ref`.

### 7.4 Effects

Effects are separate node kinds, off unless `--allow-effects` is passed. A commit in timu's own copy is not an effect (7.2.1). A push, a PR or a release is.

### 7.5 Building against an unreleased upstream

Answers question 9.4 for the case of a library and an app that depends on its released wheels, both in one projects root. The user's own process:

1. Change the library and bump its version.
2. Build the wheel locally.
3. Install that wheel in the app, and fix the app until it works.
4. Release the library.
5. Point the app at the released version.

Steps 1-3 and step 5 are two graphs, split by the release in step 4:

- **Validate graph.** `lib` (fix-review) -> `wheel` (builds it) -> `app` (installs that wheel, runs its checks, and fixes the app only if they fail). 7.7 has the file.
- **Adopt graph**, after the user releases. `app` pins the released version and runs the checks.

The release stays a human step until effects exist (7.4). No graph waits across it, which answers question 9.1 for this flow.

### 7.6 The reference case: cyllama and cyllama-desktop

Read on 2026-09-25, not changed.

- `cyllama` builds its wheel with `make wheel` (`uv build --wheel`, scikit-build-core and CMake). The `$(LIBLAMMA)` prerequisite builds llama.cpp and the other native libraries first. The build compiles C++, fetches build requirements, and can take longer than the shell tool's 600 s limit.
- `cyllama-desktop` ships a bundled Python env, built by `scripts/build-python-env.sh`. The script downloads a python-build-standalone runtime from GitHub, then pip-installs `cyllama==$CYLLAMA_VERSION` from PyPI, or a local path given in `CYLLAMA_SOURCE`. pip accepts a wheel path there; `make python-local` accepts only a directory.
- The released pin is `CYLLAMA_VERSION="${CYLLAMA_VERSION:-0.5.0}"` in that script. `python-sidecar/pyproject.toml` holds a floor, `cyllama>=0.5.0`, and the backend variant.
- `make test` and `make e2e` both run against a stub of `cyllama` (`tests/conftest.py`). The only check against the real library is the `import cyllama` smoke test at the end of `build-python-env.sh`.

Consequences:

1. **A generic `bump-dep` cannot find the pin.** It is a shell default in a build script, not a lockfile entry. Only the project knows how to install a local wheel and where the pin lives. The graph file should state that as commands.
2. **The build and install steps need no model.** They are fixed commands. An LLM agent holding `exec` with network would break the README's rule for roles. A model-free step does not: the commands come from the graph file, and nothing a model reads can change them.
3. **"All tests pass" does not test the new cyllama** in this project. The build smoke test catches an import failure only. An API change the sidecar depends on passes both `make test` and `make e2e`. Closing that gap needs a test in `cyllama-desktop` that runs against the real library.

### 7.7 Command steps instead of builder and installer roles

Replaces the `builder` role, the `installer` role and `bump-dep` above. Built in phase 4.

- **`commands` workflow.** Runs a list of commands from the graph file in the node's copy, in the sandbox, with no model. `network = true` turns network on. Each command has its own timeout. A command that exits non-zero fails the node, and its output tail becomes the node's `log` result.
- **`check-fix-review` workflow.** Runs `check` commands. If they pass, the node is done. If not, it runs `fix_review` with the failure log as an input, then runs the checks again, up to `max_rounds`. The coder still has no network. The check commands, run outside any model, rebuild whatever needs it.
- **File outputs.** `file:GLOB` must match exactly one file in the copy. The engine copies it to `work/<run id>/files/<node>/` and the output's value is that path. A dependent's sandbox gets read access to that directory.
- **Substitution into commands.** `${node.output}` may appear anywhere in a command, for a declared output of a node in `needs`. At run time the value must be a file output's path or a version-like token (`[A-Za-z0-9][A-Za-z0-9._+!~-]*`), or the node fails. The value is shell-quoted. `${NAME}` without a dot is shell syntax and is left alone.

The validate graph for the reference case:

```toml
[nodes.lib]
repo = "cyllama"
workflow = "fix-review"
objective = "..."

[nodes.wheel]
repo = "cyllama"
needs = ["lib"]
workflow = "commands"
params = { steps = ["make wheel"], network = true, timeout = 3600 }
outputs = { wheel = "file:dist/*.whl" }

[nodes.app]
repo = "cyllama-desktop"
needs = ["wheel"]
workflow = "check-fix-review"
objective = "Make the app build and pass its tests against the new cyllama wheel"
params = { check = ["CYLLAMA_SOURCE=${wheel.wheel} bash scripts/build-python-env.sh", "make test"], network = true, timeout = 3600 }
inputs = ["lib.review"]
```

`wheel` starts from `lib`'s branch (7.3), so it builds the changed library.

Risk that remains: a command step runs code a coder wrote, such as a changed build script, with network on. The sandbox limits writes and hides `$HOME` and the environment, but the workspace's own contents could still be sent out. On Linux this needs `bwrap` (TODO.md) or `--unsafe-no-sandbox`.

## 8. Phases

1. Session/Run split and the workflow registry. No behaviour change; existing tests pass unchanged. Done; see CHANGELOG, Unreleased.
2. Graph file, projects roots and local projects only (7.1), sequential scheduler, extractors, node results, node budgets, skip-on-fail, one trace. Done; see CHANGELOG, Unreleased. The example in section 4 uses `bump-dep`, which 7.7 replaces; 7.7 has a runnable example.
3. Commits in the copy, branch hand-back, and several nodes on one project (7.2.1). Done; see CHANGELOG, Unreleased.
4. `commands` and `check-fix-review` workflows, and file outputs (7.7). Done; see CHANGELOG, Unreleased. The Linux `bwrap` backend is built too (`docs/dev/bwrap.md`). Command steps with network need a sandbox with network on. Only the macOS backend exists; on Linux they need `bwrap` (TODO.md) or `--unsafe-no-sandbox`.
5. GitHub clones for projects with no local copy (7.2).
6. Effect nodes behind `--allow-effects`.
7. Parallel scheduling. `--resume <run id>` skips nodes whose spec and input values hash to a previous `done` result.

## 9. Open questions

1. Does a dependent wait for its upstream to be merged or released? If so, a graph run lasts hours or days, and durable state and resume become requirements. For a library and its app, 7.5 avoids the wait by splitting at the release. GitHub Actions with `repository_dispatch` may then orchestrate better, running `timu run` once per node.
2. Who writes the graph? A graph written by hand for each change is simple. A graph derived from dependency metadata needs a separate reverse-dependency index.
3. Can a node's output add or remove nodes? A `when` predicate on edges covers simple cases. Anything more needs the alternative in section 10.
4. How should B build against an unpublished A? For a library and its app: a local wheel before the release, the released version after (7.5).
5. Which config does a node use? Decided: a repo contributes no config (CHANGELOG, Unreleased). The graph file sets per-node roles and models, over the user file. Open: whether a node may override `extra_body` or `timeout`.
6. How do approval prompts work across nodes? `tty_approve` asks once per artifact per run. A graph with many nodes asks many times, and parallel nodes would interleave prompts. Options: serialize prompts and label each with its node id; or decide approvals per edge in the graph file before the run starts.
7. How does the app declare the library? For `cyllama-desktop`: a shell default in `scripts/build-python-env.sh`, a floor in `python-sidecar/pyproject.toml`, and `CYLLAMA_SOURCE` for a local build (7.6). How the adopt graph edits the pin is open: a `commands` step (`sed`), or a fix-review node told the new version.
8. Decided: "the app works" means the build and every test pass. No human check is needed. For `cyllama-desktop` this does not yet cover the real library (7.6, point 3).

## 10. Alternative: a delegating graph

A `lead`-like role gets a `run_workflow(repo, workflow, params)` tool and picks the order at run time. It reuses the delegation machinery (design 7) and handles dynamic graphs.

Costs:

- A model chooses the order, so reruns may differ.
- The lead can copy untrusted text into `params`. Schema checks limit this but do not remove it.

The static graph is the default. Revisit this alternative if question 3 shows that dynamic graphs are common.
