# T.A.R.S — Architecture & Operations Reference

> Last updated: 2026-04-11

## System Overview

T.A.R.S runs as a single async Python process connecting Discord bots to Claude Code CLI sessions, with tools accessible via MCP, persistent memory, encrypted vault, three-layer access control, and full security middleware.

You can run multiple instances from the same codebase using different `--profile` configs (e.g., a sandboxed production service and an unsandboxed dev/ops service). Instances share SQLite databases (WAL mode + busy_timeout for safe concurrent access) and the same Fernet vault.

---

## Architecture

```
Discord (one or more bot accounts)
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│              T.A.R.S Process                             │
│                                                          │
│  Discord Connector (multi-bot, typing, slash commands,   │
│    loop detection, dedup, reply fallback)                 │
│       │                                                  │
│       ▼                                                  │
│  Router (channel/guild/bot/category → agent mapping)     │
│       │                                                  │
│       ▼                                                  │
│  Access Control (three-layer)                            │
│   ├── Layer 1: Can they talk? (sender tier → agent tier) │
│   ├── Layer 2: What tools? (--disallowedTools per sender)│
│   └── Layer 3: Agent ceiling (static config per agent)   │
│       │                                                  │
│       ▼                                                  │
│  Agent Manager                                           │
│   ├── Context injection (channel, user, attachments)     │
│   ├── Auto-recall (memory search before each LLM call)   │
│   ├── Session management (SQLite, --resume)              │
│   └── Auto-summarize (long conversations)                │
│       │                                                  │
│       ▼                                                  │
│  Claude Code CLI (--print --output-format json)          │
│   ├── Reads CLAUDE.md from agent's project_dir           │
│   ├── Built-in tools: Read, Write, Bash, Glob, Grep,    │
│   │   WebSearch, WebFetch (blockable via disallow_builtins)│
│   └── MCP tools: custom tools via tars-tools server      │
│       │                                                  │
│       ▼                                                  │
│  MCP Server (FastMCP SDK, stdio transport)               │
│   ├── Rate limit check                                   │
│   ├── HITL gate (Discord reaction approval)              │
│   ├── Tool execution (@tool Python functions)            │
│   ├── Audit log (JSONL)                                  │
│   └── Vault access (Fernet encrypted credentials)        │
│                                                          │
│  Storage: SQLite (sessions, messages, tool logs)         │
│  Hot reload: file watcher on skills/ and src/tools/      │
└─────────────────────────────────────────────────────────┘
         │
         ▼
   External APIs (whatever you connect)
   • Google (OAuth2)
   • Discord API
   • Tavily, Groq, Gemini
   • Trello, Notion, Cloudflare
   • Your own integrations
```

## Three-Layer Architecture

T.A.R.S uses a layered directory structure to separate the engine, domain extensions, and deployment-specific config. This keeps the Core repo clean for updates and prevents deployment data from leaking into the public codebase.

```
Layer 1 (Core):    /opt/tars            ← Engine code, generic tools, scripts
Layer 2 (OTHS):    /opt/tars-oths       ← Domain-specific tools/skills (per-module subdirs)
Layer 3 (Overlay): /opt/tars-overlay    ← This deployment's config, agents, data
```

| Layer | Contains | Git workflow | Env var |
|-------|----------|-------------|---------|
| **Core** | `src/`, generic tools, skills, scripts, systemd templates | Branch + PR + cross-review | — |
| **Layer 2** | Domain tools/skills in per-module dirs (e.g. `crm/`, `analytics/`) | Self-merge OK | `TARS_OTHS` |
| **Overlay** | `config/`, `agents/`, `systemd/`, `data/`, agent identities | Direct push | `TARS_OVERLAY` |

**What goes where:**
- Engine code, generic tools → Core
- Domain tools (custom integrations, business logic) → Layer 2
- `config.yaml`, `agents.yaml`, `team.json`, agent CLAUDE.md files, service units → Overlay
- Personal names, Discord IDs, API keys, company data → **never in Core or Layer 2**

**Setup:** `setup.py` creates the overlay directory automatically (Step 2) and writes all generated config there. The `TARS_OVERLAY` env var is injected into systemd service units so the running process knows where to find config.

**Discovery:** Layer 2 modules are scanned at startup. Each subdirectory with a `tools/` or `skills/` folder is auto-discovered. `setup.py` (Step 4) lets you select which modules to enable and builds the `TARS_OTHS` path.

### Remote Neutralisation

When a non-maintainer clones Core and runs `setup.py`, the installer renames the git remote `origin` → `upstream` and blocks push on that remote. This prevents agents and users from accidentally pushing deployment data to the public Core repo.

