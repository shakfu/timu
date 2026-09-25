# timu implementation plan

Status: draft, 2026-09-25. Implements `docs/dev/design.md`; section numbers below ("design 3") refer to it.

## Scope

In scope: the v1 in `design.md`. That is four roles (`lead`, `researcher`, `coder`, `reviewer`), fixed pipelines, delegation, budgets, traces, and Agent Skills (`SKILL.md`).

Out of scope:

- `sanduk`. timu defines its own `Sandbox` protocol (phase 3) and ships only the minimal backends it needs to enforce design 3.

- Running agents in parallel, memory across runs, a GUI (design non-goals).

- The alternative task-board design (design 16).

## Decisions needed before phase 1

| # | Decision | Proposed default |
|-|-|-|
| D1 | Minimum Python | 3.11: `tomllib`, `StrEnum`, `Popen(process_group=)`. 3.10 gains little. |
| D2 | First model API | OpenAI-compatible chat completions. One adapter covers OpenRouter, llama-server, vLLM and Ollama. |
| D3 | Web search API | Brave Search over `urllib`. No runtime dependency. Needs a key. |
| D4 | Role definitions | Python in v1 (`roles.py`). File-based roles later (design 15.4). |
| D5 | Unsupported sandbox | Refuse to start a role that has `exec` without `net`. `--unsafe-no-sandbox` overrides. |
| D6 | `allowed-tools` in `SKILL.md` | Parse and store it, but do not enforce it in v1. |

D2 and D3 are the ones most likely to change the module layout.

## Module layout

```
src/timu/
  __init__.py        public API re-exports
  types.py           Capability, Budget, Usage, Origin, Artifact, Task, Result
  events.py          Event, EventSink, JsonlSink; a console sink in phase 6
  provider/
    base.py          Provider protocol, Message, ToolCall, Reply
    fake.py          FakeProvider: scripted replies for tests
    openai.py        OpenAI-compatible chat completions, streaming
  tool.py            Tool, Context, ToolOutput, output capping
  tools/
    fs.py            read, list, search, write, edit
    shell.py         shell (runs through a Sandbox)
    web.py           web_fetch, web_search
    skill.py         load_skill, read_skill_file
    delegate.py      delegate
  sandbox.py         Sandbox protocol, MacSandbox, BwrapSandbox, NoSandbox
  skills.py          discovery, frontmatter parsing, Skill
  report.py          write_report(): report path checks, atomic write
  role.py            Role, validate()
  roles.py           LEAD, RESEARCHER, CODER, REVIEWER
  prompts/*.md       role system prompts, shipped as package data
  agent.py           Agent: the tool-use loop
  workflow.py        Run (budget tree, ids), pipeline helpers
  trace.py           trace reading and replay
  cli.py             `timu run`, `timu trace`
tests/
  conftest.py        FakeProvider fixtures, tmp workdirs
  test_<module>.py   one file per module
  fixtures/skills/   sample skill directories
  fixtures/traces/   recorded traces for replay tests
```

The placeholder `core.py` (`add`, `greet`) and the myra `agent.py` are deleted in phase 0.

## Phases

Each phase ends with `make qa` passing: ruff, format check, `mypy --strict`, and pytest. Each phase lists its exit criteria. A phase starts only when the previous one meets them.

### Phase 0: housekeeping

- Delete `core.py`, the myra `agent.py`, and `tests/test_timu.py::test_add`/`test_greet`.

- Set `requires-python` per D1. Update classifiers.

- Keep `test_has_no_runtime_dependencies`.

- `[project.scripts] timu = "timu.cli:main"` moves to phase 6, when `cli.py`
  exists. An entry point to a missing module would install a broken command.

- Replace the placeholder `README.md` with a stub pointing to `docs/dev/design.md`.

Exit: `make qa` passes on an empty package.

### Phase 1: core types, events, agent loop

Goal: an agent runs on a `FakeProvider` with a trivial tool and stops correctly.

- `types.py`: the frozen dataclasses from design 5 and 8. `Capability` is a `StrEnum`: `fs.read`, `fs.write`, `exec`, `net`.

- `events.py`: `Event(kind, agent_id, parent_id, role, ts, data)`. `JsonlSink` writes one event per line.

- `provider/base.py`: timu's own message format. Adapters convert to and from the wire format, so the loop never sees the wire format.

- `provider/fake.py`: returns scripted replies and records the requests it got.

- `agent.py`: `Agent(role, provider, sink, workdir, cancel).run(task) -> Result`.

  - Budget checks before each model call (design 8).

  - Stops after 3 identical consecutive tool calls.

  - Every tool call gets a result, so the history stays valid.

  - Tool exceptions become error results.

  - No module-level mutable state (design 9).

