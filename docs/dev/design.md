# timu design

Status: draft, 2026-09-25. Nothing here is implemented yet.

timu runs a team of specialised agents toward one objective. Each agent has a role. A role fixes the agent's system prompt, tools, skills and capabilities. A workflow decides which agents run, in what order, and what passes between them.

## Goals

- Define a role in a few lines of data.

- Enforce what a role may do in code and in the OS. The prompt alone does not enforce it.

- Pass work between agents through typed hand-offs, not shared chat history.

- Bound every run by budgets, so a team always stops.

- Record every step, so a run can be inspected and replayed.

## Non-goals (v1)

- Agents running in parallel. Section 9 lists what v1 must avoid to allow it later.

- Agents negotiating freely with each other in a group chat.

- Long-term memory across runs.

- A GUI.

## 1. Terms

| Term | Meaning |
|-|-|
| Provider | Sends messages to a model API and returns a reply. |
| Tool | A function the model may call: schema, handler, required capabilities. |
| Capability | A permission a tool needs: `fs.read`, `fs.write`, `exec`, `net`. |
| Skill | Instructions and optional tools, loaded into an agent's context when needed. |
| Role | Name, system prompt, tools, skills, granted capabilities, default budget. |
| Agent | One tool-use loop running one role with its own message history. |
| Task | What an agent is asked to do: goal, inputs, acceptance criteria, budget. |
| Result | What an agent returns: status, summary, artifacts, usage, trace id. |
| Artifact | A named output with provenance: text, a file path, a diff, a source list. |
| Workflow | A procedure that turns one objective into Tasks for agents and combines their Results. |
| Team | The roles available to a workflow. |

## 2. Roles are data

The first idea was a `BaseAgent` superclass with one subclass per role. Roles differ in prompt, tools and capabilities. None of those needs different code, so a role is a value:

```python
@dataclass(frozen=True)
class Role:
    name: str
    prompt: str
    tools: tuple[Tool, ...]
    grants: frozenset[Capability]
    skills: tuple[Skill, ...] = ()
    budget: Budget = Budget()
```

There is one `Agent` class. It takes a `Role`, a `Provider` and an event sink. Subclass `Agent` only when a role needs a different loop. One example is a planner that must return a JSON plan and nothing else.

Why not subclasses: subclasses tend to override parts of the loop, and every override must then track changes to the base. With data, a new role cannot change loop behaviour by accident. A test can also list every role and check its grants.

## 3. Tools and capabilities

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema
    needs: frozenset[Capability]
    run: Callable[[Context, dict], ToolOutput]