```
origin  → upstream (fetch: github.com/TARS-OTHS/tars, push: blocked)
origin  → (unset — nothing to push to by default)
```

Users can still pull updates with `git pull upstream main`. Maintainers who need push access restore it with:

```bash
git remote rename upstream origin
git remote set-url --push origin <url>
```

---

## Key Architectural Decisions

1. **Claude Code CLI is a black box** — tools execute inside MCP subprocess, not in the main process. The `_dispatch_tools()` loop in agent_manager is dead code for the Claude Code provider.

2. **MCP server IS the middleware layer** — rate limiting, HITL, audit all happen in MCP tool handlers, not in agent_manager.

3. **All paths must be absolute** — Claude Code ignores `cwd` in `.mcp.json`. Every file reference in MCP tools uses `Path(__file__).resolve().parent.parent`.

4. **`TARS_PROFILE` env var** controls test vs production config throughout the chain (main.py → Claude Code → MCP server).

5. **HITL always uses the primary bot token** regardless of which agent or profile is active.

6. **One MCP server per Claude Code session** — each agent spawns its own MCP server subprocess. Vault is loaded fresh per MCP server.

---

## Directory Layout

```
tars/
├── src/
│   ├── core/
│   │   ├── agent_manager.py   — sessions, context injection, LLM dispatch
│   │   ├── access_control.py  — three-layer permission system (sender tier × agent tier)
│   │   ├── registry.py        — auto-discovery of all modules
│   │   ├── router.py          — message routing: connector/channel/category → agent
│   │   ├── tools.py           — @tool decorator, schema from type hints
│   │   ├── skills.py          — YAML skill loader
│   │   ├── storage.py         — SQLite (sessions, messages, tool logs)
│   │   ├── hitl.py            — HITL approval gates (connector-side, for main process)
│   │   ├── rate_limiter.py    — per-tool per-agent sliding window
│   │   ├── audit.py           — append-only JSONL audit log
│   │   ├── content_safety.py  — behavioral monitoring
│   │   ├── digest.py          — hot-reload file watcher
│   │   └── base.py            — interfaces, dataclasses
│   ├── connectors/
│   │   └── discord.py         — multi-bot, typing, slash commands, loop detection, dedup
│   ├── llm/
│   │   └── claude_code.py     — Claude Code CLI (Max subscription)
│   ├── tools/                 — @tool decorated functions (auto-discovered)
│   │   ├── memory.py          — store, search, semantic_search, forget
│   │   ├── team.py            — list, get, add, update, remove + user context
│   │   ├── web_search.py      — Tavily search
│   │   ├── google.py          — Gmail, Calendar, Drive (13 tools)
│   │   ├── trello.py          — boards, lists, cards, create, activity
│   │   ├── cloudflare.py      — zones, dns_list, dns_update
│   │   ├── notion.py          — search, read, create
│   │   ├── gemini.py          — analyze_video, analyze_image, generate_image
│   │   ├── audio.py           — transcribe_audio (Groq Whisper)
│   │   ├── discord_tools.py   — read_channel, read_message, search, send_file
│   │   ├── video.py           — video_frames, video_clip
│   │   ├── tmux.py            — list, send, read, new
│   │   ├── compress.py        — compress_context, decompress_context
│   │   ├── ingest.py          — create_skill, read_url, browse_url, install_mcp, list_capabilities
│   │   └── builtin.py         — send_message, ask_agent, send_to_agent
│   ├── lib/
│   │   └── compressor.py      — rule-based context compression (no LLM calls)
│   ├── vault/
│   │   └── fernet.py          — Fernet encrypted vault
│   ├── auth/
│   │   └── oauth2.py          — OAuth2 refresh (Google, etc.)
│   ├── mcp_server.py          — FastMCP server with middleware chain
│   └── main.py                — entry point, --profile support
├── agents/
│   └── main/                  — example agent template (CLAUDE.md.example)
├── config/
│   ├── config.yaml.example    — example production config
│   ├── agents.yaml.example    — example agent definitions
│   ├── team.json.example      — example team roster
│   ├── tars.service        — systemd unit template
│   ├── tars-rescue.service    — systemd unit template (unsandboxed)
│   └── timers/                — systemd timer+service files for scheduled tasks
├── scripts/
│   ├── test-tools.py          — e2e tests across tool categories
│   ├── health-audit.sh        — automated health checks
│   ├── monitor-container-health.sh  — Docker security baseline
│   ├── monitor-integrity.sh   — file integrity SHA256
│   ├── monitor-exposure.sh    — public port scanning
│   ├── regen-memory-context.sh — memory stats snapshot
│   ├── memory-decay.sh        — memory decay/archive/purge
│   ├── compress-context.sh    — batch context file compression
│   ├── install-timers.sh      — install all systemd timers (legacy)
│   ├── install-systemd.sh     — symlink units, daemon-reload, enable timers
│   ├── google-reauth.py       — Google OAuth2 re-authentication helper
│   └── lib-alert.sh           — shared Discord alert helper
├── skills/                    — YAML skill definitions (auto-discovered)
├── data/                      — SQLite DBs, audit logs (gitignored)
├── vault-manage.py            — interactive vault secret manager
├── setup.py                   — interactive setup wizard (single entry point)
└── setup.sh                   — deprecated stub (redirects to setup.py)
```

