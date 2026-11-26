# Proposed workflows

Status: 2026-11-26. Built: 1, 2 at depth 0, 4 in its sequential form, the `check` on `review-validate-fix`, and stopping signals 1, 2 and 4 in `fix_review`. Not built: 3, 2.6 depth, signals 3 and 5. Section 8 lists where the build differs from this draft.

Four proposals, then where loops and depth pay off, then when to stop iterating.

| # | Proposal | Adds | Depends on |
|-|-|-|-|
| 1 | `reproduce-fix-review` | a tester role; a fail-then-pass check | `fix_review(check=, failed_check=)` |
| 2 | Plan as a graph | `timu graph plan`; planned-graph rules | `parse_graph`, `run_graph`, `--resume` |
| 3 | `plan-fix-review` | nothing beyond 2 | 2 |
| 4 | `best-of-n` | parallel copies; a selector step | provisioning from `graph.py` |

Rules every proposal keeps, from earlier decisions:

- Each loop is an edge with its own `*_to_*` int param, default 0. No loop on a node.
- Every coder run is followed by a check or a reviewer, in code. Only the loop back is optional.
- A workflow's name lists its main steps in order.
- The worst-case number of coder runs is known before the run starts.

## 1. `reproduce-fix-review`

A tester writes a test that fails on the base tree. Then `fix_review` runs with that test as its check. This gives each task a fail-then-pass check that the engine runs, as SWE-bench grades fixes (<https://arxiv.org/abs/2310.06770>).

### 1.1 Steps

1. **Baseline.** The engine runs `check` on the base tree. It must pass. A suite that already fails gives no fail-then-pass signal, so the run fails.
2. **Reproduce.** The tester adds test files under `test_paths`.
3. **Confirm.** The engine runs `check` again. It must now fail.
   - If it passes, the test does not reproduce the problem. The log goes back to the tester at most `check_to_reproduce` times. Otherwise the run fails.
4. **Fix.** `fix_review(check=check, check_paths=<the new files>, failed_check=<step 3 log>)`. The coder cannot change the new tests.
5. **Review.** The reviewer gets the new tests, the step 3 log and the passing log. It judges whether the tests express the objective.

### 1.2 Roles

`tester` is `CODER` with `write_roots = test_paths`. It has `exec`, so it can run its own test. It has no `net`.

After step 2, the engine checks that the tester only added files. An edit to an existing test could weaken it. A model may not be the judge of that.

Alternative: the coder writes the test too. That is one role fewer, but the same agent then writes the check it must pass.

### 1.3 Params

| Param | Default | Meaning |
|-|-|-|
| `check` | required | suite commands, for example `["make test"]` |
| `test_paths` | `["tests/*"]` | where the tester may add files |
| `check_to_reproduce` | 0 | step 3 failures sent back to the tester |
| `check_to_fix`, `review_to_fix` | 0 | as in `fix-review` |

Worst-case coder-type runs: `(1 + check_to_reproduce) + (1 + check_to_fix + review_to_fix)`.

### 1.4 Limits

- Step 3 cannot tell a failing assertion from a test that does not import. The reviewer reads the step 3 log. A pytest-only check of the failure kind would be exact, but it would be pytest-only.
- It does not fit refactors, where no behaviour should change. Use `fix-review` with `check`.
- The tester and the coder may share a misreading of the objective. Only the reviewer guards against that. Running the tester on a different provider (`[roles.tester] provider`) reduces this.

### 1.5 Sim test

The trap tasks already hold a tempting wrong fix in `trap/`. Apply it after step 2. The tester's test should fail on it. Score per task: does the test reject `trap/`, and does it accept `solution/`? Neither score needs a coder run.

## 2. Plan as a graph

A planner writes a graph file. The engine checks the file and prints it with its worst-case cost. You read it, edit it if needed, and run it. The structure is chosen at run time but checked like a static graph.

### 2.1 Commands

```sh
timu graph plan --repos app,lib -o plan.toml "Drop Python 3.10 from lib and app"
timu graph run --dry-run plan.toml    # nodes, budget, worst-case coder runs
timu graph run plan.toml
```

Two commands over an in-run approval prompt:

- the file is the approval step, and you can edit it;
- `--resume` already works on files;
- no new gate code is needed.

The cost is one more command to type.

### 2.2 The planner

- **Role:** `planner`, with `fs.read` on each repo in `--repos`. No `exec`, no `net`, no `fs.write`.
- **Output:** the graph as a fenced `toml` block, or a `submit_graph` tool call once H3 lands. The engine writes the file, not the planner.
- **Retries:** a graph that fails `parse_graph` goes back to the planner with the error, at most `parse_to_plan` times (default 1). The error is engine text, so this loop costs one model call per retry.

### 2.3 Rules for a planned graph

The file starts with a `[plan]` table: `by = "planner"`, the objective, the repo list, and whether the planner read untrusted content. `run_graph` applies these rules while that table is present:

1. **Only workflows that run a model.** Allowed: `fix-review`, `reproduce-fix-review`, `review-validate-fix`, `check-fix-review`.
   - No `commands` nodes.
   - No `network = true`.
   - No `trust_upstream`.
   - Reason: graph.md 7.6 trusts command steps because a model cannot change them. A planned file is model output.
2. **No planning nodes.** A planned graph cannot contain a planning node. That rule sets recursion depth to 0 (2.6).
3. **Only named repos.** `repo` must be one of `--repos`. Otherwise a planner could name any project under the roots, or an `org/name` that the engine would clone.
4. **One chain per repo.** Nodes on one repo must form a chain. Two same-repo nodes that do not need each other would give two branches, and no merge exists (graph.md 7.3).
5. **An appended final check.** On each repo, the engine appends a `check-fix-review` node after the tip of the chain, with the user's `--check`. It checks the combined change, which no single node saw.
6. **A ceiling on coder runs.** The worst-case coder-run count must not exceed `--max-coder-runs`.

**Objectives:** planner-written objectives are model text in `objective`. graph.md 4.1 forbids pasting agent output there for untrusted text. A planner that read only the named repos is as trusted as the `lead` writing a goal. A planner that read untrusted input marks every node untrusted. The approval gate then applies to each node's objective.

**User edits:** deleting the `[plan]` table makes the file the user's. The rules above then stop applying. That is deliberate: the user has taken over the file.

### 2.4 Worst-case count

```
runs(node) = 1 + check_to_fix + review_to_fix       # fix-review, check-fix-review
           = (1 + check_to_reproduce) + runs(fix)   # reproduce-fix-review
           = 1 + verify_to_fix                      # review-validate-fix
total      = (sum over nodes, including the final checks) * (1 + fail_to_plan)
```

`--dry-run` prints `total` next to the budget. The budget still caps spend. The count lets you reject a plan before it spends anything.

### 2.5 Replanning

If a node fails, the planner can get the graph result and write a new file for the work left, at most `fail_to_plan` times (default 0). `--resume` reuses every node whose spec is unchanged, so finished nodes do not run again. Each replan costs up to the full ceiling again, which the `(1 + fail_to_plan)` factor in 2.4 counts.

### 2.6 Recursion

Rule 2 sets depth to 0. A node cannot plan, so no planned tree can grow below the first level.

If evidence later shows depth helps, allow it through a `depth` param:
- A node may be a planning node while depth stays below the limit. The limit reuses `Session.max_depth`.
- Leaf count is at most `max_nodes ** (depth + 1)`. With 5 nodes and depth 1 that is 25 leaves, before the loop edges. The ceiling in 2.4 must hold for the expanded tree.
- Each level gets its own final check node (rule 5). Approvals at a lower level do not make the whole correct.

Do not build depth before 3 shows that one level is short. No task in the sim needs more than one node.

### 2.7 Open questions

1. Should a planned graph let `ref` differ from `HEAD`? Default: no; the planner cannot pick a base.
2. Should the planner see the existing graph-file docs in its prompt, or a short schema? A short schema costs fewer tokens and drifts from the parser. A test could compare them.
3. Per-node budgets: should the planner set them, or the engine split `[budget]` evenly? An even split needs no model judgment.
4. Defaults for `--max-coder-runs` and `--max-nodes` (2.8). Both are placeholders; the sim has no multi-node tasks to set them from.
5. An oversized plan: reject it and send the count back to the planner, or trim it? Proposed: reject. Trimming means the engine picks which work to drop.
6. Should `--input` exist? Without it every plan is trusted and the per-node gate never applies. Issue text is the likely input, and it is usually untrusted.

### 2.8 API

`timu graph plan` writes a file and runs no node. It never runs code and never writes to a repo.

#### Steps

1. **Resolve repos.** Each `--repos` name resolves under `[projects] roots` with `find_project`. A missing or ambiguous name exits 2.
2. **Plan.** One `planner` agent runs (2.2). Its goal is the objective plus a short graph schema. It may read only the resolved repos.
3. **Check.** The engine:
   1. parses the block with `parse_graph`;
   2. applies the rules in 2.3;
   3. appends the final checks (rule 5);
   4. computes the worst case (2.4).

   An error goes back to the planner, at most `--parse-to-plan` times.
4. **Write.** The engine writes the file with its `[plan]` table to `-o`.

#### CLI

```sh
timu graph plan --repos NAME,... --check CMD [--check CMD]... [-o FILE] [--force]
                [--max-coder-runs N] [--max-nodes N] [--parse-to-plan N]
                [--input FILE]... [--config, --max-cost, -v] OBJECTIVE
```

| Flag | Default | Meaning |
|-|-|-|
| `--repos` | required | projects the plan may touch and the planner may read |
| `--check CMD` | required | the appended final check per repo; also the default `check` for nodes that take one |
| `-o`, `--out` | `plan.toml` | output file. An existing file is an error without `--force`. |
| `--max-coder-runs` | 20 | a worst case above this rejects the plan |
| `--max-nodes` | 8 | planner-written nodes, not counting appended checks |
| `--parse-to-plan` | 1 | retries after a parse or rule error |
| `--input FILE` | none | extra planner context, such as issue text. It sets `untrusted = true`. |

**Exit codes** follow `EXIT` in `cli.py`:

- 0: the file was written.
- 1: the planner failed or ran out of budget, or the plan is still invalid after retries. The last error and the rejected TOML are printed.
- 2: usage error.

On success the command prints the path, the node count and the worst case, then the follow-up commands:

```
wrote plan.toml: 4 nodes + 2 checks, worst case 11 coder runs
  timu graph run --dry-run plan.toml
  timu graph run plan.toml
```

#### The `[plan]` table

```toml
[plan]
by = "planner"
objective = "Drop Python 3.10 from lib and app"
repos = ["lib", "app"]
check = ["make test"]
untrusted = false        # true if the planner read --input
run_id = "20261126-..."  # the planning run's trace
```

Appended checks are ordinary nodes, one per repo, that need the tip of that repo's chain:

```toml
[nodes.lib-final]
repo = "lib"
needs = ["lib-ci"]
workflow = "check-fix-review"
objective = "The combined change passes the checks"
params = { check = ["make test"] }
```

`timu graph run` applies 2.3 whenever `[plan]` is present.

If `untrusted = true`, each node's objective needs approval before the node's first agent starts. Removing the table makes the file a plain user graph.

#### Python

`timu.api` gets `plan_graph`, alongside `run` and `run_graph`:

```python
def plan_graph(
    objective: str,
    repos: Sequence[str],
    *,
    check: Sequence[str],
    out: Path | None = None,          # None: return the text, write nothing
    inputs: Sequence[Artifact] = (),  # untrusted extra context
    max_coder_runs: int = 20,
    max_nodes: int = 8,
    parse_to_plan: int = 1,
    config: Config | None = None,
    sink: EventSink | None = None,
    max_cost: float | None = None,
    cancel: threading.Event | None = None,
    providers: Providers | None = None,
) -> PlanOutcome: ...


@dataclass(frozen=True)
class PlanOutcome:
    status: str              # done, failed, budget or cancelled
    summary: str
    usage: Usage
    run_id: str
    trace: Path
    graph: Graph | None      # parsed and checked; None unless done
    text: str | None         # the file contents, [plan] table included
    worst_case: int | None   # coder runs (2.4)
    errors: tuple[str, ...]  # parse or rule errors, one per failed attempt
```

`plan_graph` takes no `approve` callback. Nothing it does needs approval. A UI shows `text`, lets the user edit it, and passes the result to `run_graph`.

`run_graph --dry-run` also prints the worst case. It computes it with the same function, so the number has one source.

## 3. `plan-fix-review`

This is plan as a graph on one repo, in one command.

```sh
timu run --workflow plan-fix-review --check "make test" "Add retries to the client"
```

- The planner writes the graph. Rule 4 makes it one chain, and rule 5 appends the final check.
- The approval gate shows the graph before any node runs. This replaces reading the file.
- The nodes run in the run's copy, through `run_graph`.

**One mechanism over two:** a separate single-copy loop would repeat the work of the graph engine:
- the chain of branches,
- resume,
- budgets for each node,
- the final check.

The one real difference is the approval point. That is a flag on how the graph starts.

Build 3 only after 2, and only if typing two commands proves a real cost.

## 4. `best-of-n`

N coders work on separate copies of the same base. The check filters the results. A selector picks one. The chosen result then gets the usual review.

Repeated sampling raises the chance that some sample is right. Without a good verifier, picking that sample levels off. Brown et al. (<https://arxiv.org/abs/2407.21787>) report both effects. So `best-of-n` is only as good as its check and selector.

### 4.1 Steps

1. **Candidates.** The engine provisions N copies with `git clone --local`, as for graph nodes. Each runs one coder with the same goal. Each candidate may use a different provider (`providers` param), so the samples differ by more than temperature.
2. **Filter.** The engine runs `check` in each copy. A failing copy is dropped.
3. **Select.**
   - With 0 survivors, the run fails.
   - With 1, it is chosen.
   - With more, a selector picks one. The selector is a reviewer with the objective and each survivor's diff, and no coder summaries.
4. **Review.** The chosen copy goes through review, with `review_to_fix` back to the coder in that copy.

### 4.2 Params

| Param | Default | Meaning |
|-|-|-|
| `n` | 3 | candidates |
| `check` | required | the filter in step 2 |
| `stop_after` | `n` | stop starting candidates once this many pass the check |
| `providers` | `[]` | named providers, used in turn |
| `review_to_fix` | 0 | as in `fix-review` |

Worst-case coder runs: `n + review_to_fix`. Each candidate gets `budget / n`. A fixed split over a shared pool means one runaway candidate cannot starve the others.

### 4.3 Running candidates

- **In parallel:** wall time is about one candidate's. Cost is n times. It needs thread-safe `Session` charges, which the parallel graph scheduler already has.
- **In sequence with `stop_after = 1`:** this is retry-until-pass with fresh context. It stops at the first passing candidate, so it costs less when the first attempt usually passes.

Start with the sequential form. It needs no new concurrency, and it is the cheaper experiment.

### 4.4 Sim test

Compare these on the same tasks, at equal spend:
- `best-of-n` with `n = 3` and `stop_after = 1`;
- `fix-review` with `check_to_fix = 2`.

Both allow 3 coder runs. The difference is context: a fresh coder each time, against one coder that sees its own failed log. The pilot cannot answer this. 21 of 21 earlier trials solved on the first run.

## 5. Where loops and depth pay off

A loop edge is worth its default above 0 only if a later round often turns a failure into a pass. The sim can measure this per round: `round` events record each stage, and the oracle scores the final tree.

| Workflow | Edge or dimension | Next round has new information when | Expected value |
|-|-|-|-|
| `fix-review` | `check_to_fix` | the log names a new failure | high: deterministic feedback |
| `fix-review` | `review_to_fix` | the review names a specific defect | unknown; this is H1 |
| `review-validate-fix` | `verify_to_fix` | the verifier marks a finding unfixed | medium |
| `review-validate-fix` | `check_to_fix` (proposed) | as in `fix-review` | high |
| `reproduce-fix-review` | `check_to_reproduce` | the test passed on the base | medium: it fixes the cause of most tester failures |
| plan as a graph | `parse_to_plan` | the parser gave an error | high, and cheap |
| plan as a graph | `fail_to_plan` | a node failed for a reason a new plan can avoid | low until data shows otherwise |
| plan as a graph | `depth` | a node is too large for one coder run | unknown; no task in the sim needs it |
| `best-of-n` | `n`, `stop_after` | samples differ in outcome | depends on the check's strength |
| `lead` | delegation depth (2) | already capped | n/a |
| `research-fix-review` | none proposed | the coder needs facts it cannot find | low: research is read-only and runs once |

Edges with deterministic feedback (a check log, a parser error) give the coder something a model did not judge. They are the best candidates for a default above 0. Edges fed by an LLM verdict depend on the H1 answer.

## 6. When to stop iterating

A loop limit is a ceiling, not a target. The workflow should stop before the ceiling once more rounds cannot help. Five signals, cheapest first:

1. **No change.** The coder's run left the tree as it was: `tree_hash` before equals after. Another round with the same input will likely do the same. Stop with `failed`, summary `round N made no change`.
2. **Same failure.** The check log matches the previous failing log after removing timings and paths. The coder's change did not affect the failure. Stop after one repeat. Exact log equality is the simple form. A set of failing test ids would be exact, but it needs a parser for each tool.
3. **Same findings.** The reviewer reports the same findings as last round. With today's free text, compare the normalized report. With H3's structured findings, compare `(location, claim)` pairs. A repeat means the coder could not or would not address them.
4. **Not enough budget.** Do not start a round if the remaining budget is below the largest round so far. A round cut off by the budget spends money and leaves a half-edited tree. The engine already has the per-round usage.
5. **Good enough.** Accept when the check passes and no finding is at or above a severity threshold. This needs H3's severities. Today's verdict is APPROVE or CHANGES, so a CHANGES for a style nit costs a full round.

Rules for the stopping signal itself:

- **The model being judged does not decide.** A coder that reports "all tests pass" is not a signal. The check and the reviewer are.
- **Deterministic signals come before model ones.** Run 1 and 2 before spending a review. `fix_review` already runs the check before the review.
- **A pass is necessary, not sufficient.** In the pilot, a wrong `timu-check-files` fix passed `make test` twice (TODO.md). Only a stronger check catches that: a hidden oracle in the sim, or a fail-then-pass test in production (1).
- **Use an independent reviewer.** A reviewer on another provider than the coder is less likely to share its mistakes. That is inference; the H2 ablation design can test it with one more arm.

### 6.1 Setting the defaults

For each edge, the sim gives the solve rate at round k among trials unsolved at round k - 1, and the cost of round k. Raise an edge's default above 0 only when that rate, per dollar, beats starting a fresh candidate (4.3). Until the sim has about 30 trials per arm on tasks that trap (TODO.md), keep every default at 0.

## 7. Order of work

1. `check` on `review-validate-fix`. Small; same shape as `fix_review`.
2. Stopping signals 1, 2 and 4 in `fix_review`. Each is a few lines and needs no new data.
3. `reproduce-fix-review`, with the trap-task test in 1.5.
4. Plan as a graph (2), at depth 0.
5. `best-of-n`, sequential form, with the comparison in 4.4.
6. `plan-fix-review` (3) and `depth` (2.6), only if 4 shows a need.

## 8. Differences in the build

- **1.3 `test_paths`** are directories, default `["tests"]`, not globs. They become the tester's write roots, which take paths.
- **2.2 Retries** start a new planner with the rejected answer and the reason as an input. The agent's own one-retry check would bound retries at 1.
- **2.3 Node keys** in a planned graph exclude `ref`, `models` and `budget` as well as `trust_upstream`. `models` picks the vendor that receives the code. `ref` and `budget` follow open questions 1 and 3.
- **2.3 `check` params** written by the planner are allowed. They run offline in the sandbox, like the coder's own commands. Command steps stay excluded because a `commands` node may turn network on.
- **2.3 A `[plan]` table without `untrusted`** counts as untrusted.
- **2.8 Exit codes** follow `EXIT` in `cli.py`, so a planner out of budget exits 3. On failure the command prints each rejection reason; the rejected answers are in the trace. `api.plan_graph` takes `force`.
- **4.2 `models`** replaces `providers`. It takes model ids, as a graph node's `models` does, so it reuses the existing provider lookup.
- **4.2 Budget** is split n + 1 ways, not n, so the review keeps a share. `check_to_fix` is forwarded, so the worst case is `n + check_to_fix + review_to_fix`.
- **6 Signals** 1, 2 and 4 apply to `fix_review` only, and signal 1 only from round 2. Signals 3 and 5 need structured findings (H3).