```

Rules:

- Building an agent fails if a tool needs a capability its role does not grant. Nothing is silently dropped.

- The handler receives a `Context` holding the agent's working directory, sandbox and limits. It holds no global state.

- Tool output is capped in size before it enters the context.

- A tool reports failure as a value, not an exception. The model sees the error text and can recover.

### Capability enforcement

The prompt does not enforce a denied capability. Leaving a tool out does not enforce it either. A `shell` tool can run `curl`, so a role with `exec` can reach the network unless the OS blocks it.

| Capability | Enforced by |
|-|-|
| `fs.read` | Path check against allowed roots, after resolving symlinks. |
| `fs.write` | Same, plus atomic writes. Never inside `.git`, where hooks and config can run commands. |
| `exec` | Subprocess in a sandbox. It writes only the write roots and a private temp dir, never `.git`. Under `$HOME` it reads only the read roots and configured toolchain paths; elsewhere reads are allowed. No network. The environment is scrubbed, and `HOME` is the temp dir. |
| `net` | Only the `web_fetch` / `web_search` tools, or an exec sandbox with network on. |

Sandbox backends: `sandbox-exec` with a generated profile on macOS; `bwrap` on Linux (not yet built). If no backend is available, a role with `exec` fails at startup unless the caller passes `NoSandbox()` explicitly. It must not run with network access by mistake.

Limits of the macOS backend:

- Reads outside `$HOME` are not restricted, because toolchains live in `/usr`, `/opt`, `/Library` and elsewhere.
- Under `$HOME`, `stat` and `readlink` are allowed, so symlinked toolchains resolve. A command can learn whether a guessed path exists, but not its contents or a directory listing.
- Toolchains under `$HOME` (uv, pyenv, rustup) must be listed as `extra_read`, or commands that use them fail.
- Two fixed-name temp files are writable outside the write roots, because common tools fail without them: `xcrun_db*` in the per-user temp dir (Xcode shims) and `/tmp/sh-thd*` (here-documents; `/bin/sh` is bash 3.2, which ignores `TMPDIR` for them).

## 4. Tools and roles

Roles differ in two ways:

- which tools they have (4.2);

- the limits those tools run under (4.3).

A tool is written once. The same `read` or `shell` behaves differently per role, because the role's `Context` sets its roots and sandbox.

### 4.1 Tool catalogue

| Tool | Does | Needs |
|-|-|-|
| `read` | Returns a text file's contents, capped. | `fs.read` |
| `list` | Lists a directory, or files matching a glob. | `fs.read` |
| `search` | Searches files for a regex; returns path:line:text. | `fs.read` |
| `write` | Creates or replaces a file, atomically. | `fs.write` |
| `edit` | Replaces one exact occurrence of a string in a file. | `fs.write` |
| `shell` | Runs a command in the sandbox; returns output and exit status. | `exec` |
| `web_search` | Queries the search API; returns titles, URLs, snippets. | `net` |
| `web_fetch` | Fetches an http(s) URL; returns its text. | `net` |
| `load_skill` | Returns a skill's `SKILL.md` body (section 11). | none |
| `read_skill_file` | Returns a file from a skill's directory. | none |
| `delegate` | Runs a child agent for a role; returns its Result (section 7). | none |

`list` and `search` exist so that roles without `exec` can still find code. Otherwise every role would need `shell` to run `ls` or `grep`. They skip gitignored files by calling `git ls-files` with fixed arguments and `core.fsmonitor` off. timu runs that command, not the model, so the tools need only `fs.read`.

`edit` needs `fs.write`, not `fs.read`. It fails if the string is missing, but it does not return the file's contents.

### 4.2 Role x tool matrix

| Tool | `lead` | `researcher` | `coder` | `reviewer` |
|-|-|-|-|-|
| `read` | yes | - | yes | yes |
| `list` | yes | - | yes | yes |
| `search` | yes | - | yes | yes |
| `write` | - | - | yes | mode A: report file only |
| `edit` | - | - | yes | - |
| `shell` | - | - | yes | yes |
| `web_search` | - | yes | - | - |
| `web_fetch` | - | yes | - | - |
| `load_skill`, `read_skill_file` | if skills | if skills | if skills | if skills |
| `delegate` | yes | - | - | - |
| **Grants** | `fs.read` | `net` | `fs.read`, `fs.write`, `exec` | `fs.read`, `exec`; mode A adds `fs.write` |

Why each role lacks what it lacks:

- **`lead`** has no `write` or `shell`. It must delegate changes, so every change goes through a role that the reviewer checks. It reads files so it can check a Result against `accept`.

- **`researcher`** has no filesystem tools. It reads untrusted web content, so it must hold nothing that can be leaked and nothing that can change the workspace (section 6).

- **`coder`** has no network access. Code it writes cannot be guided by content it fetched during the same run. Anything from the web arrives as a wrapped, untrusted input.

- **`reviewer`** cannot change the code it reviews. It has no `edit`, and its sandbox mounts the workspace read-only. How its report reaches disk depends on the report mode (4.3).

Only `lead` has `delegate`, which keeps the delegation tree one level deep by default (section 7).

The skill tools are added only to a role that has skills (`with_skills`), so a role without skills has no tool that can only fail.

### 4.3 Limits per role

The same tool gets different limits from the role's `Context`:

| Limit | `lead` | `researcher` | `coder` | `reviewer` |
|-|-|-|-|-|
| Read roots | workspace | none | workspace | workspace |
| Write roots | none | none | workspace | mode A: report file; mode B: none |
| Shell writes | - | - | workspace, private tmp | private tmp only |
| Shell network | - | - | off | off |
| Shell timeout (default / max) | - | - | 120 s / 600 s | 300 s / 600 s |
| Tool output cap | 16 KB | 32 KB | 16 KB | 32 KB |

#### Reviewer report modes

The workflow sets the report path, from config or a flag such as `--report REVIEW.md` or `--report docs/dev/REVIEW.md`. The model never chooses it. There are two ways the report reaches that path. Experiment E1 (`docs/dev/experiments/e1-reviewer-report.md`) compares them.

| | Mode A: reviewer writes | Mode B: workflow writes |
|-|-|-|
| Role value | `REVIEWER_WRITE` | `REVIEWER` |
| Reviewer grants | `fs.read`, `exec`, `fs.write` (report file only) | `fs.read`, `exec` |
| Who writes | the reviewer, with `write` | the workflow, after the reviewer returns |
| Report travels as | a file | a `report` Artifact in the Result |
| Revise mid-run | yes, by rewriting the file | no; only the final Result counts |
| Path checks live in | the `write` tool (file root) | the workflow's report writer |

Mode B is the default. It keeps the reviewer without `fs.write`, and puts the path checks in one function that the model does not call.

Both modes apply the same checks to the report path:

- If the path is a symlink, the write is refused. Otherwise `REVIEW.md -> src/app.py` would overwrite code.

- The parent directory must exist, and the resolved path must stay inside the workspace.

- In a git repo, timu emits a warning event if the path is not ignored (`git check-ignore`). The report is still written.

- The write is atomic.

In mode A, the report path is a file write root. A file root allows writes to that exact path and nothing else: not a sibling, not a subdirectory. Only `write` reaches it. The shell cannot, so a test run cannot overwrite the report.

In mode B, the report is the reviewer's final message, capped at 256 KB.

In both modes the report's first line is `VERDICT: APPROVE` or `VERDICT: CHANGES`, and the workflow reads it. A report without one fails the run. Guessing would either loop or approve by accident. In mode A, a report file left unchanged by a review round also fails the run, so a stale report from an earlier round is never read as the new one.

The reviewer's shell writes only to a private temp dir. Build tools that write into the tree (`__pycache__`, `.pytest_cache`, `build/`) must therefore be redirected there, for example with `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. If that proves too fragile, run the reviewer on a throwaway copy of the workspace instead.