---

## Agents

Add agents in `config/agents.yaml`. Each agent can have its own bot account, tool access list, and channel/category routing.

Example agent types:

| Type | Built-in Tools | MCP Tools | Use Case |
|------|---------------|-----------|----------|
| **Coordinator** | Blocked (Edit, Write, Bash, MultiEdit) | All | Business ops — operates through MCP tools only |
| **Privileged** | Full access | All | Dev/ops — system admin, code changes, deployments |
| **Assistant** | Blocked | Restricted whitelist | Scoped role — only the tools they need |

### Access Control (Three-Layer)

Defined in `src/core/access_control.py`, sourced from `config/team.json`:

| Layer | What it controls | Where it lives |
|-------|-----------------|----------------|
| **1. Can they talk?** | Sender tier × agent tier → message allowed or ignored | `access_control.py` |
| **2. What tools?** | Sender tier → `--disallowedTools` computed per message | `access_control.py` → `agent_manager.py` |
| **3. Agent ceiling** | Static per-agent config (tools list, disallow_builtins) | `agents.yaml` |

**People tiers:** owner (full access) → admin (safe tools + HITL) → staff (assistant only, safe tools) → unknown (blocked)

**Agent tiers:** privileged (full CLI + all MCP) → coordinator (no CLI + all MCP) → assistant (no CLI + safe MCP only)

### Agent Config

Each agent has a `project_dir` containing CLAUDE.md (identity + tool docs), `.mcp.json` (MCP server config), and `.claude/settings.json` (permissions, env, allowed tools).

```yaml
# config/agents.yaml — example
agents:
  primary:
    display_name: "MyAgent"
    project_dir: ./agents/primary
    tools: all                    # MCP tools ("all" or explicit list)
    disallow_builtins:            # Block Claude Code built-in tools
      - Edit
      - Write
      - Bash
      - MultiEdit
    routing:
      discord:
        account: default
        channels: []              # All channels (wildcard)
```

### Communication Style (Caveman Mode)

Agents can use a terse communication style ("caveman mode") with four levels:

| Level | Behavior |
|-------|----------|
| off | Normal prose |
| lite | Drop pleasantries and hedging. Sentences intact. |
| full | Drop articles and filler. Fragments OK. |
| ultra | Maximum compression. Telegraphic. |

The style definition lives in `config/CAVEMAN.md` (shipped with Core, copied to overlay during setup). Each agent's CLAUDE.md references it with a level:

```markdown
## Communication Style
See @../../config/CAVEMAN.md — active full mode.
```

Manage via `settings.py` → Agents → Caveman mode, or edit CLAUDE.md directly. Remove the reference to disable.

### Routing

Routing determines which agent handles an incoming message. Each connector has its own routing namespace (`routing.discord`, `routing.telegram`, etc.) with connector-specific keys.

**Discord routing — scope vs filters:**

Routing has two layers: **scope** (which channels to listen in) and **filters** (additional constraints applied on top).

*Scope* (priority-based — first match wins):

| Priority | Config Key | Effect |
|----------|-----------|--------|
| 1 | `channels: [id, ...]` | Exact channel ID match — highest priority |
| 2 | `categories: [id, ...]` | All channels within a Discord category |
| 3 | `channels: []` (empty) | Wildcard — all channels |
| 4 | DM fallback (implicit) | Any agent bound to the bot handles DMs |

*Filters* (applied independently of scope):

| Filter | Config Key | Effect |
|--------|-----------|--------|
| **Bot account** | `account` | Namespace — each bot routes independently. Not optional. |
| **Guild/Server** | `guilds: [id, ...]` | Restricts to specific servers. Empty = all servers. |
| **Mentions** | `mentions: true/false` | When true, only respond if @mentioned or in a DM. |

Multiple agents **can** match the same message if routing rules overlap — all matching agents receive it.

**Examples:**

