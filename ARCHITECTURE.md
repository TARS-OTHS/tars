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
| **Rate limiting** | Per-tool sliding window (enforce mode), record-before-execute (TOCTOU-safe) |
| **Bot-to-bot loop detection** | Per-bot sliding window (5 exchanges / 60s) — suppresses runaway ping-pong |
| **Duplicate message suppression** | Per-channel dedup — same content to same channel within 120s is dropped |
| **Audit** | JSONL log of every tool call, HITL decision, auth event |
| **SSRF** | App-layer URL validation: scheme whitelist, RFC1918/localhost/link-local block, DNS rebinding protection |
| **Path traversal** | `validate_file_path()` on all tools that write to user-controlled paths |
| **SQL injection** | Parameterized queries everywhere |
| **Env isolation** | Claude Code subprocess gets allowlisted env vars only — no secret leakage |

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

After pulling new code, run `uv sync` **before** restarting the service:

```bash
cd /opt/tars
sudo -u tars git pull
sudo -u tars uv sync                       # reconcile .venv with lockfile

# If Layer 2 (TARS_OTHS) is configured and has a requirements.txt:
for dir in ${TARS_OTHS//:/ }; do
    [ -f "$(dirname "$dir")/requirements.txt" ] && sudo -u tars uv pip install -r "$(dirname "$dir")/requirements.txt"
done

sudo systemctl restart tars
```

The service unit uses `uv run --no-sync` so that service start never writes to the sandboxed, read-only `.venv`. Dependency updates are therefore **explicit**: `uv sync` runs in a normal shell (where `.venv` is writable) before the restart.

Skipping `uv sync` after a dep change means the service will either crash on startup (`ImportError` for a new dep) or silently run stale code against a bumped version. `uv sync` is a no-op when nothing changed, so it's safe to run unconditionally as part of the deploy ritual.

**Important**: `uv sync` only installs Core (Layer 1) dependencies from `pyproject.toml`. Layer 2 modules may declare their own deps in `requirements.txt` — these must be installed separately with `uv pip install -r`. Without this step, `uv sync` will actively remove Layer 2 packages it doesn't recognise.

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