The coder's shell has no network access, so dependency installs (`uv sync`, `pip install`) fail inside it. Either the workspace is prepared before the run, or a later `installer` role gets `exec` with network turned on and no other tools.

### 4.4 Invariants

`validate(role)` checks these when an agent is built:

1. Every tool's `needs` is a subset of the role's grants.

2. No role holds `net` together with `fs.write` or `exec`.

3. `shell` write access is never wider than the role's write roots. This holds by construction: the sandbox policy is built from the write roots plus the temp dir, and never from `write_files`.

4. Only roles on the delegation allowlist have `delegate`.

5. A file write root lies inside the workspace, and its parent directory exists.

6. `REVIEWER` and `REVIEWER_WRITE` differ only in `write`, `fs.write`, the report write root and the prompt's final instruction.

## 5. Hand-offs

Agents do not share message history. Each agent starts with its role prompt and its Task. Only the Result goes back to the caller.

```python
@dataclass(frozen=True)
class Task:
    goal: str
    inputs: tuple[Artifact, ...] = ()
    accept: str = ""            # acceptance criteria, checked by the caller
    budget: Budget | None = None

@dataclass(frozen=True)
class Result:
    status: Literal["done", "failed", "budget", "refused", "cancelled"]
    summary: str
    artifacts: tuple[Artifact, ...]
    usage: Usage
    trace_id: str
```

Why isolated contexts: each agent's context holds only what its task needs. One agent's tool output does not use up another agent's context window. Failures stay within the agent that caused them. The cost is that the lead must write complete Tasks, because a sub-agent cannot see the lead's conversation.

## 6. Trust and untrusted content

Web content can contain instructions aimed at the model (prompt injection). The risk is highest when one agent holds untrusted input, private data and a way to send data out ("lethal trifecta", https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/).

Rules:

- Every Artifact records its origin: `user`, `fs`, `net`, or an agent id.

