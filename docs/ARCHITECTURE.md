# Architecture

## Overview

TARS is a layered platform for running AI agents with persistent memory, secure credential handling, team awareness, and Docker-based isolation.

```
┌─────────────────────────────────────────────────────────────┐
│                      User Layer                              │
│  Discord · Slack · Telegram · WhatsApp · Signal              │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                   OpenClaw Gateway                           │
│  Message routing · Agent lifecycle · Tool dispatch            │
│  Session management · Cron scheduling                        │
│                                                              │
│  Plugins:                                                    │
│    tars-memory — auto-recall, session state persistence       │
│    tars-team   — team context injection, roster management    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                    Service Layer (Docker Compose)             │
│                                                              │
│  ┌────────────┐ ┌────────────┐ ┌──────────────────────────┐ │
│  │ Auth Proxy │ │ Memory API │ │   Embedding Service      │ │
│  │   :9100    │ │   :8897    │ │       :8896              │ │
│  │            │ │            │ │                          │ │
│  │ Credential │ │ SQLite+FTS │ │ BGE-small-en-v1.5       │ │
│  │ injection  │ │ Session    │ │ 384-dim vectors          │ │
│  │ Ops proxy  │ │ state      │ │ ONNX Runtime             │ │
│  └────────────┘ └────────────┘ └──────────────────────────┘ │
│                                                              │
│  ┌────────────┐ ┌────────────┐ ┌──────────────────────────┐ │
│  │ Web Proxy  │ │ Dashboard  │ │   Cron Service           │ │
│  │   :8899    │ │ :8765/8766 │ │   (containerised)        │ │
│  │            │ │            │ │                          │ │
│  │ HTTP/S     │ │ Web UI     │ │ Memory lifecycle          │ │
│  │ outbound   │ │ Task mgmt  │ │ Backup · Extract          │ │
│  │ for agents │ │ Settings   │ │ Promote · Decay           │ │
│  └────────────┘ └────────────┘ └──────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ Credential Proxy (:8899)                                 ││
│  │ Forward HTTP proxy — intercepts outbound traffic and     ││
│  │ injects API keys in-flight for sandboxed containers      ││
│  └──────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌────────────┐ ┌────────────────────────────────────────── ┐│
│  │ MCP Gateway│ │ MCP DB (PostgreSQL)                       ││
│  │  :12008    │ │                                           ││
│  │  MetaMCP   │ │ Server config, namespaces, endpoints      ││
│  │  Aggregator│ │                                           ││
│  └────────────┘ └───────────────────────────────────────────┘│
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                   Agent Sandboxes (Docker)                    │
│                                                              │
│  Image: tars-sandbox:base                                    │
│  Base: node:20-slim + python3/pip + curl/git/jq + ffmpeg     │
│  Pre-installed: sympy, mpmath, mcporter (MCP tool access)    │
│  Agents install additional packages emergently via pip/npm   │
│                                                              │
│  Security:                                                   │
│  - Read-only root filesystem                                 │
│  - cap_drop: ALL (no Linux capabilities)                     │
│  - No Docker socket access                                   │
│  - Network only via web proxy (http_proxy env vars)          │
│  - No raw API keys — auth proxy injects credentials          │
│  - Resource limits: 2GB RAM, 2 CPUs per agent                │
│  - User: node (non-root)                                     │
│                                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐       │
│  │ T.A.R.S │  │ Agent 2 │  │ Agent 3 │  │ Agent N │       │
│  │ (coord) │  │ (spec)  │  │ (spec)  │  │         │       │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘       │
└─────────────────────────────────────────────────────────────┘
```

## Components

### OpenClaw Gateway

The gateway is the orchestration layer. It:
- Receives messages from Discord, Slack, Telegram, WhatsApp, Signal
- Routes them to the correct agent (via @mention in guilds)
- Manages agent sandbox lifecycle (start, stop, restart)
- Dispatches tool calls from agents
- Handles cron-triggered agent activations
- Manages session state and heartbeats
- Loads plugins (tars-memory, tars-team) for extended functionality

The gateway runs on the host (not containerized) as it needs Docker socket access to manage agent sandboxes.

