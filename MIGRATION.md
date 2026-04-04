# Migrating from OpenClaw to T.A.R.S

## 1. Overview

This guide covers migrating an existing TARS deployment running on OpenClaw to T.A.R.S. T.A.R.S is a lightweight replacement — see [TARSCLAW-SPEC.md](TARSCLAW-SPEC.md) for the full system architecture.

The migration happens in two steps:
1. **Kill OpenClaw** — build T.A.R.S, connect it to existing services, cut over
2. **Kill the Auth Proxy** — replace the proxy with in-process vault + security middleware

T.A.R.S is generic and deployment-agnostic. This document describes the migration process for any OpenClaw-based TARS deployment.

## 2. What You're Replacing

### 2.1 OpenClaw Gateway

The core dependency being removed. It provides:
- Message routing (Discord → agent)
- Agent lifecycle management (Docker sandbox start/stop)
- Tool dispatch via plugin system
- LLM gateway (`/v1/chat/completions`)
- Session state management

Typical systemd services:
- `openclaw-gateway.service` (Node.js)
- `openclaw-proxy.service` (Docker bridge proxy)
- `openclaw-dashboard.service` (web UI)

### 2.2 Existing Services (kept)

These services continue running through Step 1. Adapt to your deployment:

| Service | Purpose |
|---------|---------|
| Auth proxy | Credential injection, HITL gates, rate limiting, audit |
| Memory API | SQLite + FTS5 + semantic search |
| Embedding service | Vector embeddings for semantic search |
| MCP gateway | External tool servers (e.g., Google Workspace via MetaMCP) |

### 2.3 OpenClaw Plugins (replaced by native Python)

**Memory plugin** (typically TypeScript):
- Tools: `memory_search`, `memory_semantic_search`, `memory_store`, `session_state_save`, `session_state_get`
- Hooks: `before_agent_start` (auto-recall), `agent_end` (auto-save)

**Team plugin** (typically TypeScript):
- Tools: `team_list`, `team_get`, `team_add`, `team_update`, `team_remove`
- Hook: `before_prompt_build` (inject user context)
- Resolves connector user ID → team member

### 2.4 Auth Proxy Security Features

If your deployment uses an auth proxy, it likely provides:
- HITL-gated actions (Discord reaction-based approval flow)
- Per-route, per-agent rate limiting
- Content safety pipeline (sanitization, injection scoring, behavioral monitoring)
- Structured audit logging
- OAuth2 auto-refresh for external APIs

## 3. Migration Strategy — Two Cuts

```
Today                          After Step 1                    After Step 2 (final)
─────                          ────────────                    ────────────────────
OpenClaw Gateway               T.A.R.S process                T.A.R.S process
  ├── message routing            ├── message routing (new)       ├── message routing
  ├── agent lifecycle            ├── agent manager (new)         ├── agent manager
  ├── tool dispatch              ├── tool registry (new)         ├── tool registry
  ├── plugin hooks               ├── middleware chain (new)      ├── middleware chain
  └── LLM gateway               └── Claude Code CLI (new)       └── Claude Code CLI

Auth proxy                     Auth proxy (kept)               Fernet vault (in-process)
  ├── credential injection       ├── credential injection        ├── vault.get() calls
  ├── HITL gates                 ├── HITL gates                  ├── HITL middleware
  ├── rate limiting              ├── rate limiting               ├── rate limit decorators
  ├── OAuth2 refresh             ├── OAuth2 refresh              ├── OAuth2 module
  ├── content safety             ├── content safety              ├── content safety middleware
  └── audit logging              └── audit logging               └── audit JSONL

Memory API                     Memory API (kept)               Memory API (kept or inlined)
Embedding service              Embedding service (kept)        Embedding service (kept or inlined)
MCP gateway                    MCP gateway (kept)              MCP gateway (kept)
Many systemd services          2 systemd services              1-2 systemd services
Cron jobs                      Cron jobs (kept, migrate later) Built-in scheduler
```

## 4. Step 1 — Kill OpenClaw

**Goal:** Replace OpenClaw gateway with T.A.R.S. Everything else stays. Agents keep working — same memories, same channels, same security.

**Rollback:** Stop T.A.R.S, restart OpenClaw gateway. 30 seconds.

### 4.1 What to Build

Each component implements part of TARSCLAW-SPEC.md:

| Component | Spec Section | Details |
|-----------|-------------|---------|
| `src/core/registry.py` | 2.1–2.3 | Auto-discover modules from `src/` subdirs |
| `src/core/agent_manager.py` | 2.5 | Load agents from `config/agents.yaml`, manage sessions |
| `src/core/router.py` | 2.5 | Route messages: connector → agent based on routing config |
| `src/core/tools.py` | 5.1 | `@tool` decorator, schema from type hints, dispatch |
| `src/core/middleware.py` | 9.5–9.8 | Pre/post hooks on tool calls and LLM requests |
| `src/connectors/discord.py` | 2.6 | Multi-bot Discord via `discord.py` |
| `src/llm/claude_code.py` | 4.1 | Claude Code CLI as LLM engine |
| `src/main.py` | — | Entry point — start registry, connectors, agent manager |