- A Result from an agent that read `net` content is marked untrusted, and so is every Artifact derived from it.

- Untrusted Artifacts reach other agents as quoted data in a fixed wrapper, never as the Task goal.

- A workflow may require a human to approve an untrusted Artifact before a role with `exec` or `fs.write` receives it.

How the rules are enforced:

- Two markers. `origin=net` means web content first-hand; `untrusted` marks anything derived from it, such as the summary of a coder that read research. The wrapper uses both. The approval gate asks only about first-hand web content, once per artifact, so a run does not prompt again for every derived summary.
- The wrapper tag carries a random suffix per rendered task (`<untrusted-3f9a1c ...>`). Content cannot close a tag whose name it cannot predict. Unlike escaping `<`, this keeps code in the content verbatim.
- `web_fetch` refuses non-http(s) URLs, and hosts that resolve to private, loopback or link-local addresses, including after a redirect. Otherwise a researcher could reach services on the user's machine or network, such as a local model server or a cloud metadata endpoint. DNS rebinding between the check and the connection is not prevented.

This lowers the risk but does not remove it. The model can still follow instructions it finds inside quoted data. The capability split in section 4 limits the damage when that happens.

## 7. Workflows

Everything below is built on one call: `agent.run(task) -> Result`.

Two kinds of workflow:

1. **Fixed pipeline.** Ordinary Python code calls `run` in a set order. Example: `research -> code -> review`, with at most N review-fix rounds. Easy to predict and test.

2. **Delegation.** The `lead` has a `delegate(role, goal, inputs, accept)` tool. Calling it builds a new agent for that role, runs it, and returns the Result as the tool output. The lead decides the order at run time.

Delegation is a tool around `run`, so both kinds share one code path. Start with fixed pipelines. Add delegation once the pipelines are tested.

These map to the "prompt chaining", "orchestrator-workers" and "evaluator-optimizer" patterns in Anthropic's "Building effective agents" (https://www.anthropic.com/engineering/building-effective-agents).

Delegation limits:

- A maximum depth (default 2), so a delegated agent cannot keep delegating.

- A child's budget is taken from its parent's remaining budget.

- A role can delegate only to roles listed in its configuration.

How delegation passes work:

- `delegate(role, goal, accept, inputs)` takes earlier results by agent id (`inputs: ["a2"]`), not as pasted text. The run attaches each one as an artifact with its real provenance: `origin=net` for a role that has `net`. Pasted text would lose the wrapper and bypass the approval gate.
- The tool result is a JSON line (id, status, untrusted flag, usage), then the child's answer. An untrusted answer is wrapped as in section 6. A child's failure is a result with `is_error` set, not an exception, so the lead can react.
- An agent that receives an untrusted child result is tainted. Its later children start untrusted, because a goal it writes may carry injected instructions. With the approval gate on, such a goal is shown for approval before it reaches a role with `exec` or `fs.write`. The gate is on by default for the `lead` workflow: in a live run, the lead passed research by id and also copied it into the coder's goal, despite a prompt telling it not to.

## 8. Budgets and termination

```python
@dataclass(frozen=True)
class Budget:
    turns: int = 50             # model calls
    tool_calls: int = 100
    tokens: int = 500_000
    cost_usd: float | None = None
    wall_seconds: int = 1800
```

- Every agent checks its budget before each model call. When it runs out, the agent returns `status="budget"` with its work so far.

- The workflow has its own budget, and every child's budget comes out of it. Agents charge the workflow as they spend, not when they finish, so a lead and the children it is waiting on draw from one live remainder. Each agent also stops when the workflow budget runs out. Separate budgets for each branch of the delegation tree are not modeled.

- If the same tool call with the same arguments repeats 3 times in a row, the agent stops with status `failed`.

## 9. Runtime constraints

- No module-level mutable state. Interrupt flags, output settings and usage counters belong to an agent or a run.

- Agents do not print. They send events (`model_delta`, `tool_call`, `tool_result`, `result`) to a sink. The CLI is one sink, and a trace file is another.

- Cancellation uses a per-run token that tools check. A per-process signal flag cannot tell agents apart.

