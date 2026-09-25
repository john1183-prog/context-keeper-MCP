# Context & Design Keeper (MCP Server)

Stateful project memory for AI coding tools (Cursor, Claude Desktop, Windsurf, Continue).

Keeps, per project, all in plain git-diffable JSON under `.ai_context/`:

| Store | File | Purpose |
|--------|------|---------|
| Session memory | `session_memory.json` | Current focus + rolling action history |
| Design blueprint | `design_blueprint.json` | Architecture rules + the project's design system (project-wide and per-section) |
| Decisions | `decisions.json` | Critical decisions, with rationale and alternatives considered |
| Progress | `progress.json` | Hierarchical task tracker — goals broken into subtasks, worked one at a time |

Plus a small **cross-project index** outside any one project
(`~/.context-keeper/projects.json` by default), so you can see every
project this has been used on.

Commit `.ai_context/` to the repo and context survives across machines and
sessions automatically — no external service required.

## Honest notes on what this does and doesn't do

- **Design system**: this server has no visual/creative judgment of its
  own — it's a data store. `infer_project_type_hints` only returns raw,
  read-only evidence (manifest files, folder names, README text, file-type
  counts); it never decides anything. The actual judgment about what a
  project should look like still comes from the AI model calling this
  server, via `set_design_system`. What this gets you is that judgment
  happening explicitly once (with an `anti_patterns` field forcing the
  model to name generic patterns to avoid) and then being served back
  consistently every session, instead of quietly drifting.
- **No enforcement**: nothing here checks generated code against the
  recorded design system or blocks a build that violates it. That would
  require a real static-analysis/lint layer, which wasn't built here — an
  AI client can still ignore what it reads.
- **PyPI**: not published. `.github/workflows/publish.yml` is ready to do
  it automatically on a version tag, once you add a `PYPI_API_TOKEN` repo
  secret — that step needs your PyPI account, not something done for you.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install

```bash
cd context-keeper-mcp
uv tool install .
# or: pip install -e .
```

Installs a `context-keeper-mcp` command on your PATH.

Once pushed to GitHub:

```bash
uv tool install git+https://github.com/YOUR_USER/context-keeper-mcp
```

## Test with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector context-keeper-mcp
```

## Cursor

**Settings → Features → MCP → Add New MCP Server**
- **Name:** `context-keeper`
- **Type:** `command`
- **Command:** `context-keeper-mcp`

## Claude Desktop

Edit:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "context-keeper": { "command": "context-keeper-mcp" }
  }
}
```

> If the IDE can't find it on PATH, use the absolute path from
> `which context-keeper-mcp` / `where context-keeper-mcp` instead.

## Tools & resource

| Kind | Name | Purpose |
|------|------|---------|
| Resource | `context://project_memory` | Full dump: focus, design system, architecture, decisions, task progress + next task, recent actions |
| Tool | `update_current_focus` | Switching files / tasks |
| Tool | `append_session_note` | Log a minor observation |
| Tool | `search_session_history` | Find a past note by keyword |
| Tool | `update_design_blueprint` | Lock architecture rules for a code component |
| Tool | `set_architecture_summary` | High-level system description |
| Tool | `get_component_rules` | Look up one component's architecture rules |
| Tool | `infer_project_type_hints` | Read-only scan of the repo for project-type evidence |
| Tool | `set_design_system` | Lock the project-wide visual/UX direction, once |
| Tool | `set_section_design_notes` | Override the design system for one section |
| Tool | `get_design_system` | Read the design system before generating any UI |
| Tool | `record_decision` | Log a critical decision with rationale and alternatives |
| Tool | `search_decisions` | Check a decision wasn't already made |
| Tool | `get_recent_decisions` | List recent decisions |
| Tool | `plan_tasks` | Submit a goal, split into subtasks up front, each with an optional priority |
| Tool | `break_down_task` | Split an existing task further once it turns out bigger, each with an optional priority |
| Tool | `get_next_task` | The single highest-priority actionable task to pick up now |
| Tool | `update_task_status` | todo / in_progress / blocked / done |
| Tool | `update_task_priority` | Re-prioritize a task (high / medium / low) after the fact |
| Tool | `list_tasks` | List tasks, with parent/child context and priority |
| Tool | `list_projects` | Every project this has been used on, most recent first |

## Example prompts

```
Call infer_project_type_hints, then set_design_system with a
project_type, visual_direction and anti_patterns based on what
you find — no generic SaaS-template look.
```

```
Call plan_tasks with goal "Build auth system", subtasks
["Design schema", "Implement login endpoint", "Add JWT middleware"],
and priorities ["high", "high", "medium"]. Then call get_next_task
and start on that one.
```

```
This is bigger than expected — call break_down_task on T4 with
["Add token refresh", "Add revocation list"].
```

```
Before touching auth, call search_decisions for "auth" so I don't
contradict something already decided.
```

## Concurrency

All writes to a project's own stores are guarded by one file lock
(`.ai_context/.lock`, 5s timeout). The cross-project index has its own
short-timeout lock and fails silently rather than ever blocking a real
tool call.

## Optional: fixed storage paths

```bash
export CONTEXT_KEEPER_DIR=/path/to/my-app/.ai_context   # per-project store
export CONTEXT_KEEPER_HOME=/path/to/central/index        # cross-project index
```

## Publishing to PyPI

```bash
git tag v0.3.0
git push --tags
```

...will run `.github/workflows/publish.yml` automatically, once a
`PYPI_API_TOKEN` secret is set on the GitHub repo.

## License

MIT
