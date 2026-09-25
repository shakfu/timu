# E1: reviewer report mode

Status: planned. Runs after plan phase 6.

## Question

Does letting the reviewer write its own report (mode A) give better reviews than returning the report for the workflow to write (mode B)? Design 4.3 defines both modes.

Mode B is the default because the reviewer then has no `fs.write`. Mode A's one advantage is that the reviewer can revise the report during a run. E1 checks whether that advantage shows up in review quality. It does not compare security: mode B is safer by construction.

## Hypothesis

- H1: mode A finds more seeded defects than mode B.

- H0: no difference in defects found.

A secondary question is whether either mode delivers a missing or malformed report more often.

## Setup

Fixtures in `evals/e1/fixtures/`:

| Set | Count | Size | Content |
|-|-|-|-|
| Small | 6 | 200-800 LOC | 3 seeded defects each |
| Large | 2 | 3-5k LOC | 4 seeded defects each; the budget forces a multi-step review |
| Clean | 2 | 500-1k LOC | no seeded defects; measures false positives |

The large set is there because revising mid-run should matter most in long reviews.

Defect categories, spread across fixtures:

- logic error

- off-by-one

- missing error handling

- unsafe input handling

- test that passes without checking the behaviour

Each defect is listed in `defects.toml` by id, file, line range and category. The reviewer never sees this file.

Controls:

- Same model, provider settings, budget, tools and fixture checkout in both modes.

- The prompts share one body and differ only in the final instruction.

- Mode A and mode B runs are interleaved in random order, so provider drift over time affects both modes.

Matrix:

- 10 fixtures x 2 modes x 5 repeats x 2 models = 200 reviewer runs.

- The models are chosen at run time. One should be a strong model and one a cheaper one (design 15.5).

Cost: estimate cost per run from the phase 6 traces. Set a hard `cost_usd` cap on the whole experiment before launching. If the cap is reached, analyse the completed pairs only, and record that the run was cut short.

## Metrics

Primary: recall, meaning seeded defects found divided by seeded defects, per run.

A finding matches a defect if it names the defect's file, and either a line within 3 lines of the range or the function that contains it. Matching is automatic. Check 20% of runs by hand. If manual and automatic matching disagree on more than 10% of the checked findings, fix the matcher and re-score.

Secondary:

| Metric | Why |
|-|-|
| Findings on clean fixtures | false-positive rate |
| Report missing or empty | how reliably each mode delivers a report |
| Tokens, cost, turns, wall time | what the revise ability costs |
| Mode A: number of `write` calls to the report | whether the reviewer actually revises |
| Mode A: refused writes outside the report | the model tried to write beyond its root |

## Analysis

- Pair the runs by (fixture, model, repeat).

- Take the recall difference A - B for each pair.

- Compute a 95% bootstrap confidence interval, resampling fixtures rather than runs, because runs on the same fixture are correlated.

- Report the result per model and pooled.

## Decision rule (fixed before any runs)

Switch the default to mode A only if all of these hold:

1. The pooled recall difference is at least 5 percentage points, and its 95% CI lower bound is above 0.

2. The same direction holds for both models.

3. Mode A's missing-report rate is no higher than mode B's.

4. Mode A's median cost per run is at most 20% higher.

Otherwise, keep mode B. If the mode A reviewer rarely revises its report (median of at most one `write` call), revising does not explain any recall gain. In that case, record this and keep mode B.

## Threats to validity

- Seeded defects are simpler than real ones. The large fixtures reduce this but do not remove it.

- The different final instruction can change behaviour by itself, not only through the ability to revise. The shared prompt body limits this.

- Results hold only for the models tested.

- 5 repeats per cell may be too few for small effects. If the CI spans 0 but the point estimate is above 5 points, add repeats up to the cost cap. Do not change the rule.

## Outputs

- `evals/e1/results/<date>.jsonl`: one line per run, with the metrics above and the trace id.

- A results section appended to this file: a summary table, the decision, and the date.
