# Extensibility

Status: proposal. It answers design 15.4 ("Should roles live in Python, or in files that users can add without code?") and extends it to workflows and tools.

## 1. Current state

| Part | Where defined | What a user can change without editing timu |
|-|-|-|
| Roles | Python values in `src/timu/roles.py` | the model and skills of a built-in role, in `[roles.<name>]` |
| Workflows | functions in `src/timu/workflow.py` | nothing |
| Tools | module constants in `src/timu/tools/` | nothing |
| Skills | `SKILL.md` directories | anything: `./.timu/skills`, then `~/.config/timu/skills` |

`src/timu/cli.py` hardcodes each workflow in three places: the `--workflow` choices, the roles whose providers it checks before the run, and a dispatch with per-workflow arguments. It also sets the approval-gate default by name (`lead` on).

## 2. Constraints

1. Every role passes `role.validate`, whatever its source. The check already runs when an agent is built, so file-defined roles get it without new code.
2. A file can select capabilities, never add them. Capabilities come only from tools, and tools are code.
3. No runtime dependencies (design 12). Use `tomllib`, the frontmatter parser in `skills.py`, and `importlib.metadata` for plugins.
4. Trust follows whoever can write the file. Section 3 sets this out.

## 3. Trust by source

| Source | Who can write it | Roles | Workflows | Tools |
|-|-|-|-|-|
| timu | timu's maintainers | yes | yes | yes |
| Installed plugin package | whoever the user chose to install | yes | yes | yes |
| `~/.config/timu/` | the user | yes | as a delegating role (4.2) | no |
| Workspace: `./.timu/` | the repo's author, and the coder in any earlier run | no | no | no |

Plugin code runs in the timu process, outside the sandbox. Only `shell` commands run sandboxed. A plugin tool's `needs` is therefore a claim, not a limit. Installing a plugin trusts it as fully as installing timu.

### 3.1 Workspace `timu.toml`

Removed; see CHANGELOG, Unreleased. timu reads no workspace `timu.toml`: models, roles and request settings belong to the user or the workflow instance, not the repo. Agents still cannot write `timu.toml` or `.timu/` at the workspace root.

## 4. Roles

### 4.1 Roles as files