### Plugins

#### tars-memory
- Replaces OpenClaw's built-in memory with the TARS memory API (`plugins.slots.memory: "tars-memory"`)
- Hooks `before_agent_start` to inject session state + relevant memories
- Hooks `agent_end` to auto-save session state
- Provides native memory tools (`memory_search`, `memory_store`, `session_state_save`, etc.)
- Config: memory API URL, auto-recall toggle, max recall results

#### tars-team
- Hooks `before_prompt_build` to inject `<user-context>` and `<team>` blocks
- Resolves sender by Discord ID against `config/team.json`
- Provides team management tools (`team_list`, `team_get`, `team_add`, `team_update`, `team_remove`, `team_sync`)
- Auto-syncs Discord allowlists in `openclaw.json` when team changes

### Auth Proxy (:9100)

Central credential manager. All outbound API calls from agents go through the auth proxy, which:
- Injects the correct API key/token based on the route
- Supports Bearer tokens, query parameters, and custom headers
- Auto-refreshes OAuth2 tokens (Google)
- Logs all API usage per agent
- Hosts ops proxy endpoints for self-service infrastructure

**Key principle:** Raw API keys never enter agent sandboxes. All secrets are stored in an age-encrypted vault. The auth proxy is the only component that reads secrets.

Routes are configured per integration:
```
/anthropic/  → api.anthropic.com    (Bearer token)
/discord/    → discord.com/api/v10  (Bot token)
/tavily/     → api.tavily.com       (Bearer token)
/github/     → api.github.com       (Bearer token)
...
```

### Memory API (:8897)

Persistent memory system that gives agents continuity across sessions.

**Storage:** SQLite with FTS5 full-text search indices.

**Features:**
- Semantic search (via embedding service)
- Session state persistence (channel-scoped)
- Entity and relationship extraction
- Confidence-based lifecycle (decay → archive → purge)
- Agent-scoped memories (each agent has its own namespace)
- Shared memory scope for cross-agent knowledge
- Auto-extraction from session logs

**Memory types:** semantic, episodic, procedural
**Categories:** system, project, episodic, user, business, people, infrastructure, procedural, session, agent

**Lifecycle:**
- Memories decay if not accessed for 7+ days
- Low-confidence memories get archived
- Archived memories purge after 30 days
- Pinned memories never decay

### Embedding Service (:8896)

Local embedding generation using BGE-small-en-v1.5 via ONNX Runtime.

- 384-dimensional vectors
- Used for semantic search and deduplication
- No external API calls — runs entirely locally
- ~50ms per embedding on typical hardware

### Web Proxy (:8899)

HTTP/HTTPS proxy for agent sandboxes. Agents can't make direct outbound connections — all web traffic routes through this proxy. The `http_proxy` and `https_proxy` environment variables are set automatically in every sandbox container.

### Credential Proxy

Forward HTTP proxy that intercepts outbound traffic from sandbox containers and injects API keys in-flight. Works alongside the auth proxy (reverse proxy) to ensure credentials never enter agent containers.

### MCP Gateway (:12008)

MetaMCP-based MCP server aggregator. Centralises access to external tool integrations via the Model Context Protocol (MCP).

**Architecture:**
- MetaMCP runs as a Docker service with a PostgreSQL backing store
- MCP servers (e.g., Google Workspace) run as child processes inside the MetaMCP container
- Agents access MCP tools via the `mcporter` CLI (pre-installed in sandbox image)
- Credentials are encrypted in the age vault, decrypted into tmpfs (RAM-only) at startup via `scripts/inject-mcp-creds.sh`, and mounted into the container — never on disk, never visible to agents

**How agents use it:**
```bash
# List all available MCP tools
mcporter list

# Call a specific tool
mcporter call google-workspace google-workspace__gmail_search query="invoices"
mcporter call google-workspace google-workspace__calendar_today
mcporter call google-workspace google-workspace__drive_list_files query="budget 2026"
```

**Configured MCP servers:**
- **Google Workspace** (`@pegasusheavy/google-mcp`) — 128 tools covering Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, Meet, Chat, Contacts, Notes, Tasks, YouTube

