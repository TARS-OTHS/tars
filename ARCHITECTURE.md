# T.A.R.S — Architecture & Operations Reference

> Last updated: 2026-04-04

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
│   │   ├── ingest.py          — create_skill, read_url, browse_url, install_mcp, list_capabilities
│   │   └── builtin.py         — send_message, ask_agent, send_to_agent
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
│   ├── install-timers.sh      — install all systemd timers
│   ├── google-reauth.py       — Google OAuth2 re-authentication helper
│   └── lib-alert.sh           — shared Discord alert helper
├── skills/                    — YAML skill definitions (auto-discovered)
├── data/                      — SQLite DBs, audit logs (gitignored)
├── vault-manage.py            — interactive vault secret manager
├── setup.py                   — interactive setup wizard
└── setup.sh                   — system-level setup (deps, service account)
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
    tools: all                    # MCP tools
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
| **System** | create_skill, read_url, browse_url, browser, install_mcp, list_capabilities |
| **Tmux** | list, send, read, new |
| **Inter-agent** | send_message, ask_agent, send_to_agent |

*HITL-gated tools (marked in config) require human approval via Discord reaction.*

Add your own integrations by dropping a `@tool` decorated Python file into `src/tools/`.

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

T.A.R.S runs as a systemd service. You can run multiple instances with different profiles:

```bash
# Main service
systemctl start tars.service
systemctl status tars.service
journalctl -u tars -f

# Additional instance with different profile
systemctl start tars-rescue.service
```

Both run the same codebase (`uv run python -m src.main`), differentiated by `--profile`. They share SQLite databases (WAL mode) and the Fernet vault. Each instance has its own lock file.

---

## Security

| Layer | Implementation |
|-------|---------------|
| **Access control** | Three-layer: sender tier × agent tier, per-message tool filtering, static agent ceiling |
| **Credentials** | Fernet vault (`config/secrets.enc`), per-instance random salt, PBKDF2 key derivation |
| **HITL** | Configurable gated tools, Discord reaction approval, timeout with fail-closed default |
| **Content safety** | Three-stage pipeline: sanitize → injection scoring → behavioral monitoring |
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

Wildcard patterns supported (e.g., `amazon_sp_*: max_per_hour: 60`).

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

Memories decay when not accessed. Pinned memories are immune.

```
Day 0:  0.70 confidence (new memory)
Day 10: 0.59
Day 30: 0.38
Day 60: 0.05 → archived (hidden from search)
+90 days archived → permanently deleted
```

### Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| **semantic** | Facts, knowledge | "Client prefers email over Slack for updates" |
| **episodic** | Events, experiences | "Deployed v2.1 on March 15, rollback needed for auth bug" |
| **procedural** | How-to, processes | "Use vault-manage.py to rotate API keys" |

---

## Scheduled Tasks (systemd timers)

All scheduled tasks use systemd timers (`Persistent=true` — catches up missed runs after reboot). Timer/service files in `config/timers/`, installed via `scripts/install-timers.sh`.

| Timer | Schedule | Script | Purpose |
|-------|----------|--------|---------|
| tars-memory-context | Every 30 min | regen-memory-context.sh | Memory stats snapshot for agents |
| tars-memory-decay | Daily 03:00 | memory-decay.sh | Confidence decay, archive, purge |
| tars-health-audit | Every 6h | health-audit.sh | System health + temp cleanup |
| tars-integrity | Every 12h | monitor-integrity.sh | File integrity checksums |
| tars-exposure | Daily 02:00 | monitor-exposure.sh | Public port scanning |

Add your own timers by creating service+timer files in `config/timers/` and running `scripts/install-timers.sh`.

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
sudo -u tars TARS_OTHS=... TARS_OVERLAY=... scripts/sync.sh   # install all layers
sudo systemctl restart tars
```

`scripts/sync.sh` runs `uv sync` (Core) then installs Layer 2 and Layer 3 `requirements.txt` files discovered via `TARS_OTHS` and `TARS_OVERLAY` env vars. This ensures Layer 2 packages survive Core dependency reconciliation — bare `uv sync` actively removes packages it doesn't recognise.

The service unit uses `uv run --no-sync` so that service start never writes to the sandboxed, read-only `.venv`. Dependency updates are therefore **explicit**: `scripts/sync.sh` runs in a normal shell (where `.venv` is writable) before the restart.

Skipping sync after a dep change means the service will either crash on startup (`ImportError` for a new dep) or silently run stale code against a bumped version. The script is a no-op when nothing changed, so it's safe to run unconditionally as part of the deploy ritual.

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
| **ROADMAP.md** | Feature roadmap |
| **SCRIPTS.md** | All scripts with usage examples |
| **skills/README.md** | Skill format reference |
