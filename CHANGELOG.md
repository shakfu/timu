# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- timu no longer reads `./timu.toml`. Config comes only from the user file, `--config` and the environment. The file could still set models, `extra_body` and `[roles]`. On OpenRouter the model id picks the vendor that receives your code, and `extra_body` can change provider routing, so a cloned repo could choose where your code went. The file was also read from the current directory, not from `-C`. The coder could write `timu.toml` in a subdirectory, and a later run started there would load it.

- A user skill now wins over a workspace skill of the same name in `./.timu/skills`. Before, the repo's copy replaced it, so a cloned repo could change the instructions of a skill the user's config named. A repo can still add skills with new names.

### Added

- Each run prints its config sources on one line (`config: <file> + TIMU_MODEL`) and records them in the trace.

- `timu graph run FILE`: a graph of workflows across local projects, found under `[projects] roots`. Each node runs on a `git clone --local` copy, where timu commits its changes on a branch for you to fetch. Agents cannot commit: a node whose `HEAD` moved during its workflow fails. Several nodes may change one project in sequence, each starting from the last one's branch. Declared outputs pass to downstream nodes as schema-checked `params` or as `inputs`. A failed node skips its descendants only. Nodes can have their own budgets, and the graph writes one trace, with each agent tagged by node. This is phases 2 and 3 of `docs/dev/graph.md`.

- Graph workflows `commands` and `check-fix-review`, and `file:GLOB` outputs (graph.md 7.7). They run fixed commands in the sandbox, with network only if the node asks for it. No model runs them, so no agent ever holds a shell and network together. `${node.output}` in a command takes only a file path or a version-like token, shell-quoted: a version string such as `1; rm -rf ~` fails the node. Output from a step with network on is marked untrusted.

- Linux sandbox backend with `bwrap`. It denies network access, hides `$HOME` behind an empty tmpfs, and mounts the filesystem read-only except the role's write roots. It keeps `.git`, `timu.toml` and `.timu` at each write root read-only. timu probes `bwrap` at startup and, where it cannot run, says why. Unlike the macOS profile, it cannot block a path that does not exist yet. So a command may create a new `.git`, `timu.toml` or `.timu`, but may not change existing ones.

### Changed

- Workflows are registered in `WORKFLOWS` (`workflow.py`). The CLI takes its `--workflow` choices, provider checks and approval default from there, not from three hardcoded lists.

- A `Session` holds what the agents of one invocation share: budget, run id, sink, cancel flag and approvals. `Run` binds it to one workdir, and `Run.at(path)` gives another `Run` in the same session. This is phase 1 of `docs/dev/graph.md`.

## [0.2.0]

### Security

- `./timu.toml` may set only models, timeouts, `extra_body` and `[roles]`; other keys come from the user config, the environment or `--config`. It overlays the user config instead of replacing it. Before, a cloned repo's `timu.toml` could set `base_url` and `api_key_env`, and timu sent that environment variable as a bearer token to the repo's server. It could also add `$HOME` paths to `sandbox.extra_read`.

- Agents cannot write `timu.toml` or `.timu/` at the workspace root, with the `write` tool or in the sandbox, so one run cannot change the config or skills of the next. The `write` tool's `.git` check now ignores case: `.GIT/config` passed it on case-insensitive macOS volumes.

### Added

- A warning when a skill in `./.timu/skills` replaces a user skill of the same name. Workspace skills still take precedence; the warning shows that yours did not run.

- Agent loop: `Agent(role, provider, sink, workdir).run(task) -> Result`. It stops on a final answer, provider error, refusal, cancel, any `Budget` limit, or 3 identical consecutive tool calls. Design in `docs/dev/design.md`, phases in `docs/dev/plan.md`.

- Core types (`Task`, `Result`, `Budget`, `Usage`, `Artifact`, `Capability`), `Role`, `Tool`, JSONL event traces, and `FakeProvider` for scripted tests.

