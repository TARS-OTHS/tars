# Skills

Skills are YAML-defined prompt templates that become Discord slash commands automatically. Drop a `.yaml` file in this directory and it's live on next restart (or instantly via hot reload).

## Format

```yaml
name: my_skill
description: Short description shown in Discord slash command list
parameters:
  - name: topic
    type: string
    required: true
    description: What to work on
  - name: depth
    type: string
    choices: [brief, detailed, comprehensive]
    required: false
    description: Level of detail
prompt: |
  Analyse {topic} at a {depth} level.
  Search memory for relevant context first.
tools:
  - web_search
  - memory_search
```

## Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Skill identifier (lowercase, underscores). Becomes the slash command name. |
| `description` | Yes | One-line description shown in Discord's command picker. |
| `parameters` | No | List of parameters. Each has `name`, `type`, `description`, and optional `required` (default false) and `choices`. |
| `prompt` | Yes | The prompt template. Use `{param_name}` for parameter substitution. |
| `tools` | No | List of MCP tools the skill needs. If omitted, the agent's full tool list is available. |

## Parameter Types

- `string` — free text input
- `string` with `choices` — dropdown selection in Discord

## How It Works

1. On startup, `src/core/skills.py` scans `skills/*.yaml` and `agents/*/skills/*.yaml`
2. Each skill registers as a Discord slash command with its parameters
3. When invoked, `{param_name}` placeholders are replaced with user input
4. The expanded prompt is sent to the agent's LLM with the specified tools available

## Agent-Specific Skills

Place skills in `agents/<name>/skills/` to scope them to a specific agent. They're prefixed automatically (e.g., `tars:my_skill`) to avoid name collisions.

## Creating Skills via Chat

Agents can create skills conversationally using the `create_skill` tool:

```
User: "Create a skill that analyses competitor pricing"
Agent: [calls create_skill] → writes YAML to skills/ → available as /competitor_pricing
```

## Examples

See `example.yaml` and `code_review.yaml` in this directory.