```yaml
# Agent responds to all channels on bot "main", only when @mentioned
routing:
  discord:
    account: main
    channels: []
    mentions: true

# Agent scoped to a specific Discord category
routing:
  discord:
    account: assistant
    categories: ["1234567890"]
    mentions: false

# Agent locked to one channel in one server
routing:
  discord:
    account: ops
    channels: ["9876543210"]
    guilds: ["1111222233"]
    mentions: true
```

**LLM defaults** can include `mcp_config` to wire external MCP servers for all agents:

```yaml
defaults:
  llm:
    provider: claude_code
    model: sonnet
    mcp_config: /path/to/mcp.yaml   # Optional: external MCP servers
```

---

## Tools

Tools are `@tool` decorated async Python functions in `src/tools/`. They auto-discover on startup — drop a file, it's available.

Included tool packs:

| Category | Tools |
|----------|-------|
| **Memory** | store, search, semantic_search, forget |
| **Team** | list, get, add, update, remove |
| **Google Workspace** | Gmail (search, read, send), Calendar (list, create), Drive (search, list, download, create, upload, delete, share) |
| **Trello** | boards, lists, cards, create_card, activity |
| **Media** | gemini_analyze_image, gemini_analyze_video, gemini_generate_image, transcribe_audio, video_frames, video_clip |
| **Discord** | read_channel_history, read_message, search_channel_history, send_discord_file |
| **Cloudflare** | zones, dns_list, dns_update |
| **Notion** | search, read, create |
| **Web** | web_search (Tavily) |
| **Compression** | compress_context, decompress_context |
| **System** | create_skill, read_url, browse_url, browser, install_mcp, list_capabilities |
| **Tmux** | list, send, read, new |
| **Inter-agent** | send_message, ask_agent, send_to_agent |

*HITL-gated tools (marked in config) require human approval via Discord reaction.*

Add your own integrations by dropping a `@tool` decorated Python file into `src/tools/`.

### Tool Development Guide

1. **Create the file** — `src/tools/my_tool.py` (or `<layer2-module>/tools/my_tool.py` for domain tools)
2. **Decorate with `@tool`** — name, description, and optionally `hitl=True` for gated tools
3. **Type-hint parameters** — the schema is auto-generated from type hints (str, int, bool, Optional, list)
4. **Accept `ctx: ToolContext`** — provides `ctx.vault` (secrets), `ctx.config` (config dict), `ctx.storage` (SQLite)
5. **Return a string** — the tool result shown to the LLM

```python
from src.core.base import ToolContext
from src.core.tools import tool

@tool(name="my_tool", description="Does something useful")
async def my_tool(ctx: ToolContext, query: str, limit: int = 10) -> str:
    api_key = ctx.vault.get("my-api-key")
    # ... do work ...
    return f"Found {limit} results for {query}"
```

The tool is auto-discovered on startup — no imports to add, no registry to update. It's immediately available to all agents via MCP.

**Testing:** `uv run python scripts/test-tools.py --tool my_tool`

**HITL gating:** Add `hitl=True` to the decorator for tools that should require human approval regardless of deployment config: `@tool(name="dangerous_tool", description="...", hitl=True)`

### MCP Configuration

Each agent gets an MCP server (`src/mcp_server.py`) spawned as a subprocess by Claude Code CLI. The server config lives in two places:

**Per-agent `.mcp.json`** — in the agent's project directory, configures the T.A.R.S tool server:

```json
{
  "mcpServers": {
    "tars-tools": {
      "command": "/opt/tars/.venv/bin/python3",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/opt/tars",
      "env": { "TARS_PROFILE": "${TARS_PROFILE:-}" }
    }
  }
}
```

**External MCP servers** — additional MCP servers (third-party tools, other systems) are configured in `config/mcp.yaml`:

```yaml
servers:
  tars-tools:
    transport: stdio
    command: /opt/tars/.venv/bin/python3
    args: ["-m", "src.mcp_server"]
    cwd: /opt/tars

  # Example: connect an external MCP server
  my-server:
    transport: stdio
    command: npx
    args: ["-y", "@my-org/mcp-server"]
```

When `mcp_config` is set in `defaults.llm`, all agents get access to the external servers. Tools from external MCP servers appear as native tools to the LLM — no difference from built-in tools.

### Inter-Agent Communication

Agents can communicate via two mechanisms, depending on whether they share a process:

**`ask_agent` / `send_to_agent` (not currently functional):**

These tools exist in `builtin.py` and route through `AgentManager.handle_internal_message()`. They are designed for direct HTTP LLM backends (Claude API, Groq, Ollama) where the system controls the message loop. They do **not** work when agents use the Claude Code CLI as their LLM backend, because the CLI owns its own session and cannot accept programmatically injected messages.

Since all agents currently use Claude Code CLI, these tools are non-functional. They remain in the codebase for future use when agents switch to direct API backends.

