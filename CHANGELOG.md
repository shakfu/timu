# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- `[provider] api_key_file` and `[search] api_key_file` read a key from a file instead of the environment. On macOS, a sandboxed command can read the environment of the user's processes, keys included. It cannot read files under `$HOME`. timu refuses a key file that other users can read.

- timu no longer reads `./timu.toml`. Config comes only from the user file, `--config` and the environment. The file could still set models, `extra_body` and `[roles]`. On OpenRouter the model id picks the vendor that receives your code, and `extra_body` can change provider routing, so a cloned repo could choose where your code went. The file was also read from the current directory, not from `-C`. The coder could write `timu.toml` in a subdirectory, and a later run started there would load it.

- The Linux sandbox hides `/run`, `/tmp` and `/var/tmp` behind empty tmpfs mounts. A new network namespace does not isolate Unix sockets bound to a path, and a read-only mount does not stop `connect()`. So a command could reach the session D-Bus, ssh-agent, gpg-agent or `docker.sock`, and through them run code outside the sandbox. With network on, the sandbox keeps `/etc/resolv.conf` when it points into `/run`.

- With `--approve-untrusted`, a task that an agent writes for a role with `net` now needs approval: its goal and every input. The lead reads the workspace, gitignored files included, and the researcher can put text into a URL. So a lead steered by injected text could send workspace secrets out. The `lead` workflow now prompts before each researcher task.

- A graph node with `network = true` fails to load if a node upstream of it runs a model, unless it sets `trust_upstream = true`. A coder's changes reach later nodes through a chained branch or a file output, so their commands could run coder-written code with network access. `check-fix-review` now turns network on for its first `check` run only; re-checks after the coder run offline.

- A user skill now wins over a workspace skill of the same name in `./.timu/skills`. Before, the repo's copy replaced it, so a cloned repo could change the instructions of a skill the user's config named. A repo can still add skills with new names.

- The macOS sandbox denies LaunchServices opens, Apple events, and signals to processes outside the sandbox. A command could build an `.app` in the workspace and start it with `open`. launchd ran it unsandboxed, with full file and network access. A command could also kill timu. The profile still starts from `(allow default)`. A command can still read the environment of the user's other processes through `sysctl(KERN_PROCARGS2)`, API keys included. No SBPL rule tested on macOS 26 blocks this.

### Added

- Each run prints its config sources on one line (`config: <file> + TIMU_MODEL`) and records them in the trace.

- `timu graph run FILE`: a graph of workflows across local projects, found under `[projects] roots`. Each node runs on a `git clone --local` copy, where timu commits its changes on a branch for you to fetch. Agents cannot commit: a node whose `HEAD` moved during its workflow fails. Several nodes may change one project in sequence, each starting from the last one's branch. Declared outputs pass to downstream nodes as schema-checked `params` or as `inputs`. A failed node skips its descendants only. Nodes can have their own budgets, and the graph writes one trace, with each agent tagged by node. This is phases 2 and 3 of `docs/dev/graph.md`.

- Graph workflows `commands` and `check-fix-review`, and `file:GLOB` outputs (graph.md 7.7). They run fixed commands in the sandbox, with network only if the node asks for it. No model runs them, so no agent ever holds a shell and network together. `${node.output}` in a command takes only a file path or a version-like token, shell-quoted: a version string such as `1; rm -rf ~` fails the node. Output from a step with network on is marked untrusted.

- Linux sandbox backend with `bwrap`. It denies network access, hides `$HOME` behind an empty tmpfs, and mounts the filesystem read-only except the role's write roots. It keeps `.git`, `timu.toml` and `.timu` at each write root read-only. timu probes `bwrap` at startup and, where it cannot run, says why. Unlike the macOS profile, it cannot block a path that does not exist yet. So a command may create a new `.git`, `timu.toml` or `.timu`, but may not change existing ones.

- `review-validate-fix` workflow: a reviewer reports findings as JSON, a validator confirms or rejects each, the coder fixes the confirmed ones most severe first, and a verifier checks each fix. Each step is its own role, so `[roles.validator]` and `[roles.verifier]` can set a model. An agent whose JSON block is missing or malformed is told what is wrong and gets one more try; a second bad block fails the run.

  ```sh
  timu run --workflow review-validate-fix --verify-to-fix 1 "review src/ for correctness"
  ```

- CI (`.github/workflows/ci.yml`) runs the tests on Ubuntu and macOS, on CPython 3.11-3.14 and PyPy 3.11. On Ubuntu, it lifts the user-namespace restriction so the bwrap tests run instead of skipping.

### Changed

- No workflow step repeats unless asked. A loop is a back-edge with its own limit, default 0: `review_to_fix` (`--review-to-fix`) for fix-review, and `check_to_fix` for check-fix-review. They replace `max_rounds` and `--max-rounds`, which defaulted to 3. check-fix-review passed its `max_rounds` to the inner fix-review too, so the default allowed up to 9 coder runs.

- In `lead`, delegating to the coder also runs the reviewer on its work, and the lead gets both results. The result is done only on `VERDICT: APPROVE`. Before, only the lead's prompt asked it to request a review, so a lead could finish with unreviewed changes. Every workflow now checks every fix in code. With `--approve-untrusted`, a lead that has read web content now gets one more prompt per coder task, for the review goal.

- An agent whose final answer is empty, or fails its task's check, is told why and gets one more try. A second unusable answer fails it. `review-validate-fix` checks each JSON block this way. In live runs on a local 9B model, the lead and the validator each ended once with an empty reply, which passed as done.

- `timu run` passes a workflow only the flags it declares. Setting one it does not declare, such as `--report` with `lead`, is a usage error.

- `timu.__version__` comes from the installed package metadata, so `pyproject.toml` is the one place that sets it. `make release` only bumps that version; it no longer runs `git add` or `git commit`.

- Workflows are registered in `WORKFLOWS` (`workflow.py`). The CLI takes its `--workflow` choices, provider checks and approval default from there, not from three hardcoded lists.

- A `Session` holds what the agents of one invocation share: budget, run id, sink, cancel flag and approvals. `Run` binds it to one workdir, and `Run.at(path)` gives another `Run` in the same session. This is phase 1 of `docs/dev/graph.md`.

### Fixed

- `timu.toml` rejects unknown tables, keys and role names, and a `timeout` that is not positive. Before, a typo such as `[roles.reviewr]` was ignored, and that role ran on the default model.

- `--max-cost` rejects `nan`, `inf`, zero and negative amounts as usage errors. `nan` passed every budget check, so the run had no spending limit.

- A graph node named `files` no longer collides with the directory for file outputs. That directory is now `_files`, which no node id can name.

- An agent stops a turn's remaining tool calls once the workflow's tool-call limit is spent. Before, it checked only its own count, so a lead whose child spent the limit kept calling tools.

- A verdict line with Markdown inside it, such as `**VERDICT:** APPROVE`, is accepted. Before, the run failed with "no VERDICT line".

- A `file:` output is copied to its path in the workspace, under the node's files directory. Before, it was copied by base name, so two outputs named `x.txt` overwrote each other and both returned the last file.

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