**Adding new MCP servers:**
1. Access MetaMCP UI via SSH tunnel: `ssh -L 12008:172.17.0.1:12008 <host>`
2. Open `http://localhost:12008`
3. Add a new server with its command, args, and environment variables
4. Assign it to the `default` namespace

**Security model:** Same as auth proxy — credentials are encrypted in the vault, decrypted into tmpfs (RAM-only) at service startup, and mounted read-only into the MetaMCP container. Agents only see the tool interface via mcporter. No raw credentials reach agent sandboxes, and no plaintext credentials exist on disk.

### Dashboard (:8765/:8766)

Web interface for platform management:
- Task queue and status
- Service health monitoring
- Agent management (create, configure, restart)
- System resource usage

Bound to `127.0.0.1` by default. If Tailscale is configured during setup, bound to `0.0.0.0` (accessible via Tailscale IP only — not public internet).

### Cron Service

A containerised cron service that maintains the memory system. Runs as a Docker Compose service with access to the memory database volume.

Jobs:
- **Memory context regeneration** (every 30 min): rebuild MEMORY_CONTEXT.md from memory API
- **Memory lifecycle** (every 6h): confidence decay, archival, purge
- **Memory backup** (every 6h): SQLite snapshot
- **Memory extraction** (every 30 min): extract facts from session logs
- **Memory promotion** (every 12h): promote high-confidence memories

An additional host-side cron job runs `scripts/regen-memory-context.sh` to regenerate the main agent's memory context file.

## Agent Model

### Sandbox Image

All agents run in Docker containers using the `tars-sandbox:base` image, built from `templates/Dockerfile.sandbox` during setup.

**Pre-installed:**
- Node.js 20, Python 3, pip, git, curl, jq, ffmpeg
- sympy, mpmath (symbolic math)
- mcporter (MCP tool access via gateway)

**Emergent package installation:** Agents can install additional packages at runtime using `pip install --user` or `npm install`. Packages installed to `/workspace/.local/` persist across sessions in the workspace volume. The `PYTHONUSERBASE` environment variable is pre-configured.

This keeps the base image small (~350MB) while letting agents self-serve any tools they need. If an agent needs `requests`, `beautifulsoup4`, or any other package, it installs it once and it's available from then on.

### Topology

Agents follow an emergent topology — no fixed org chart. See [MULTI-AGENT-SPEC.md](MULTI-AGENT-SPEC.md).

- **Day 1:** T.A.R.S is the sole coordinator
- **As needs emerge:** Specialist agents are added via `scripts/add-agent.sh`
- **Structure adapts:** Hub-and-spoke, mesh, or hybrid — whatever the work requires

### Roles

| Role | Description |
|------|-------------|
| **Coordinator** | Routes tasks, delegates to specialists, reports to humans. T.A.R.S is the default coordinator. |
| **Specialist** | Deep expertise in one domain. Called by coordinators or directly by humans. |
| **Assistant** | General-purpose, handles tasks that don't need a specialist. |

### Agent Workspace

Each agent has a workspace mounted into its sandbox container:

```
~/.openclaw/workspaces/<agent-id>/
  SOUL.md          — personality, values, boundaries
  IDENTITY.md      — id, name, role, domain
  AGENTS.md        — operating rules (session startup, memory, credentials)
  TOOLS.md         — available services and endpoints
  MEMORY.md        — memory system reference
  .mcporter/       — mcporter config (MCP gateway connection)
  .local/          — pip/npm packages installed by the agent (persists)
```

The main agent (T.A.R.S) workspace is created at `~/.openclaw/workspace/` during initial setup. Additional agents get `~/.openclaw/workspaces/<id>/`.

### Agent Lifecycle

**Creation:** Owner tells T.A.R.S to create an agent → T.A.R.S collects details conversationally → runs `scripts/add-agent.sh` → agent is live in a sandbox. See [agent-management skill](../skills/agent-management.md).

**Destruction:** Owner tells T.A.R.S to remove an agent → T.A.R.S confirms → runs `scripts/remove-agent.sh` → workspace archived (not deleted).