**Discord @mention workaround (current method):**

All inter-agent communication goes through Discord. Agents @mention each other using `send_message`:

```
send_message(channel_id="...", content="<@BOT_ID> your message here", bot="sender_name")
```

The target bot's Discord connector picks up the @mention and responds naturally. This works across all agents regardless of process boundaries.

Agent CLAUDE.md files should document bot IDs for each peer and instruct agents to use this method.

---

## Services

### Dual-Instance Pattern

T.A.R.S supports running two instances from the same codebase for separation of privilege:

| Instance | Service | Profile | Sandbox | Purpose |
|----------|---------|---------|---------|---------|
| **Main** | `tars.service` | default | Sandboxed (`ProtectSystem=strict`, `NoNewPrivileges=true`, capability drop) | User-facing agents — business ops, assistants |
| **Ops** | `tars-rescue.service` | `rescue` | Unsandboxed (full filesystem, sudo access) | Single privileged agent for dev/ops, deploys, debugging |

Both run the same codebase (`uv run python -m src.main`), differentiated by `--profile`. They share SQLite databases (WAL mode) and the Fernet vault. Each instance has its own lock file.

The main instance blocks built-in tools (Edit, Write, Bash) via `disallow_builtins` — agents operate through MCP tools only. The ops instance grants full access, but restricts who can use it via access control (owner-only).

`setup.py` offers to set up the ops instance in Step 12. It creates a separate `agents.rescue.yaml`, a dedicated bot account, and the systemd unit.

### Systemd

```bash
# Main service
systemctl start tars.service
systemctl status tars.service
journalctl -u tars -f

# Ops instance
systemctl start tars-rescue.service
```

### Sandboxing (Main Instance)

The main service unit applies systemd sandboxing:

- `ProtectSystem=strict` — filesystem is read-only except explicit `ReadWritePaths`
- `NoNewPrivileges=true` — no privilege escalation
- `CapabilityBoundingSet=` — all capabilities dropped
- `PrivateDevices=true` — no access to physical devices
- `RestrictNamespaces=true` — no namespace creation
- `SystemCallFilter=~@mount @reboot @swap @debug @obsolete` — dangerous syscalls blocked
- `ReadWritePaths` — limited to overlay dirs, data, tmp, and Claude Code cache

The ops instance intentionally omits these restrictions.

### File Ownership

All files under the install directory are owned by `tars:tars` (the service user). Root-owned files inside the tree cause latent breakage — readable but not writable by the service, and `uv sync` / `uv run` fail on root-owned files in `.venv/`.

When editing files as root (e.g. from a Claude Code session running as root), chown back after every save:

```bash
# Check for misowned files
find /opt/tars -not -user tars 2>/dev/null
# Fix
sudo chown -R tars:tars <paths>
```

---

## Security

| Layer | Implementation |
|-------|---------------|
| **Access control** | Three-layer: sender tier × agent tier, per-message tool filtering, static agent ceiling |
| **Credentials** | Fernet vault (`config/secrets.enc`), per-instance random salt, PBKDF2 key derivation |
| **HITL** | Configurable gated tools, Discord reaction approval, timeout with fail-closed default |
| **Content safety** | Three-stage pipeline: sanitize (log-only) → injection scoring → behavioral monitoring |
| **Security alerts** | Real-time alerts to configured Discord channel for content safety and behavioral anomalies |
| **Rate limiting** | Per-tool sliding window (enforce mode), record-before-execute (TOCTOU-safe) |
| **Bot-to-bot loop detection** | Per-bot sliding window (5 exchanges / 60s) — suppresses runaway ping-pong |
| **Duplicate message suppression** | Per-channel dedup — same content to same channel within 120s is dropped |
| **Audit** | JSONL log of every tool call, HITL decision, auth event |
| **SSRF** | App-layer URL validation: scheme whitelist, RFC1918/localhost/link-local block, DNS rebinding protection |
| **Path traversal** | `validate_file_path()` on all tools that write to user-controlled paths |
| **SQL injection** | Parameterized queries everywhere |
| **Env isolation** | Claude Code subprocess gets allowlisted env vars only — no secret leakage |

### Content Safety Pipeline

Three-stage pipeline in `src/core/content_safety.py`. Applied to **web-facing tool output** — tools that fetch external content which could contain adversarial payloads. Runs in `src/mcp_server.py` after tool execution.

**Web-facing tools scanned:** `web_search`, `browse_url`, `read_url`, `browser`, `gmail_read`, `gmail_search`, `download_file` (defined in `src/core/alerts.py:WEB_FACING_TOOLS`).

#### Stage 1: Sanitization

`sanitize(text)` — strips content that shouldn't reach the LLM context:

