# TODO

## Critical

## High

- [ ] Measure what the reviewer adds. A partial sim run (77 of 150 trials, `openai/gpt-6-luna`, $0.28) left these gaps:
  - `check-blind` never ran, so the coder-summary ablation has no data.

  - The traps trapped 0 of 15 runs. Make them harder, or run a weaker coder model.

  - n = 5 per cell gives 95% CIs of 0.38-1.00. Separating the arms needs about 30 trials per arm on tasks that trap.

  - In 2 runs a wrong `timu-check-files` fix (`sh check.py`) passed the visible tests and the review. `make test` passed it too in the `check` arm.

- [ ] Run `reproduce-fix-review` with a real model on the trap tasks (workflow-proposals.md 1.5). Score whether the tester's test rejects `trap/` and accepts `solution/`. Neither score needs a coder run.

- [ ] Compare `best-of-n` (`n = 3`, `stop_after = 1`) with `fix-review` (`check_to_fix = 2`) at equal spend (workflow-proposals.md 4.4). Both allow 3 coder runs; the arms differ in fresh context against the coder seeing its own failed log.

- [ ] Live-test the `anthropic` and `openai` provider types (billed). Only recorded streams have run. Unverified against the real APIs: that the Responses API accepts the replayed output items, `id`s included, with `store = false`; that `include = ["reasoning.encrypted_content"]` is accepted on non-reasoning models; and that the thinking strip-and-retry fires on the real 400.

- [ ] Structured output: a `submit_review` tool with a JSON schema in place of the `verdict` regex and the `json_items` fenced-block parser. Stopping signals 3 (same findings) and 5 (no finding above a severity) wait on it (workflow-proposals.md 6).

## Medium

- [ ] Decide what the `tokens` budget limits. It sums the whole prompt on every turn, so a 30k-token prompt hits the 500k default at about 16 turns. It also counts cached input in full.

- [ ] A provider retry before any streamed text leaves no event in the trace. A trial that waited 600 s for a reply, then retried, showed only a slow agent. Emit a `warning` event, and consider a shorter time limit for the first byte.

- [ ] Split `workflow.py` and `graph.py`; one `Run` constructor. The git layer is out, in `timu/git.py`. `workflow.py` still holds `Session`, `Run`, every workflow and the JSON protocol.

- [ ] Decide whether a planned graph may carry planner-written `check` commands. They are allowed now and run offline in the sandbox; forbidding them leaves only the appended per-project check.

- [ ] Anthropic: consider the `thinking-binding-controls-2026-08-01` beta with `prefix_mismatch_behavior = "drop_block"` in place of stripping every thinking block and resending. It drops only the blocks after the trim, but it needs adaptive thinking on, which not every model accepts.

- [ ] Set the `timu graph plan` defaults from data: `--max-coder-runs` (20) and `--max-nodes` (8) are placeholders. The sim has no multi-node tasks to set them from.

- [ ] A flag to mark the workspace untrusted, for `review-validate-fix` on a third-party branch. Workspace content is `Origin.FS` and trusted, though the code under review may come from an attacker.

- [ ] Deny-default macOS sandbox profile in place of `(allow default)` plus denies.

## Low

- [ ] Landlock backend for Linux, where Ubuntu 24.04 and later block `bwrap`.

- [ ] Add an optional `Provider.abort()`, so that cancel stops the stream. The stream keeps running unbilled now, so `--max-cost` can be overrun.

- [ ] Warn at run start on macOS when the API key comes from the environment. Sandboxed commands can read it via `KERN_PROCARGS2`.

- [ ] `context_tokens` per provider table; trim at a fraction of it instead of the fixed 64k.

- [ ] `tree_hash` through a temporary git index and `git write-tree`, reusing git's stat cache.

- [ ] Mark `test_base_suite_passes` (89 s of the 130 s suite) `slow` and run it only in CI.

- [ ] CI: pin actions by commit SHA; pass `--locked` to `uv run`.

- [ ] Property-based tests (Hypothesis) for `reserved`, `_within`, `verdict`, `json_items` and `_Output`.

- [ ] Repeat detection catches only identical consecutive calls, not A-B-A-B loops.

- [ ] `Result.trace_id` is an agent id for agents and a run id for workflows. Two meanings in one field.

- [ ] `check_files` misses `bazel`, `gradle`, `cmake` and `ctest`. Alternative: restore the check files from the base commit before each re-check, instead of detecting the edit.

- [ ] Only if a planned graph shows a single level is short: `plan-fix-review` and a `depth` param (workflow-proposals.md 2.6, 3).

- [ ] `docs/dev/combo.md` asks for an Anthropic adapter outside core, to exercise the public provider API before a release. The built-in `anthropic` type now covers Anthropic, but nothing outside timu uses the entry-point API yet. Rewrite or close the note.
