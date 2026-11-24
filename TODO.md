# TODO

## Critical

## High

- [ ] Measure what the reviewer adds. A partial sim run (77 of 150 trials, `openai/gpt-6-luna`, $0.28) left these gaps:
  - `check-blind` never ran, so the coder-summary ablation has no data.

  - The traps trapped 0 of 15 runs. Make them harder, or run a weaker coder model.

  - n = 5 per cell gives 95% CIs of 0.38-1.00. Separating the arms needs about 30 trials per arm on tasks that trap.

  - In 2 runs a wrong `timu-check-files` fix (`sh check.py`) passed the visible tests and the review. `make test` passed it too in the `check` arm.

- [ ] Structured output: a `submit_review` tool with a JSON schema in place of the `verdict` regex and the `json_items` fenced-block parser.

## Medium

- [ ] Decide what the `tokens` budget limits. It sums the whole prompt on every turn, so a 30k-token prompt hits the 500k default at about 16 turns. It also counts cached input in full.

- [ ] A provider retry before any streamed text leaves no event in the trace. A trial that waited 600 s for a reply, then retried, showed only a slow agent. Emit a `warning` event, and consider a shorter time limit for the first byte.

- [ ] Split `workflow.py` and `graph.py`; one `Run` constructor.

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