- Invisible Unicode (zero-width joiners, bidi overrides, BOM)
- `<script>` and `<style>` blocks, then all remaining HTML tags
- HTML entity unescaping
- Data URIs and base64 blocks >200 chars → `[base64-removed]`
- Unicode NFC normalization
- Whitespace collapsing

**Currently log-only** — sanitize runs and alerts to the security channel when >50 chars would be removed, but the original content passes through unchanged. This allows monitoring false-positive rates before switching to active stripping.

#### Stage 2: Injection Scoring

`score_injection(text)` — scores external content 0–10 for prompt injection signals. 23 regex patterns across four categories:

| Category | Example patterns | Score per match |
|----------|-----------------|-----------------|
| **Instruction injection** | `ignore previous instructions`, `<\|im_start\|>`, `[INST]` | 3–4 |
| **Authority spoofing** | `emergency override`, `admin mode`, `debug mode:` | 2–3 |
| **Exfiltration** | `send this to`, `email it to`, `post in channel` | 2 |
| **Delimiter attacks** | `--- BEGIN SYSTEM`, `HUMAN:`, `ASSISTANT:` | 2–3 |

Alerts fire at **score >= 3** with the tool name, score, and matched patterns. Scans first 50KB for performance.

#### Stage 3: Behavioral Monitoring

`BehaviorMonitor` in `src/core/content_safety.py` — watches agent action patterns over time. Four anomaly checks:

| Check | Trigger | Severity |
|-------|---------|----------|
| **Sensitive after external** | Gated tool within 5 min of consuming external content | HIGH |
| **Novel tool** | Tool not in agent's first 50-call baseline | MEDIUM |
| **Volume spike** | 3× rolling average in 10 min window (min 10 calls) | MEDIUM |
| **Rapid sensitive** | 3+ different sensitive tools within 5 min | HIGH |

Sensitive tools for behavioral monitoring: `send_email`, `share_drive_file`, `install_mcp`, `team_add/remove/update`, `create_skill`, `send_message`.

### Security Alerts

`AlertSender` in `src/core/alerts.py` — sends real-time alerts to a Discord channel via REST API.

**Configuration** (Layer 3 `config.yaml`):

```yaml
security:
  alert_channel: "123456789"    # Discord channel ID for all security alerts
  alert_bot: "tars"             # Bot account for sending (vault key: discord-{alert_bot})
```

Both fields are required for alerts to fire. If either is missing, alerts fall back to `logger.warning()` only.

The alert bot must have access to the alert channel. The bot token is resolved from the vault as `discord-{alert_bot}`.

**Alert types sent to the channel:**

- Content safety: injection score >= 3 (tool name, score, matched patterns)
- Content sanitized: >50 chars of invisible content stripped (tool name, chars removed)
- Behavioral anomalies: all four checks above (agent ID, check type, severity, details)
- HITL decisions: approvals, denials, timeouts

### HITL (Human-in-the-Loop)

Configurable in Layer 3 `config.yaml`. When an agent calls a gated tool, execution pauses and an approval request is posted to the configured Discord channel. An approver must react with ✅ or ❌.

**Configuration:**

```yaml
security:
  hitl:
    connector: discord
    channel: "123456789"        # Channel for approval requests
    approvers: ["user_id"]      # Discord user IDs who can approve
    timeout: 1800               # Seconds before auto-deny (default: 30 min)
    fail_mode: closed           # "closed" = deny on timeout/error, "open" = allow
    poll_interval: 3            # Seconds between reaction checks
    gated_tools:                # Tools requiring approval
      - send_email
      - install_mcp
      - cloudflare_dns_update
      - team_add
      - team_update
      - team_remove
      - drive_delete
      - discord_delete_channel
```

Tools are gated by **two mechanisms** (either triggers the gate):
1. Listed in `gated_tools` in config (Layer 3 — deployment-specific)
2. Decorated with `@tool(hitl=True)` in source (Core — hardcoded for universally dangerous tools)

**Example flow:**

1. User asks agent: "Email the quarterly report to the client"
2. Agent calls `send_email` → MCP middleware detects it's in `gated_tools`
3. Approval request posted to HITL channel: "🔒 **HITL Approval Required** — Agent `main` wants to call `send_email` with args: {to: ..., subject: ...}"
4. Approver reacts ✅ → tool executes, result returned to agent
5. If ❌ or timeout → tool returns denial message, agent explains to user
6. Decision logged to audit trail and alert channel

### Access Control

Three-tier system in `src/core/access_control.py`. Every incoming message is checked against sender tier × agent tier.

