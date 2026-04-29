# agent-management

Manage and create custom sub-agents.

## When to use

Use when the user wants to create a specialized agent, modify an existing agent definition, or understand how to delegate work to sub-agents.

## Agent definitions

Agents are defined as `.md` files in two locations:

- **User-level** (global): `~/.openharness/agents/`
- **Project-level** (overrides user-level): `<project-root>/.openharness/agents/`

Files are discovered automatically — no restart needed. Project-level definitions override user-level definitions with the same name.

## Agent file format

Each `.md` file has YAML frontmatter for configuration and a markdown body for the system prompt:

```markdown
---
name: review
description: Code review agent — read-only, can explore codebase
disallowedTools: file_edit, file_write, notebook_edit, bash
allowedSubagents: Explore
model: inherit
---

You are a code review specialist. Analyze the provided code for bugs,
security issues, and maintainability. Report findings concisely.
```

### Frontmatter fields

| Field | YAML key | Type | Description |
|-------|----------|------|-------------|
| name | `name` | string | **Required.** Agent type identifier |
| description | `description` | string | **Required.** When to use this agent |
| tools | `tools` | list or comma-string | Allowed tool whitelist. Omit or `null` = all tools |
| disallowed tools | `disallowedTools` or `disallowed_tools` | list or comma-string | Denied tool blacklist |
| allowed subagents | `allowedSubagents` or `allowed_subagents` | list or comma-string | Subagent types this agent may spawn. Omit = all |
| model | `model` | string | Model override. `"inherit"` = use parent's model |
| effort | `effort` | string or int | Reasoning effort: `"low"`, `"medium"`, `"high"` |
| permission mode | `permissionMode` or `permission_mode` | string | `"dontAsk"`, `"acceptEdits"`, etc. |
| max turns | `maxTurns` or `max_turns` | int | Max agent loop iterations |
| skills | `skills` | list or comma-string | Skills to load for this agent |
| color | `color` | string | UI color indicator |
| background | `background` | bool | Run as background task |
| omit CLAUDE.md | `omitClaudeMd` or `omit_claude_md` | bool | Skip CLAUDE.md injection |

### Available tool names

Use these in `tools` / `disallowedTools`:

`file_read`, `file_write`, `file_edit`, `notebook_edit`, `glob`, `grep`, `lsp`, `bash`, `web_fetch`, `web_search`, `agent`, `task_create`, `task_get`, `task_list`, `task_output`, `task_stop`, `task_update`, `send_message`, `team_create`, `team_delete`, `enter_worktree`, `exit_worktree`, `enter_plan_mode`, `exit_plan_mode`, `cron_create`, `cron_delete`, `cron_list`, `cron_toggle`, `remote_trigger`, `mcp_auth`, `list_mcp_resources`, `read_mcp_resource`, `ask_user_question`, `config`, `skill`, `tool_search`, `brief`, `sleep`, `todo_write`, `memory_write`

### Built-in subagent types

These can be referenced in `allowedSubagents`:

- `Explore` — read-only codebase search
- `Plan` — architecture and planning
- `worker` — full implementation capability
- `verification` — build/test verification

## Spawning an agent

Use the `agent` tool with `subagent_type` matching the agent definition name:

```
agent(
  subagent_type="review",
  prompt="Review the changes in src/auth/login.py for security issues",
  description="Security review of auth module"
)
```

The `description` field is shown to the user. The `prompt` field is the full task instruction sent to the sub-agent.

## Creating a custom agent

When the user asks to create a specialized agent:

1. Clarify the agent's purpose, tool restrictions, and subagent access
2. Write the `.md` file to the appropriate location using `file_write`
   - For project-specific agents: `<project-root>/.openharness/agents/<name>.md`
   - For global agents: `~/.openharness/agents/<name>.md`
3. The agent is immediately available — call it with `agent(subagent_type="<name>", ...)`

## Common patterns

**Read-only review agent:**
```markdown
---
name: review
description: Code review agent — read-only
disallowedTools: file_edit, file_write, notebook_edit, bash
allowedSubagents: Explore
---
You are a code review specialist...
```

**Implementation worker with limited scope:**
```markdown
---
name: refactor-worker
description: Refactoring agent — can edit but not spawn sub-agents
disallowedTools: agent
---
You are a refactoring specialist...
```

**Research agent with web access:**
```markdown
---
name: researcher
description: Web research agent
tools: web_search, web_fetch, file_read, glob, grep
allowedSubagents: Explore
---
You are a research agent. Search the web and local codebase...
```

## Rules

- Always confirm the agent definition with the user before writing the file
- Include `disallowedTools` for any agent that should not modify files — never rely on system prompt instructions alone for tool restrictions
- When `disallowedTools` includes `agent`, the sub-agent cannot spawn further sub-agents
- Use `allowedSubagents` to grant selective access instead of the blanket `agent` deny when the sub-agent needs limited delegation
- Prefer project-level definitions (`.openharness/agents/`) for project-specific workflows
- The markdown body after frontmatter becomes the agent's system prompt — write it with the same care as any system prompt
