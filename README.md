# timu

**timu**—Swahili for *team*—brings together specialised LLM agents, each with a defined role that determines its prompt, tools, and permissions. Workflows coordinate these roles to achieve a shared objective.

Status: alpha. The design is in `docs/dev/design.md`.

## Architecture

![timu architecture: the CLI starts a workflow; a Run builds one agent per task from a role; agents call the model and run tools, with shell commands in a sandbox; events go to the console and a JSONL trace](docs/media/architecture.svg)

The source is `docs/media/architecture.d2`. `make diagrams` regenerates the SVG.

## Roles

| Role | Can | Cannot |
|-|-|-|
| `lead` | read the workspace, delegate to the others | write files, run commands |
| `researcher` | search and fetch the web | touch the workspace |
| `coder` | read, write and edit files, run commands | reach the network |
| `reviewer` | read files, run tests, return a report | change the code |

No role holds network access together with the ability to write files or run commands. Web content, and anything derived from it, reaches other agents only as marked, untrusted data. See design section 6.

## Workflows

A workflow turns one objective into tasks for agents and combines their results. It decides which roles run, in what order, and what passes between them. timu has two kinds (design section 7):

- **Pipeline**: Python code starts agents in a fixed order.
- **Delegation**: a `lead` agent starts other agents with its `delegate` tool, in an order it picks at run time.

The built-in workflows, in `src/timu/workflow.py`:

| Workflow | Kind | Steps |
|-|-|-|
| `fix-review` (default) | pipeline | Coder, then reviewer. The report's first line is `VERDICT: APPROVE` or `VERDICT: CHANGES`. On `CHANGES`, the coder gets the report and tries again. Stops on approval or after `--max-rounds` (default 3). A report with no verdict fails the run. |
| `research-fix-review` | pipeline | Researcher first. Its findings and source URLs go to the coder as untrusted inputs. Then `fix-review`. |
| `lead` | delegation | The lead delegates to the researcher, coder and reviewer. Delegation depth is at most 2. |

All agents in a workflow share one `Run`: one budget, one trace, one cancel flag. Each agent gets the smaller of its role's budget and what the run has left. Agents pass work as artifacts that record their origin. They never paste it into a goal, so the untrusted mark survives.

### Adding a workflow

Workflows are code. `timu.toml` cannot define one, and there is no plugin hook. A workflow is a function that takes a `Run` and an objective and returns a `Result`:

```python
def code_then_review(run: Run, objective: str) -> Result:
    coded = run.run_agent(CODER, Task(objective, accept="The tests pass."))
    if coded.status == "done":
        work = Artifact("coder-summary", coded.summary, origin=Origin.AGENT,
                        source=coded.trace_id, untrusted=coded.untrusted)
        coded = run.run_agent(REVIEWER, Task("Review the coder's work.", (work,)))
    run.emit("workflow_result", status=coded.status, summary=coded.summary)
    return replace(coded, usage=run.used, trace_id=run.run_id)
```

To expose it, add a `Workflow` to `WORKFLOWS` in `src/timu/workflow.py`. It names the roles whose providers the CLI checks before the run, and whether `--approve-untrusted` defaults to on. The CLI builds `--workflow` from `WORKFLOWS`.

## Requirements

- Python 3.11 or later. No runtime dependencies.

- A sandbox for the `shell` tool: `sandbox-exec` on macOS, or `bwrap` (bubblewrap) on Linux. Without one, roles that run commands refuse to start unless you pass `--unsafe-no-sandbox`, and timu says why.

  Ubuntu 24.04 and later stop `bwrap` from creating user namespaces (https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces). timu then reports `bwrap cannot create a sandbox here`. An AppArmor profile for `/usr/bin/bwrap` with the `userns` rule allows it; see `docs/dev/bwrap.md`.

- An OpenAI-compatible model API: OpenRouter by default, or a local server such as llama-server.

- Optional: a Brave Search API key in `BRAVE_API_KEY`, so the researcher can search as well as fetch.

## Install

```sh
uv sync            # from a checkout; `make help` lists the other targets
uv run timu --help
```

## Configure

timu reads `~/.config/timu/timu.toml`. A minimal file:

```toml
[provider]
model = "openai/gpt-6-luna"              # an OpenRouter model id
# base_url = "http://127.0.0.1:8080/v1"  # a local server instead
# api_key_env = ""                       # a server without a key

[sandbox]
extra_read = ["~/.local/share/uv/python"]  # toolchains under $HOME
```

timu does not read a `timu.toml` in the workspace or the current directory. Models, roles and request settings are yours to choose, not a cloned repo's. `--config PATH` replaces the user file. Each run prints the config it used, for example `config: ~/.config/timu/timu.toml + TIMU_MODEL`. Agents cannot write `timu.toml` or `.timu/` in the workspace.