Tests:

- Each `Result.status`: `done`, `failed`, `budget`, `refused`.

- Each budget limit triggers at the right count.

- The repeated-call detector fires at 3, not 2.

- A cancelled token stops the run between tool calls.

- Events arrive in order, with correct ids.

- A static check that no module-level variable of the package is reassigned (lint, or an AST test).

Phase 1 also adds minimal `tool.py` (`Tool`, `Context` with `workdir`, `cancel`,
`max_output`) and `role.py` (`Role`, no `validate`). Phase 2 extends both.
Cancellation uses `threading.Event`; no `CancelToken` class.

Exit: a scripted 5-turn conversation with 5 tool calls (a batch of 2, and an
unknown tool the model recovers from) produces the expected `Result`, message
history and event stream.

### Phase 2: filesystem tools and capability checks

- `tool.py`: `Tool` from design 3. `Context` holds `workdir`, `read_roots`, `write_roots`, `sandbox`, `cancel` and `max_output`. Each role builds its own `Context` from the limits in design 4.3.

- `tools/fs.py`: `read`, `list`, `search`, `write`, `edit` (design 4.1).

  - Paths are resolved with symlinks followed, then checked against the roots.

  - Writes are atomic: temp file plus `os.replace`.

  - `edit` requires exactly one match.

  - Binary files are refused.

  - Output is capped, keeping the head and tail.

  - `search` uses `re` and skips binary files and anything `.gitignore`d.

- `role.py`: `validate(role)` checks the invariants in design 4.4 that exist by now: 1, 2 and 5, plus duplicate tool names and write roots without `fs.write`. Rule 3 moves to phase 3 (`shell`), 4 to phase 8 (`delegate`), 6 to phase 6 (reviewer roles). `make_context(role, workdir, cancel)` resolves the role's roots; `Agent` calls it, so a bad role fails at construction.

Tests:

- Paths outside the roots, `..` escapes and symlink escapes are refused.

- A failed write leaves the original file unchanged.

- `edit` with 0 matches and with 2 matches.

- `validate` rejects every forbidden combination in design 4.4.

- File write roots (design 4.3): the exact path is writable; a sibling, a subdirectory and a symlink at the path are refused.

- The role-matrix test moves to phase 6, where `roles.py` is created.

Exit: a `FakeProvider`-driven agent edits a file in a temp workdir, and cannot touch a file outside it.

### Phase 3: sandbox and shell

- `sandbox.py`: `Sandbox` protocol with `wrap(argv, policy) -> argv`. A backend only rewrites the command; `shell.py` owns process groups, timeouts and cancel for every backend. There is no `net` flag, since invariant 2 forbids `exec` with `net`.

  - `MacSandbox(home, extra_read)`: `sandbox-exec` with a generated profile. See design 3 for what it allows.

  - `BwrapSandbox`: deferred. No Linux host was available to test it, and an untested sandbox is worse than none. Until it exists, Linux fails closed.

  - `NoSandbox`: used only when passed explicitly (later `--unsafe-no-sandbox`). The agent emits one `warning` event per run; tools cannot emit events.

  - `detect()` picks a backend. If none is found, agent construction fails for a role with `exec`.

- `tools/shell.py`:

  - Runs in its own process group.

  - Kills the whole group on timeout or cancel.

  - Merges stdout and stderr, caps them, and appends the exit status.

Tests (skipped where the backend is missing, with the reason printed):

- Inside the sandbox, `curl` and a raw `socket.connect` fail.

- A write outside the write roots fails.

- In the reviewer sandbox, a shell write to the workspace (including `REVIEW.md`) fails and a write to the private temp dir succeeds.

- `pytest` runs in the reviewer sandbox with the cache and bytecode redirected (design 4.3).

- A timeout kills grandchild processes too.

- Cancel stops a running command within 1 s.

Risk: `sandbox-exec` is deprecated on macOS but still works in current releases. This is an inference; verify on the macOS versions you run. If it stops working, a `Sandbox` backend from `sanduk` is the fallback. That integration is outside this plan.

Exit: `CODER` runs `make test` in a temp repo through the sandbox, and has no network access.

### Phase 4: real provider

- `provider/openai.py`: chat completions over `urllib` (D2).

  - SSE streaming. Text deltas become `model_delta` events.

  - Tool-call deltas are merged by index.

  - Retries 429, 5xx and network errors with backoff.

  - Detects context-length errors.

  - Validates the type of every field in the response, since the response is untrusted input.