- The v1 loop is synchronous. Parallel agents later means threads or `asyncio`. Keeping state out of globals now keeps that change small.

## 10. Observability

- Each run writes a JSONL trace: one line per event, with the agent id, parent id, role, timestamp and usage. Traces live in `$XDG_STATE_HOME/timu/runs/`, outside the workspace, so the agents they record cannot read or change them.

- A trace holds enough to replay a run with a fake provider. Tests use this. Replay feeds the recorded model replies and the recorded web results back in, and runs local tools for real. It checks that timu made the same decisions: events, tool calls with their arguments, error flags, statuses. It does not check tool output text, which holds temp paths and timings.

- `timu trace <run>` prints the agent tree with cost and status per node.

## 11. Skills

A skill is a directory in the Agent Skills format (https://agentskills.io/specification): a `SKILL.md` with YAML frontmatter (`name`, `description`, optional fields) and a Markdown body, plus optional `scripts/`, `references/` and `assets/`.

Skills load in three stages:

1. The role prompt lists `name` and `description` of each skill the role has.

2. `load_skill(name)` returns the `SKILL.md` body.

3. `read_skill_file(name, path)` returns a file under the skill directory.

This keeps the prompt short as the number of skills grows. Running a skill's `scripts/` needs `exec`, so a role without `exec` can read a script but not run it. A skill cannot give a role more capabilities than the role grants.

A role names its skills; each must exist when the agent is built. A role with skills must list `load_skill` among its tools, so the tool set stays explicit. The shell sandbox can read the role's skill directories, so their scripts run even when they live under `$HOME`.

## 12. Dependencies

The package has no runtime dependencies today, and `tests/test_timu.py::test_has_no_runtime_dependencies` checks this. Everything above works with the standard library except web search, which needs a search API reached over `urllib`. Which search API to use is an open question.

## 13. Testing

- A `FakeProvider` returns scripted replies, so the loop, budgets and hand-offs can be tested without network access.

- Every role is checked for: no disallowed capability combination, all tool needs granted.

- Sandbox tests confirm that `exec` without `net` cannot open a socket.

- Workflow tests replay recorded traces.

## 14. Failure modes

| Failure | Mitigation |
|-|-|
| Lead writes a vague Task; child solves the wrong problem | `accept` field; lead checks the Result against it |
| Agents delegate back and forth without end | Depth limit, shared budget |
| Injected web text makes a coder run commands | No `net`+`exec` role; untrusted wrapper; approval gate |
| Shell reaches the network despite no `net` grant | OS sandbox; fail at startup if unavailable |
| Context overflow in a long agent run | Per-agent context; trim old tool output; budget stop |
| Reviewer approves its own team's broken code | Reviewer runs tests itself; `reviewer` is a separate role that can write only its report |
| Cost runs away | `cost_usd` budget at workflow level |

## 15. Open questions

1. Skills: should `allowed-tools` in `SKILL.md` narrow a role's tools while the skill is loaded, or be ignored? The spec marks it experimental.

2. Search API for `researcher`, and does it justify a runtime dependency?

3. Does `lead` need `fs.read`, or should it only see Results?

4. Should roles live in Python, or in TOML/Markdown files that users can add without code?

5. Which model per role? Cheaper models for `researcher` and `reviewer` would cut cost. Is quality then good enough?

6. Human approval: required at which points by default?

7. How are file changes merged when several coders exist? A git worktree per coder is one option.

8. Sandboxing is to move to `sanduk`. sanduk runs a whole agent in a disposable Linux container; it does not wrap single commands. The integration shape is undecided: (a) each exec role's agent runs inside a sanduk container, with its tools unwrapped there; or (b) sanduk exposes per-command execution behind timu's `Sandbox` protocol. Until then, `MacSandbox` is interim.

## 16. Alternative framing

The design above treats the team as a program: Python code, or a `lead` agent, calls workers. Another option is a shared task board. Agents claim tasks by role, post Results, and add follow-up tasks. There is no central lead.

That fits long-running work with many independent tasks. It is harder to bound and debug, because no single agent owns the objective. Revisit it if delegation depth or the lead's context becomes the limit.