The API key comes from `OPENROUTER_API_KEY`, or the variable `api_key_env` names. `TIMU_MODEL` and `TIMU_BASE_URL` override the file. `src/timu/config.py` documents every key, including per-role models, skills and the search key.

The sandbox hides `$HOME` from commands. List any toolchain that lives there, such as uv's Python, in `extra_read`, or commands that use it fail.

## Run

```sh
timu run "make test fails; fix calc.py"
timu run --workflow research-fix-review "upgrade to the current tomllib API"
timu run --workflow lead "add a --json flag to the report command"
```

`--workflow` picks one of the workflows above. The reviewer's report goes to `REVIEW.md`, or `--report PATH`. Add it to `.gitignore`; timu warns if it is not ignored.

Useful flags:

- `--max-cost USD` stops the run at a spending limit. All agents share one budget.

- `--approve-untrusted` asks on the terminal before web content reaches the coder. It is on by default for `lead`. Without a terminal the answer is no; pass `--no-approve-untrusted` for unattended runs.

- `-v` shows every tool result.

Exit codes: 0 approved or done, 1 failed or refused, 2 usage error, 3 budget spent, 130 interrupted. Press Ctrl-C once to stop after the current step, twice to abort.

## Graphs

A graph runs several workflows across projects, in dependency order. Each node is one workflow on one project. A node can pass outputs to the nodes that need it. The design is in `docs/dev/graph.md`.

List where your projects live in `~/.config/timu/timu.toml`:

```toml
[projects]
roots = ["~/projects"]
```

A graph file names projects by directory name, or as `owner/name` to also check the GitHub `origin`:

```toml
[nodes.a]
repo = "a"
workflow = "fix-review"
objective = "Add a --json flag; bump the minor version"
outputs = { version = "toml:pyproject.toml#project.version", notes = "changelog:latest" }

[nodes.b]
repo = "org/b"
needs = ["a"]
workflow = "fix-review"
objective = "Use a's new --json flag"
inputs = ["a.notes", "a.review"]
```

```sh
timu graph run graph.toml
```

- Each node works on a `git clone --local` copy under `~/.local/state/timu/work/`. Your checkouts are not changed. Uncommitted changes in them are not copied, and timu warns about them.
- timu commits each node's changes on a branch in its copy, as `timu <timu@localhost>`, with your hooks and signing off. Agents cannot commit, and a node whose `HEAD` moved fails. The run prints `git -C <project> fetch <copy> <branch>:<branch>` for each node that changed something.
- If a node does not finish `done`, the nodes that need it are skipped. Other nodes still run.
- Outputs: `toml:FILE#KEY`, `file:GLOB`, `changelog:latest` (the first released section of `CHANGELOG.md`) and `git:diff`. A node also exposes its workflow's results, such as `a.review` or `a.log`.
- `file:GLOB` must match one file. timu copies it out of the copy, and the output is the copy's path. Nodes that need it may read it.
- `${node.output}` may appear only in `params`, and each param is checked against the workflow's schema. Free text reaches a node only through `inputs`.
- Two workflows run commands from the graph file in the sandbox, with no model. `commands` runs `steps` in order. `check-fix-review` runs `check`; if it fails, it runs fix-review with the failure log as an input and checks again, up to `max_rounds`. Both take `network = true` and `timeout` in seconds per command. In a command, `${node.output}` is replaced by a file path or a version-like value, shell-quoted; anything else fails the node.

  ```toml
  [nodes.wheel]
  repo = "lib"
  workflow = "commands"
  objective = "Build the wheel"
  params = { steps = ["uv build --wheel"], network = true, timeout = 1800 }
  outputs = { wheel = "file:dist/*.whl" }

  [nodes.app]
  repo = "app"
  needs = ["wheel"]
  workflow = "check-fix-review"
  objective = "Make the app pass its tests with the new wheel"
  params = { check = ["uv pip install ${wheel.wheel}", "make test"], network = true }
  ```

- `budget = { cost_usd = 1.0 }` on a node, or under `[defaults]`, caps that node's agents. `--max-cost` caps the whole graph.
- Several nodes may use one project. A node starts from the branch of its nearest ancestor on that project.

## Traces

Every run writes a JSONL trace to `~/.local/state/timu/runs/`, outside the workspace.

```sh
timu trace              # the latest run: agent tree, status and cost
timu trace <run id>
```

## Develop

```sh
make qa                 # lint, format check, typecheck, tests
TIMU_LIVE=1 make test   # also the live tests; billed against the configured model
```

The live tests are skipped by default. With `TIMU_KEEP_TRACES=tests/fixtures/traces`, the CLI live tests save their traces as replay fixtures, with local paths and your username replaced by placeholders. `make test` replays every fixture there.