- Config: base URL, key env var, and model per role. Uses `tomllib` for a `timu.toml` (`./timu.toml`, else `~/.config/timu/timu.toml`), with `TIMU_BASE_URL` and `TIMU_MODEL` overrides. There is no default model: ids change too often to hard-code one.

- A context-length error is not retried; the run fails. Dropping old tool output to fit is left until traces show it is needed.

- A request in flight is not cancellable from the cancel event. Ctrl-C in the CLI (phase 6) interrupts it with `KeyboardInterrupt`.

Tests:

- SSE parsing from recorded byte streams, including split lines, a missing `[DONE]`, and an error mid-stream.

- Malformed JSON and wrong field types produce `failed`, not an exception.

- One live test, marked `@pytest.mark.live`. It runs only with `TIMU_LIVE=1`, not whenever a key is set, so `make test` never bills.

Exit: `CODER` completes a small real task against a real model.

### Phase 5: skills

- `skills.py`:

  - Lookup order: `.timu/skills/` in the workdir, then `~/.config/timu/skills/`. The first match by name wins. Only the skills a role names are parsed, since the spec makes the name equal the directory name. No skills ship in the package yet, so there is no package root.

  - Frontmatter parser, stdlib only. It supports the subset the spec uses: scalars,
    quoted strings, `>` and `|` blocks, and one level of mapping for `metadata`.
    Anything else is a parse error naming the file and line. It is not a general
    YAML parser.

  - Validates `name` against the spec: lowercase, digits and hyphens, at most 64 characters, and equal to the directory name. `description` must be non-empty and at most 1024 characters.

- `tools/skill.py`:

  - `load_skill(name)` returns the body.

  - `read_skill_file(name, path)` returns a file, confined to the skill directory.

  - Neither tool needs a capability, because the skill directory is a trusted, read-only root.

- The role prompt ends with an `<available_skills>` list of name and description pairs.

Tests:

- Valid and invalid frontmatter fixtures.

- Name and directory mismatch.

- Path escape in `read_skill_file`.

- Discovery precedence.

- The prompt lists only the role's skills.

Exit: a skill's instructions change a `FakeProvider`-scripted agent's tool choice, and a live agent loads a fixture skill when its description matches the task.

### Phase 6: pipelines, coder and reviewer, CLI

- `roles.py` and `prompts/`: `CODER`, `REVIEWER` (report mode B) and `REVIEWER_WRITE` (mode A). Both reviewer prompts share one body and differ only in the final instruction (design 4.3).

- `report.py`: `write_report(path, text, workspace)`. It refuses symlinks, checks the parent and the workspace, writes atomically, and warns when the path is not gitignored. Mode B calls it from the workflow. Mode A's file root uses the same checks.

- `workflow.py`:

  - `Run` owns the root budget, the id allocation and the sink.

  - `run.agent(role).run(task)` subtracts the child's usage from the parent's budget.

  - `pipeline_fix_review(objective, max_rounds)` is the first built-in workflow.

- `cli.py`:

  - `timu run --workflow fix-review "<objective>"`: streams events to the console and writes `$XDG_STATE_HOME/timu/runs/<id>.jsonl`. Not `.timu/runs/`: a trace in the workspace would be writable by the coder it records.

  - `[roles.<name>] skills` and `[sandbox] extra_read` in `timu.toml`. `extra_read` is interim until sandboxing moves to sanduk (design 15.8).

  - `--report PATH` sets the report path. `--report-mode return|write` picks mode
    B (default) or A.

  - Exit codes: 0 done, 1 failed, 2 usage error, 3 budget, 130 interrupted.

  - Ctrl-C sets the run's cancel `threading.Event`.

Tests:

- The pipeline stops at `max_rounds`.

- The reviewer's findings reach the next coder Task as input artifacts.

- Mode B: the report Artifact is written to `--report PATH`. A missing Artifact gives `failed`. An Artifact over 256 KB is refused.

- Mode A: the reviewer can write `--report PATH` and nothing else.

- Both modes: a symlink at the path is refused, and a path that is not gitignored emits a warning event but is still written.

- The two reviewer roles differ only in the fields listed in design 4.4 rule 6.

- A budget shared across the pipeline stops the pipeline.

Exit: `timu run` fixes a seeded bug in a fixture repo, and the reviewer confirms the tests pass.

### Experiment E1: reviewer report mode

Runs after phase 6, before phase 7. It compares report modes A and B on live models. The procedure and decision rule are in `docs/dev/experiments/e1-reviewer-report.md`.

- `evals/e1/`: fixture repos with seeded defects, and a runner script.

- `make eval-e1`: live and billed, so it is not part of `make test`.

Exit: the E1 result is recorded, and the default report mode is confirmed or changed.

### Phase 7: researcher and provenance