### 4.2 What to Connect (existing services over HTTP)

| Service | T.A.R.S wrapper | Notes |
|---------|-----------------|-------|
| Auth proxy | `src/tools/auth_proxy.py` | Thin HTTP client — forward credentialed requests |
| Memory API | `src/tools/memory.py` | `memory_store`, `memory_search`, `memory_semantic_search` |
| Embedding service | Called by memory API | No wrapper needed |
| MCP gateway | `src/core/mcp_client.py` | Connect, discover tools, surface as native |

### 4.3 Port OpenClaw Plugins to Native Python

**Memory plugin → `src/tools/memory.py` + middleware:**
- `memory_store`, `memory_search`, `memory_semantic_search` → `@tool` functions calling memory API over HTTP
- `session_state_save`, `session_state_get` → `@tool` functions
- Auto-recall (inject memories before LLM call) → pre-LLM middleware
- Auto-save session state on agent end → post-session middleware

**Team plugin → `src/tools/team.py` + middleware:**
- `team_list`, `team_get`, `team_add`, `team_update`, `team_remove` → `@tool` functions
- Source of truth: Memory DB (pinned memories, never decay)
- Cache file: `config/team.json` (regenerated from DB for fast reads)
- User context injection → agent manager pre-LLM step
- Unknown user detection → injected as `<unknown-user>` block in prompt

### 4.4 Config Migration

```bash
tars migrate --from /path/to/existing/tars
```

Automated:
1. `config/agents.json` → `config/agents.yaml`
2. `config/team.json` → copy (same format)
3. `config/mcp-servers.json` → `config/mcp.yaml`
4. Discord channel→agent routing → `routing:` blocks in `agents.yaml`
5. `skills/registry.json` + skill files → `skills/*.yaml`
6. Agent workspaces → `agents/<name>/workspace/`
7. SOUL/identity files → `agents/<name>/SOUL.md`

Manual:
- Verify Discord bot tokens are accessible
- Verify memory API, auth proxy, MCP gateway are reachable
- Test each agent end-to-end on a test channel

### 4.5 Parallel Testing Strategy

T.A.R.S runs alongside TARS with zero interference. Key: **separate Discord bot accounts for testing**.

```
                      TARS (production)              T.A.R.S (testing)
                      ────────────────               ──────────────────
Discord bot:          Existing bot token              New test bot token
Channels:             Production channels             #tars-test (new channel)
LLM:                  Claude Code via OpenClaw         Claude Code direct
Memory:               memory-api (shared)             memory-api (shared, read-only)
Auth proxy:           auth-proxy (shared)             auth-proxy (shared)
MCP:                  mcp-gateway (shared)            mcp-gateway (shared)
```

**Setup for parallel testing:**

```bash
# 1. Create a test Discord bot at https://discord.com/developers/applications
#    Invite it to the same server with same permissions
#    Create a #tars-test channel

# 2. Migrate config (does not touch existing TARS)
cd /path/to/tars-v2
uv sync
python -m src.cli migrate --from /path/to/existing/tars

# 3. Configure test bot token in your vault or .env

# 4. Set routing to test channel only in config/agents.yaml:
#    agents:
#      my-agent:
#        routing:
#          discord:
#            channels: ["<tars-test-channel-id>"]
#            mentions: true

# 5. Run T.A.R.S — both systems running simultaneously
uv run python -m src.main

# 6. Verify
python -m src.cli healthcheck
# Test: message @test-bot in #tars-test
# Test: memory search, tool execution, skills
```

**What can be tested in parallel:**
- Message handling, routing, typing indicators
- Skill execution
- Memory search (reads from same memory API)
- Tool execution via auth proxy
- MCP tool calls

**What shares state (safe):**
- Memory API — read/write safe, same memories visible
- Auth proxy — credentialed API calls work through same proxy
- MCP gateway — same tools available

**Zero-downtime cutover:**
Stop old bots, update config, start new bots. No data migration, no state transfer. Discord reconnection takes ~2 seconds.

### 4.6 Cutover Procedure

```bash
# Pre-cutover checklist:
#   [ ] T.A.R.S tested on test channel for ≥1 week
#   [ ] All skills working
#   [ ] Memory read/write verified
#   [ ] Auth proxy tools tested (HITL approval flow)
#   [ ] Typing indicators showing

# 1. Cutover (60 seconds, zero downtime)
systemctl stop openclaw-gateway.service
systemctl stop openclaw-proxy.service
systemctl stop openclaw-dashboard.service

# Update T.A.R.S config: production channels + production bot token
systemctl start tars.service
# Verify agents respond

# 2. Rollback (if needed, 30 seconds)
systemctl stop tars.service
systemctl start openclaw-gateway.service
```

### 4.7 What's Running After Step 1

```
RUNNING:
  tars.service              ← NEW (replaces openclaw-gateway, proxy, dashboard)
  auth-proxy                ← KEPT (credentials, HITL, rate limiting, audit)
  memory-api                ← KEPT (memory storage + search)
  embedding-service         ← KEPT (semantic search)
  mcp-gateway               ← KEPT (external tool servers)
  cron jobs                 ← KEPT (memory lifecycle, security)

REMOVED:
  openclaw-gateway.service
  openclaw-proxy.service
  openclaw-dashboard.service
  OpenClaw config directory (archived)
  OpenClaw plugins (replaced by native Python)
  Docker agent sandboxes (agents run in-process)
  OpenClaw npm package
```