**Sender tiers** (from `config/team.json`):
- `owner` — full access, can use any tool
- `admin` / `staff` — safe tools only (unless HITL-approved)
- `unknown` — denied by default (`unknown_policy: deny`)

**Safe tools allowlist** — configured per-deployment in Layer 3:

```yaml
security:
  access_control:
    safe_tools:
      - memory_search
      - web_search
      - team_list
      # ... read-only tools
    unknown_policy: deny
```

Tools not in `safe_tools` require owner tier, or HITL approval for agents. Each deployment configures its own allowlist based on which tools are available and appropriate.

### Rate Limiting

Per-tool sliding window in `src/core/rate_limiter.py`. Records the call **before** execution (TOCTOU-safe — no race between check and execute).

```yaml
security:
  rate_limits:
    mode: enforce               # "enforce" = block, "log" = warn only
    defaults:
      max_per_hour: 100
    tools:
      send_email:
        max_per_hour: 10
      install_mcp:
        max_per_day: 5
```

Wildcard patterns supported:

```yaml
    tools:
      send_email:
        max_per_hour: 10
      install_mcp:
        max_per_day: 5
      amazon_sp_*:               # Matches all Amazon SP-API tools
        max_per_hour: 60
      trello_*:                  # Matches all Trello tools
        max_per_hour: 30
```

---

## Vault

Fernet-encrypted credential store at `config/secrets.enc`. Per-instance random salt at `config/secrets.salt`. Key derived from passphrase via PBKDF2 (100k iterations). Secrets decrypted into memory at startup, passphrase never stored.

Manage via: `uv run python vault-manage.py`

---

## Memory System

Inline SQLite with FTS5 full-text search and BGE-small-en-v1.5 embeddings (384-dim, ONNX). No external services. DB at `data/memory.db`.

| Feature | Implementation |
|---------|---------------|
| **Storage** | SQLite WAL mode, UUID primary keys |
| **Search** | FTS5 keyword search + embedding cosine similarity |
| **Scope** | Per-agent (`agent:<id>`), global, group — agents only see their own + shared |
| **Context injection** | Pinned + high-confidence memories injected at session start |
| **Audit trail** | `changelog` table logs every insert/update/delete |
| **Dedup** | Semantic deduplication at 0.80 similarity threshold |

### Memory Lifecycle

Memories decay when not accessed. The decay rate is 0.0108/day.

```
Day 0:  0.70 confidence (new memory)
Day 10: 0.59
Day 30: 0.38
Day 60: 0.05 → archived (hidden from search)
+90 days archived → permanently deleted
```

**Pinning:** Memories can be pinned to make them immune to decay. Pinned memories always appear in search results and are injected at session start. Pin a memory by passing `pinned=true` when storing via `memory_store`, or update an existing memory's pinned status through the memory database directly. Use pinning for critical facts that should never expire (e.g. system configuration, key contacts, core procedures).

**Accessing resets decay:** When a memory is returned in a search result, its `last_accessed` timestamp updates, resetting the decay clock. Frequently useful memories naturally survive longer.

### Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| **semantic** | Facts, knowledge | "Client prefers email over Slack for updates" |
| **episodic** | Events, experiences | "Deployed v2.1 on March 15, rollback needed for auth bug" |
| **procedural** | How-to, processes | "Use vault-manage.py to rotate API keys" |

---

## Context Compression

Optional rule-based compression for agent context files (codex docs, skill prompts). Strips unambiguous prose filler while preserving code blocks, config, paths, URLs, headings, and tables. No LLM calls — pure regex/heuristics, runs in milliseconds.

CLAUDE.md files are excluded from batch compression — they are carefully tuned agent prompts where every word matters.

**Configuration** (`config.yaml`):

```yaml
security:
  compression:
    enabled: false              # opt-in, disabled by default
    level: standard             # lite (filler only) | standard (filler + contractions)
    memory_recall: false        # compress memories at injection time
```

Per-agent override in `agents.yaml`:

```yaml
agents:
  my_agent:
    compression:
      enabled: true
      level: lite
```

**Levels:**
- `lite` — strips filler phrases only ("please note that", "it is important to", etc.)
- `standard` — filler phrases + contractions ("do not" → "don't")

**Tools:** `compress_context` (with lite/standard/report levels), `decompress_context` (restore from .original backup)

**Batch:** `scripts/compress-context.sh [--dry-run] [--level lite|standard]`

**Implementation:** `src/lib/compressor.py` (engine), `src/tools/compress.py` (MCP tool wrapper)

---

## Scheduled Tasks (systemd timers)

All scheduled tasks use systemd timers (`Persistent=true` — catches up missed runs after reboot). Timer templates in `config/timers/`, installed automatically by `setup.py` via `scripts/install-systemd.sh`.

