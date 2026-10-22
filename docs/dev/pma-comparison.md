# timu and pma

Date: 2026-10-22. pma at `a224f25` (`~/projects/pma`).

Sources: pma's `README.md`, `docs/dev/async_dispatch.md`, `docs/dev/workflows.md` sections 1-5, and `crates/pma-core/src/{accept,report_runs,jobs}.rs`. The rest of pma's 42k lines was not read. Claims about it rest on its docs.

## Scope

- timu is an agent harness. It runs its own model loop, with roles, tools, a sandbox and a trust model.
- pma coordinates external coding agents (`claude`, `opencode`, `omp`) across many repositories. It never calls a model API itself.

## Comparison

| | timu | pma |
|-|-|-|
| Workflows | Python functions with checked params. Graphs are TOML: one workflow per node, with `needs`, outputs and `${node.output}` refs. | A JSON or Rhai document compiled to a typed graph of 5 primitives with declared effects. Revisions are proposed, then activated. A run (instance) is frozen at its revision and resumable (`workflows.md` W1-W16). |
| Routing | Rules: a command's exit status drives the `check-fix-review` loop. | Rules only; a model never chooses an edge (W7). |
| Async | None. `run_graph` runs ready nodes one at a time (`graph.py:604`). A crashed graph cannot resume. | `--detach` queues a job; an on-demand runner drains the queue. `pma watch` reads agent logs. `notify` runs a command when a job ends. Up to `max_parallel` agents run per pass (`pass.rs:1915`). One job at a time. |
| Cost control | `--max-cost` and per-node budgets, enforced while running. | A pass prints a plan with a cost ceiling. Nothing is spent until that plan's id is approved (W10). |
| Accepting a change | Model reviewer, validator and verifier. | Rules over recorded evidence (`accept.rs`): tests at base and head, file scope, edits to the check's own files, empty change, cost over budget. |
| Reporting | One JSONL trace per run; `timu trace` shows one. | `pma report --by project/class/agent`: first-pass rate, accepted, landed, attempts, cost, agent and review time. Scan history; HTML status page. |
| Isolation | OS sandbox on every command. No role holds network with exec or write. Untrusted content is tracked and gated. | Push credentials are stripped. Only the project's test command runs under Landlock or Seatbelt. Reads and network stay open. |

## Candidates for timu

By value per effort. All six are built; see CHANGELOG, Unreleased. Where the build departs from the text: candidate 2 reports uncapped nodes but does not refuse them, and candidate 3 is `--max-parallel` on the command line.

1. **Flag edits to the check's own files.** `accept.rs` `drivers()` maps a test command to the files that implement it: `make` to `Makefile` and `*.mk`, `pytest` to `conftest.py`, `pytest.ini`, `tox.ini` and `setup.cfg`. A run that edits one goes to a human. In timu's `check-fix-review`, a coder can make the check pass by editing `conftest.py`, and only a model reviewer might notice. A rule catches it without one. Also flag a run that changed nothing. Effort: low.

2. **`timu graph run --dry-run`.** Print the nodes in order with workflow, model and budget cap, and the summed ceiling. Refuse a node with no cap when `--max-cost` is unset. `Graph` already holds the data. Effort: low.

3. **Run independent graph nodes in parallel.** Nodes already run on separate clones. Run the ready set in a `concurrent.futures.ThreadPoolExecutor`, bounded by `max_parallel`. Needs:
   - thread-safe budget accounting;
   - one lock around the approval prompt on `/dev/tty`;
   - console lines tagged with the node id;
   - cancel reaching every worker.

   Effort: medium. It pays only for graphs with independent branches.

4. **Resume a graph from its trace.** `node_result` events record each node's status, branch and head. `timu graph run --resume <run id>` would skip nodes that ended `done`. This is pma's W9: derive progress from the record, store no cursor. Effort: medium.

5. **`timu report`.** Totals across the traces in `~/.local/state/timu/runs/`: runs, done rate, cost and wall time, by workflow, model or project. It answers whether a workflow is worth its cost. Effort: low.

6. **Model per graph node.** pma routes a model per node, such as haiku for review. timu graph nodes cannot name a model. Effort: low.

Not worth taking:
- `TODO.md` scanning, ranking and the portfolio views;
- GitHub issue sync;
- routing-policy revisions;
- the typed workflow language, which is the wrong scale for one-repository workflows;
- stage, commit and push. timu already prints the `git fetch` command.

## Alternative: pma drives timu

pma adds an agent as a worker record, not a code change. Jobs, detach, notify, `watch`, cross-run reports and plan approval are pma's layer. timu would need only a final result record on stdout (`--json`): status, cost, changed paths and trace path, beside its existing exit codes. pma would supply the queue and the reporting; timu would supply the sandbox and untrusted-content tracking that pma lacks.

The cost: timu graphs and pma workflows overlap, and one must own multi-repository work.

If timu stays interactive on one repository, candidates 1, 2 and 5 are enough.