- `tools/web.py`:

  - `web_fetch(url)`: `http(s)` only, with a size cap and a timeout.

  - HTML is reduced to text with `html.parser`.

  - `web_search(query)` uses the API from D3.

- `researcher(search)` role factory: `web_search` needs an API key, so the tool is built from `[search] api_key_env` in `timu.toml`. Without a key the researcher can fetch but not search, and the CLI warns. The workflow takes source URLs from the researcher's final message into a `sources` artifact.

- Provenance (design 6):

  - An agent that called a `net` tool marks its Result and artifacts `Origin.NET`.

  - Every artifact derived from one inherits the mark.

  - Untrusted artifacts go into a Task only inside the fixed `<untrusted source="...">` wrapper.

- Optional approval gate: `--approve-untrusted` asks on the terminal before first-hand web content reaches a role with `exec` or `fs.write`, once per artifact. A denial ends the run with status `refused`.

Tests:

- Only an agent that called a `net` tool gets the untrusted mark, and derived artifacts inherit it.

- The wrapper is always applied, and cannot be closed early by content that contains the closing tag.

- `web_fetch` rejects `file://`, and is capped in size.

- The approval gate blocks the hand-off when approval is denied.

Exit: a `research -> code -> review` pipeline completes, and the trace shows the research output wrapped as untrusted in the coder's Task.

### Phase 8: delegation and lead

- `tools/delegate.py`: `delegate(role, goal, inputs, accept)`.

  - Builds a child agent through the `Run`.

  - Enforces the depth limit and the role allowlist (design 7).

  - Returns a JSON line (id, status, untrusted, usage), then the child's answer, wrapped if untrusted. Earlier results are passed by id.

  - The workflow budget is charged live, so children spend from the lead's remainder (design 8).

- `LEAD` role and prompt. The prompt asks the lead to write complete Tasks and to check each Result against `accept`.

- `timu run --workflow lead "<objective>"`.

Tests:

- Depth limit reached.

- A role not on the allowlist is refused.

- The child's budget comes out of the parent's.

- A child failure comes back as a tool result, not an exception.

- The trace shows the parent and child tree.

Exit: the lead completes an objective that needs both research and code, using only delegation.

### Phase 9: traces and replay

- `trace.py`: loads a JSONL trace, rebuilds the agent tree, and replays a run by giving the recorded model replies to a `FakeProvider`.

- `timu trace <id>`: prints the tree with role, status, turns, tokens and cost for each node.

- Recorded traces from phases 6-8 become regression fixtures. `TIMU_KEEP_TRACES=tests/fixtures/traces` makes the live CLI tests save theirs, with a `.json` file naming the scenario. `tests/scenarios.py` holds the workspace and objective each scenario starts from, shared by the live and replay tests. Saved traces have the temp dir, `$HOME` and the username replaced by `{TMP}`, `{HOME}` and `{USER}`. Replay points `{TMP}` at its own temp dir, so absolute paths in recorded tool calls still resolve.

Tests:

- Replay produces the same event stream, ignoring timestamps.

- A trace with a missing line fails replay with the line number.

Exit: every workflow in `fixtures/traces/` replays deterministically under `make test`.

## Order and dependencies

```
0 -> 1 -> 2 -> 3 -> 4 -> 6 -> E1 -> 7 -> 8 -> 9
               \-> 5 -/
```

Phase 5 (skills) depends only on phases 1 and 2, so it can move earlier. Phase 4 can also come before phase 3 if a real model is wanted sooner. Phase 6 needs both 3 and 4.

## Risks

| Risk | Effect | Mitigation |
|-|-|-|
| Sandbox backend unavailable or deprecated | `CODER` cannot run safely | D5 refusal; `Sandbox` protocol allows a later backend |
| Frontmatter subset rejects real-world `SKILL.md` files | Skills fail to load | Error names file and line; widen the subset from actual failures |
| OpenAI-compatible servers differ in tool-call streaming | Broken tool calls on some servers | Strict type checks; recorded stream fixtures per server |
| Lead writes poor Tasks | Wasted child runs | `accept` field; phase 8 exit test; prompt iteration using traces |
| Live-model tests are flaky | CI noise | Live tests opt-in only; replay tests carry the regression load |

## Alternative ordering

The plan builds the platform before any role is useful, so a real model first runs in phase 4. The alternative is a vertical slice: build `CODER` end to end in one phase (phases 1, 2, 3 and 4 at minimal depth), then deepen each layer. That gets feedback from real models sooner, at the cost of reworking the loop and provider interfaces once phases 5-8 put demands on them. It is worth choosing if the prompts and role split are the main uncertainty. The plan above is better if the safety properties are.