## 5. Step 2 — Kill the Auth Proxy

**Prerequisite:** Step 1 stable for at least one week.

**Goal:** Replace auth proxy with in-process vault + security middleware. Fully realize TARSCLAW-SPEC.md sections 9.2–9.10.

**Rollback:** Restart auth-proxy, revert tool code to HTTP wrappers.

### 5.1 What to Build

| Component | Spec Section | Replaces |
|-----------|-------------|----------|
| `src/vault/fernet.py` | 9.2 | External vault + auth proxy credential injection |
| `src/core/hitl.py` | 9.5 | Auth proxy HITL system |
| `src/core/rate_limiter.py` | 9.6 | Auth proxy rate limiting |
| `src/core/audit.py` | 9.7 | Auth proxy audit logging |
| `src/core/content_safety.py` | 9.8 | Auth proxy content safety |
| `src/auth/oauth2.py` | — | Auth proxy OAuth2 refresh |
| `tars vault migrate` | — | One-time: existing vault → Fernet vault |

### 5.2 Migration Sequence

1. **Build vault** — Fernet encryption, `vault.get()`/`vault.set()`, passphrase handling
2. **Migrate secrets** — `tars vault migrate --from /path/to/existing/vault`
3. **Build OAuth2 module** — token refresh for external APIs
4. **Update tool code** — change HTTP proxy calls to `vault.get()` + direct API calls (tool by tool)
5. **Build HITL middleware** — Discord reaction flow, configurable gates, timeout/fail-mode
6. **Build rate limiter** — decorator-based, config-driven
7. **Build audit logging** — JSONL writer with secret redaction
8. **Build content safety** — sanitization + scoring + behavioral monitoring
9. **Test everything** — each gated tool tested, rate limits verified, audit log inspected
10. **Cutover** — stop auth-proxy, all credentialed calls now go direct

### 5.3 Cutover Procedure

```bash
# All tools ported to direct API calls + vault
# HITL, rate limiting, audit, content safety running in-process

# Stop the proxy
systemctl stop auth-proxy.service

# Verify
tars healthcheck

# Rollback if needed
systemctl start auth-proxy.service
```

### 5.4 What's Running After Step 2

```
RUNNING:
  tars.service              ← The whole system
  memory-api                ← KEPT (or inline later)
  embedding-service         ← KEPT (or inline later)
  mcp-gateway               ← KEPT (external tool servers)

REMOVED:
  auth-proxy                ← replaced by vault + middleware
  Host cron jobs            ← migrated to built-in scheduler (or kept)
  Security monitors         ← replaced by content safety middleware + audit
```

## 6. Optional Step 3 — Consolidate Services

Not required. Do if external service maintenance becomes a burden.

- **Inline memory API** — rewrite in Python, use SQLite directly. Eliminates Node.js dependency.
- **Inline embedding** — load embedding model in-process. Eliminates one service.
- **Keep MCP gateway** — external tool servers justify their own process.

Endgame: one Python process + MCP gateway.

## 7. Compatibility Guarantees

- **Memory schema**: Compatible. Step 1 uses the same memory API. Step 2 (if inlined) uses same SQLite schema.
- **Agent config**: `agents.yaml` is a superset of `agents.json`. No information loss.
- **Discord routing**: Same channel→agent mapping, same @mention support, same bot tokens.
- **LLM sessions**: Session history preserved — T.A.R.S reads the same messages table.
- **Skills**: Skill registry maps to `skills/` YAML directory.
- **Security**: HITL gates, rate limiting, content safety, audit logging all preserved — rebuilt as in-process middleware.
- **Team context**: Same team data, same user resolution, same prompt injection — Python instead of TypeScript.

## 8. What Gets Simpler

| Before (OpenClaw) | After Step 1 | After Step 2 (final) |
|------|-------------|---------------------|
| Many systemd services | 2 (T.A.R.S + supporting) | 1-2 |
| Multiple Docker containers | Fewer (auth-proxy, memory, embedding, MCP) | Minimal (MCP only) |
| Cron jobs | Kept | Built-in scheduler |
| Multi-language (Python + TypeScript) | Python + Node.js (memory API) | Python only |
| OpenClaw npm dependency | None | None |
| External vault + auth proxy | External vault + auth proxy | Fernet vault (one file) |
| Plugin SDK (TypeScript) | `@tool` decorator (Python) | `@tool` decorator (Python) |
| Docker sandbox per agent | Shared process | Shared process |

## 9. What You Lose (and why that's OK)

- **Per-agent Docker isolation**: Agents share a process. If you need hard isolation, run multiple T.A.R.S instances.
- **Network-layer credential injection**: Replaced by in-process vault. Same trust boundary.
- **OpenClaw plugin ecosystem**: Replaced by `@tool` decorator and YAML skills. Simpler, Python-native.
