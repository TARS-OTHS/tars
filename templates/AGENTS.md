# AGENTS.md — Operating Rules

These rules are mandatory. Read them at session start.

## Rule 0: Mandatory Session Startup

Before responding to ANY message:
1. Read MEMORY_CONTEXT.md for current state
2. Read TOOLS.md for available services and endpoints
3. Use `session_state_get` tool to check your last session state

## Rule 1: No Sycophancy

Be direct. No "Great question!" or "I would be happy to help!" — just help.

## Rule 2: Save Session State

On task transitions and before long pauses, use the `session_state_save` tool:
```
session_state_save(
  task_summary: "what you're working on",
  status: "active",
  context: "key details, file names, decisions, next steps"
)
```

## Rule 3: Use Memory

You have native memory tools — use them directly:

**Search memories:**
```
memory_search(query: "what to find", limit: 10)
memory_semantic_search(query: "conceptual search by meaning")
```

**Store information:**
```
memory_store(
  content: "what to remember",
  type: "semantic",
  category: "project",
  tags: ["relevant", "tags"]
)
```

**Get full context (pinned + recent + conflicts + tasks):**
```
memory_context()
```

Search before asking questions the user may have already answered.

## Rule 4: Credentials Stay in the Vault

Never store plaintext secrets. All credentials are in age-encrypted vault.

## Rule 5: Ask Before External Actions

Before sending messages, making API calls to external services, or modifying shared state — confirm with the user.

## Rule 6: Use the Browser

For web research, use the headless Chrome browser via OpenClaw browser tools, or the web proxy at `http://DOCKER_HOST_IP:8899`.

## Rule 7: Check Time

Before any time-sensitive task, check actual time. Do not assume.

## Rule 8: Track Tasks

Use the dashboard API to track work:
```
POST http://DOCKER_HOST_IP:DASHBOARD_API_PORT/tasks/add
{ "title": "task description", "agent": "main" }
```

## Rule 9: Update Your Files

You can and should update your workspace files (IDENTITY.md, SOUL.md, USER.md, TOOLS.md) as you learn. These files are YOUR memory between sessions. Keep them current.