All agents inherit the sandbox configuration from `agents.defaults.sandbox` in `openclaw.json`. New agents are automatically sandboxed — there is no way to create an unsandboxed agent through the standard tooling.

## Team System

A single registry at `config/team.json` contains all humans and agents. See [TEAM-SPEC.md](TEAM-SPEC.md).

- **Humans:** name, role, responsibilities, contact methods, access level (owner/admin)
- **Agents:** name, role, domain, capabilities, home channel

The tars-team plugin injects team context into every agent prompt (~300 tokens), so agents always know who they're talking to and who else is on the team.

**Team management** is conversational through T.A.R.S — owner-only. See [team-management skill](../skills/team-management.md).

## Skills

Skills are instruction documents that tell agents how to handle specific workflows. Registered in `skills/registry.json`.

| Skill | Category | Description |
|-------|----------|-------------|
| `team-management` | system | Add, update, remove team members |
| `agent-management` | system | Create and destroy persistent agents |
| + others | various | See `skills/registry.json` for full list |

## Security

### Credential Management

All secrets are stored in an age-encrypted vault (`$TARS_HOME/.secrets-vault/`). Individual encrypted files also exist in `$TARS_HOME/.secrets/`. The vault resolver script decrypts on demand for the auth proxy.

**No plaintext secrets in config files or environment variables reach agent containers.**

### Agent Isolation (Enforced)

Every agent runs in a sandboxed Docker container with:

| Measure | Implementation |
|---------|---------------|
| **Sandbox mode** | `agents.defaults.sandbox.mode: "all"` — all tool execution is sandboxed |
| **Read-only root** | Agents cannot modify system files |
| **Capability drop** | `cap_drop: [ALL]` — no Linux capabilities |
| **Non-root user** | Runs as `node` user inside container |
| **Network isolation** | All traffic forced through web proxy via `http_proxy`/`https_proxy` env vars |
| **Credential isolation** | No access to `.secrets/`, vault, or age keys — auth proxy injects credentials at the network layer |
| **Resource limits** | 2GB RAM, 2 CPUs per agent container |
| **Workspace isolation** | Each agent gets its own workspace volume — cannot access other agents' workspaces |
| **Memory isolation** | Agent-scoped memory — agents read their own scope by default, shared scope explicitly |

### Access Levels

| Level | Capabilities |
|-------|-------------|
| **Owner** | All agents, all tools, exec auto-approved, config changes, team management |
| **Admin** | All agents, all tools, exec with approval, no config changes |

## Networking

All services bind to the Docker bridge network (`172.17.0.1`), not public interfaces. Agent sandboxes access services via bridge IP + port.

```
Agent Sandbox → http_proxy (172.17.0.1:8899) → Web Proxy → Internet
Agent Sandbox → 172.17.0.1:9100               → Auth Proxy → External APIs
Agent Sandbox → 172.17.0.1:8897               → Memory API
Agent Sandbox → mcporter → 172.17.0.1:12008   → MCP Gateway → MCP Servers → External APIs
```

The dashboard binds to `127.0.0.1` by default. With Tailscale, it binds to `0.0.0.0` but is only reachable via the Tailscale network.

## Configuration

The setup wizard (`setup.sh`) generates all configuration:
- `~/.openclaw/openclaw.json` — Gateway config (agents, channels, tools, plugins, secrets, sandbox)
- `~/.openclaw/exec-approvals.json` — Per-agent exec permissions
- `config/team.json` — Team registry (humans + agents)
- `.env` — Docker service ports and paths (no secrets)
- `.secrets-vault/` — Age-encrypted credential vault
- `tars-sandbox:base` — Docker image for agent sandboxes

Post-install changes are handled through:
- `scripts/update.sh` — Pull repo changes, rebuild if needed, restart
- `scripts/add-agent.sh` — Add a new persistent agent (automatically sandboxed)
- `scripts/remove-agent.sh` — Remove an agent (archive workspace)
- T.A.R.S conversational team/agent management via skills

## Updates

`scripts/update.sh` handles in-place updates: git pull, plugin dependency install, conditional Docker rebuild (including sandbox image if `Dockerfile.sandbox` changed), gateway restart, health checks. Supports `--no-rebuild` and `--dry-run`.