- Filesystem tools `read`, `list`, `search`, `write`, `edit`. Paths are resolved through symlinks and checked against the role's read and write roots; a write root can be a single file. Writes are atomic and never go inside `.git`. `list` and `search` skip gitignored files.

- Role checks at agent construction: every tool's capability is granted, `net` is never combined with `fs.write` or `exec`, and single-file write roots are inside the workdir and not symlinks.

- `shell` tool, run in a macOS `sandbox-exec` sandbox: no network, writes only to the role's write roots and a per-run temp dir, no reads of `$HOME` beyond the role's roots and configured toolchain paths. The environment is scrubbed, so API keys in the parent do not reach commands. Here-documents work: `/bin/sh` (bash 3.2) writes them to `/tmp/sh-thd*` whatever `TMPDIR` says, and only that name is allowed there. Timeout and cancel kill the whole process group. A role with `exec` fails to build where no sandbox exists (Linux, for now) unless `NoSandbox()` is passed.

- `OpenAIProvider`: OpenAI-compatible chat completions over `urllib`, streamed, for OpenRouter, OpenAI, llama-server, vLLM and Ollama. Retries 429, 5xx and network errors, but not after text has been streamed, since a retry would repeat it. Every response field is type-checked, and malformed responses fail the run instead of raising. OpenRouter `reasoning_details` are returned to the model on the next turn.

- `timu.toml` config for the base URL, key variable, model, and per-role models. `TIMU_BASE_URL` and `TIMU_MODEL` override it.

- Agent Skills (`SKILL.md`, https://agentskills.io/specification). A role names its skills; the system prompt lists each one's name and description; `load_skill` and `read_skill_file` load the rest on demand. The frontmatter parser handles the YAML subset skills use and reports anything else with file and line, which keeps the package free of dependencies.

- `timu run "<objective>"`: the fix-review workflow. A coder changes the code, then a reviewer runs the tests and returns a report whose first line is `VERDICT: APPROVE` or `VERDICT: CHANGES`. Findings go back to the coder, for up to `--max-rounds` rounds. The report goes to `--report` (default `REVIEW.md`). With `--report-mode write`, the reviewer writes it itself and can write nothing else. All agents share one budget, and `--max-cost` caps it. The exit code is 0 when approved, 1 on failure, 2 on a usage error, 3 when the budget runs out, and 130 when interrupted. Traces go to `$XDG_STATE_HOME/timu/runs/`.

- `--workflow research-fix-review`: a researcher with `web_search` (Brave, `[search] api_key_env`) and `web_fetch` runs first, and its findings reach the coder. Web content, and anything derived from it, goes into a later agent's task only inside a tag with a random suffix, which the content cannot close. `--approve-untrusted` asks on the terminal before web content reaches the coder. `web_fetch` refuses non-http(s) URLs and private, loopback and link-local addresses, including after redirects.

- `--workflow lead`: a lead agent meets the objective by delegating to the researcher, coder and reviewer. `delegate` passes earlier results by id, so web content stays marked untrusted in the child's task. Every agent in a run draws live from one budget, so children spend from what the lead has left. A lead that has read web content passes that taint to the children it starts, and the goals it writes need approval before they reach the coder. The approval gate is on by default for `lead`, because a live run showed the lead copying research into the coder's goal despite its prompt. Without a terminal the answer is no; pass `--no-approve-untrusted` for unattended runs.

- `timu trace [RUN]` prints a run's agent tree, with status, turns, tool calls, tokens and cost for each agent. With no argument, it shows the latest run.

- Trace replay (`timu.trace`) runs a recorded run again with the recorded model replies and web results, and reports the first trace line where timu decided differently. Recorded runs in `tests/fixtures/traces/` replay under `make test`. Traces saved as fixtures have local paths and the username replaced by placeholders.

### Changed

- Requires Python 3.11 or later.

### Removed

- Placeholder `add` and `greet` functions.

## [0.1.0]

### Added

- Initial project structure

- Core module with example functions

- Test suite with pytest

- Build system using uv_build