| Timer | Schedule | Script | Purpose | Alerts |
|-------|----------|--------|---------|--------|
| tars-memory-context | Every 30 min | regen-memory-context.sh | Regenerates `MEMORY_CONTEXT.md` with memory stats and service health snapshot for agent context injection | No |
| tars-memory-decay | Daily 03:00 | memory-decay.sh | Applies confidence decay (0.0108/day), archives memories below threshold, purges archives older than 90 days | On purge |
| tars-health-audit | Every 6h | health-audit.sh | Checks service status, disk/RAM/swap usage, zombie processes, cleans temp files | On warning/critical |
| tars-integrity | Every 12h | monitor-integrity.sh | SHA256 checksums of critical files vs baseline — detects unauthorized changes | On mismatch |
| tars-exposure | Daily 02:00 | monitor-exposure.sh | Scans for unexpected public-facing ports | On unexpected port |
| tars-container-health | Every 6h | monitor-container-health.sh | Checks Docker containers for security drift (capabilities, non-root, no-new-privileges) | On drift |

**Dependencies:** All timer services set `After=tars.service` to avoid running during startup. The memory-context timer depends on the memory database existing (`data/memory.db`).

**Alerts:** Scripts that detect issues send Discord alerts via `scripts/lib-alert.sh`, which reads the bot token from the Fernet vault. The alert channel is configured in `config.yaml` under `security.alert_channel`.

**Installation:** `setup.py` generates unit files into `<overlay>/systemd/` (substituting paths and injecting `TARS_OVERLAY`), then calls `scripts/install-systemd.sh` to symlink them into `/etc/systemd/system/` and enable timers. Timers are always installed (not optional) — they are required infrastructure.

**Adding custom timers:** Create `.service` + `.timer` files in `config/timers/` (Core) or `<overlay>/systemd/` (deployment-specific), then run `scripts/install-systemd.sh <overlay-dir>`.

---

## Operations

### Start / Stop / Status
```bash
systemctl start tars.service
systemctl stop tars.service
systemctl status tars.service
journalctl -u tars -f
```

### Updating a Running Install

After pulling new code, run `scripts/sync.sh` **before** restarting the service:

```bash
cd /opt/tars
sudo -u tars git pull
sudo -u tars scripts/sync.sh              # install all layers
sudo systemctl restart tars
```

`scripts/sync.sh` runs `uv sync` (Core) then installs Layer 2 and Layer 3 `requirements.txt` files discovered via `TARS_OTHS` and `TARS_OVERLAY` env vars. If these aren't in the shell environment, the script falls back to reading them from the `tars.service` systemd unit — so the service unit is the single source of truth. No shell profile exports needed.

This ensures Layer 2 packages survive Core dependency reconciliation — bare `uv sync` actively removes packages it doesn't recognise.

The service unit uses `uv run --no-sync` so that service start never writes to the sandboxed, read-only `.venv`. Dependency updates are therefore **explicit**: `scripts/sync.sh` runs in a normal shell (where `.venv` is writable) before the restart.

Skipping sync after a dep change means the service will either crash on startup (`ImportError` for a new dep) or silently run stale code against a bumped version. The script is a no-op when nothing changed, so it's safe to run unconditionally as part of the deploy ritual.

### Git Workflow

Direct commits to `main` are blocked by a pre-commit hook (shipped in `hooks/pre-commit`, installed by `setup.py`). All Core changes go through feature branches:

```bash
git checkout -b fix/description
# make changes
git add <files> && git commit -m "fix: description"
git push -u origin fix/description
gh pr create
# cross-review → merge via GitHub
```

**Remote neutralisation:** Non-maintainer installs have `origin` renamed to `upstream` with push blocked (see Three-Layer Architecture above). Maintainers restore push access by renaming back.

**Hook enforcement:** The pre-commit hook checks the current branch name. If it's `main`, the commit is rejected with a formatted message explaining the branch + PR workflow. The hook also warns agents that deployment-specific files don't belong in Core.

`setup.py` installs hooks automatically (Step 3) and compares shipped hooks against installed ones on re-run, offering to update outdated hooks.

### Test Mode
```bash
uv run python -m src.main --profile test
```

### Run E2E Tests
```bash
uv run python scripts/test-tools.py                    # all tests
uv run python scripts/test-tools.py --tool team_list   # single tool
```

### Vault Management
```bash
uv run python vault-manage.py
```

---

## Docs

| Document | Purpose |
|----------|---------|
| **ARCHITECTURE.md** | This file — full system reference |
| **MIGRATION.md** | Migration guide from OpenClaw |
| **SCRIPTS.md** | All scripts with usage examples |
| **skills/README.md** | Skill format reference |