A role file is Markdown with frontmatter. It uses the same format as `SKILL.md`, and the same parser in `skills.py`. Claude Code subagents use the same shape (https://docs.claude.com/en/docs/claude-code/sub-agents).

```markdown
---
name: doc-writer
description: Writes and edits the Markdown docs.
tools: read list search write edit
write_roots: docs
skills: house-style
budget:
  turns: 30
  tool_calls: 60
---
You write documentation for this project. ...
```

The frontmatter maps to `Role` fields. The body is the prompt. The parser supports scalars and one level of mapping, but not lists. List values are space-separated, like `allowed-tools` in the skills spec.

- `tools` names entries in a tool catalogue: `read list search write edit shell web_fetch web_search delegate`. `web_search` is in the catalogue only when its key is set. `load_skill` and `read_skill_file` are added when `skills` is set, as `with_skills` does now.
- `grants` is the union of the tools' `needs`. The file does not declare it. A declared grant that no tool uses changes nothing, except that `fs.read` enables `read_roots`, and `read` needs `fs.read` anyway.
- `delegates` names other roles. `validate` already requires `delegate` and `delegates` together.
- `model` stays in `timu.toml` `[roles.<name>]`. The model is a deployment choice, the same for a role wherever it is defined.

Discovery: `~/.config/timu/roles/*.md`, then plugins. A workspace cannot define roles (7). A file's name must equal its `name`, as for skills.

Alternative: roles in `timu.toml`, as `[roles.x] prompt_file = "..."`. That takes two files per role and splits one definition across them. Rejected.

### 4.2 Resolving roles by name

The pipelines take roles by value (`CODER`, `REVIEWER`). A user role named `coder` would have no effect on `fix-review`. Two options:

- **Resolve by name (recommended).** `Run.roles` holds every known role, built-ins first. Pipelines look roles up by name, so a user `coder` with a house prompt changes `fix-review`. timu prints a notice when a file overrides a built-in. `validate` still applies. Provenance does not depend on the name: the untrusted mark comes from the `net` grant and from what the agent read.
- **Forbid shadowing.** Simpler, but then a user who wants a different coder prompt has to copy the workflow.

`REVIEWER_WRITE` is derived from `REVIEWER`. With name resolution it is derived from the resolved `reviewer`.

### 4.3 A delegating role is a workflow

`lead()` runs one agent whose role has `delegates`, with `Run.roles` filled in. With that step made generic, any delegating role is a workflow:

```sh
timu run --role triage "why does CI fail on 3.11?"
```

This gives no-code composition without a pipeline language. The model chooses the order, so it suits jobs where the order varies. The approval gate defaults to on for any role with `delegates`, as for `lead` now (design 7).

## 5. Workflows

### 5.1 One interface

```python
@dataclass(frozen=True)
class Workflow:
    name: str
    help: str
    roles: tuple[str, ...]      # providers checked before the run
    approve_untrusted: bool     # default for --approve-untrusted
    run: Callable[[Run, str, Options], Result]


@dataclass(frozen=True)
class Options:
    skills: Mapping[str, tuple[str, ...]]
    search: Tool | None
    params: Mapping[str, Any]   # keyword arguments for the workflow
```

Done; see CHANGELOG, Unreleased. `WORKFLOWS: dict[str, Workflow]` in `workflow.py` holds the built-ins. `cli.py` builds `--workflow` choices, the provider check and the gate default from it. `--max-rounds`, `--report` and `--report-mode` fill `params`. `Options` carries the role skills, not the whole `Config`: no workflow reads anything else from it. `-p key=value` is not built; graph nodes will set `params` from the graph file (`graph.md` 4.1).

### 5.2 Adding a workflow

- **Delegating role** (4.3). No code.
- **Plugin.** Entry point group `timu.workflows` (https://packaging.python.org/en/latest/specifications/entry-points/). The value is a `Workflow`. Install with `uv tool install timu --with my-flows`. Entry points are loaded only when `--workflow` names them, so a broken plugin fails only its own runs.
- **Local file**, optional: `--workflow ./flows/x.py:flow`. Useful while writing one. It runs code from the workspace, so it needs the same allow step as other workspace files.

### 5.3 Rejected: a declarative pipeline format

`fix-review` needs a loop, a verdict parse, and branches on each agent's status. A TOML or YAML format for that is a small programming language, with worse errors and no debugger. Fixed-order jobs are short in Python (see the README example). Variable-order jobs fit a delegating role. Revisit if users write many near-identical pipelines.

## 6. Tools

Entry point group `timu.tools`. The value is a `Tool`. Role files may name plugin tools. `validate` checks `needs` against grants as for built-ins. As section 3 notes, this does not confine the handler.

Alternative: expose MCP servers as tools (https://modelcontextprotocol.io/). Each server is a separate process, which a sandbox could confine. It needs an MCP client: either a runtime dependency, or a stdlib JSON-RPC client over stdio. Deferred.

## 7. Workspace files

A repo cannot choose models, roles or workflows. Those belong to the user, or to the workflow instance that runs on the repo (`graph.md` 3). timu reads no role or config files from the workspace, so there is no `timu allow` step.

Rejected: role files in `./.timu/roles/`, read after `timu allow` records their SHA-256 (the direnv model, https://direnv.net/). An approved file still lets the repo's author define an agent's prompt, tools and delegates. Each edit would also need a new approval.

`./.timu/skills` is still read. A workspace skill has no more power than other repo files: its text has the same trust as a README the agent reads, and its scripts run in the same sandbox as the repo's tests. A repo may add skills, but a user skill of the same name wins, and timu warns that the repo's copy was ignored.

## 8. Order of work

1. Add the `Workflow` registry and `Options` (5.1). No behaviour change.
2. Add role files from `~/.config/timu/roles`, name resolution (4.2), and `--role` (4.3).
3. Add entry points for workflows, tools and roles.

## 9. Open questions

1. Should a role file set its own budget, or should workflows own budgets?
2. Should `-p key=value` params be typed, with a schema per workflow, or left as strings for each workflow to parse?
3. Should `timu roles` and `timu workflows` list everything found, with its source? This is cheap, and it makes section 3 visible.
