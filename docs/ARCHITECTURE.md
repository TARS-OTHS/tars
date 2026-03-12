# Architecture

## Overview

TARS is a layered platform for running AI agents with persistent memory, secure credential handling, and Docker-based isolation.

```
┌─────────────────────────────────────────────────────────────┐
│                      User Layer                              │
│  Discord · Signal · Telegram                                 │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                   OpenClaw Gateway                           │
│  Message routing · Agent lifecycle · Tool dispatch            │
│  Session management · Cron scheduling                        │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                    Service Layer                              │
│                                                              │
│  ┌────────────┐ ┌────────────┐ ┌──────────────────────────┐ │
│  │ Auth Proxy │ │ Memory DB  │ │   Embedding Service      │ │
│  │   :9100    │ │   :8897    │ │       :8896              │ │
│  │            │ │            │ │                          │ │
│  │ Credential │ │ SQLite+FTS │ │ BGE-small-en-v1.5       │ │
│  │ injection  │ │ Session    │ │ 384-dim vectors          │ │
│  │ Route mgmt │ │ state      │ │ ONNX Runtime             │ │
│  │ Ops proxy  │ │ Entities   │ │                          │ │
│  └────────────┘ └────────────┘ └──────────────────────────┘ │
│                                                              │
│  ┌────────────┐ ┌────────────┐ ┌──────────────────────────┐ │
│  │ Web Proxy  │ │ Dashboard  │ │   Cron Engine            │ │
│  │   :8899    │ │ :8765/8766 │ │   (host crontab)         │ │
│  │            │ │            │ │                          │ │
│  │ HTTP/S     │ │ Web UI     │ │ Memory lifecycle          │ │
│  │ outbound   │ │ Task mgmt  │ │ Backup · Extract          │ │
│  │ for agents │ │ Settings   │ │ Promote · Decay           │ │
│  └────────────┘ └────────────┘ └──────────────────────────┘ │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                   Agent Sandboxes                            │
│                                                              │
│  Each agent runs in an isolated Docker container:            │
│  - Own workspace (mounted volume)                            │
│  - Own identity (SOUL.md, AGENTS.md, TOOLS.md)               │
│  - Resource limits (memory, CPU)                             │
│  - Capability drops (cap_drop: ALL)                          │
│  - Read-only root filesystem                                 │
│  - Network access only through web proxy                     │
│  - API access only through auth proxy                        │
│                                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐       │
│  │ Agent 1 │  │ Agent 2 │  │ Agent 3 │  │ Agent N │       │
│  │ (main)  │  │         │  │         │  │         │       │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘       │
└─────────────────────────────────────────────────────────────┘
```

## Components

### OpenClaw Gateway

The gateway is the orchestration layer. It:
- Receives messages from Discord, Signal, Telegram
- Routes them to the correct agent
- Manages agent container lifecycle (start, stop, restart)
- Dispatches tool calls from agents
- Handles cron-triggered agent activations
- Manages session state and heartbeats

The gateway runs on the host (not containerized) as it needs Docker socket access to manage agent containers.

### Auth Proxy (:9100)

Central credential manager. All outbound API calls from agents go through the auth proxy, which:
- Injects the correct API key/token based on the route
- Supports Bearer tokens, query parameters, and custom headers
- Auto-refreshes OAuth2 tokens (Google)
- Logs all API usage per agent
- Hosts ops proxy endpoints for self-service infrastructure

**Key principle:** Raw API keys never enter agent containers. The auth proxy is the only component that reads `.secrets/`.

Routes are configured per integration:
```
/anthropic/  → api.anthropic.com    (Bearer token)
/google/     → www.googleapis.com   (OAuth2, auto-refresh)
/discord/    → discord.com/api/v10  (Bot token)
/tavily/     → api.tavily.com       (Bearer token)
/github/     → api.github.com       (Bearer token)
...
```

### Memory DB (:8897)

Persistent memory system that gives agents continuity across sessions.

**Storage:** SQLite with FTS5 full-text search indices.

**Features:**
- Semantic search (via embedding service)
- Session state persistence
- Entity and relationship extraction
- Confidence-based lifecycle (decay → archive → purge)
- Agent-scoped memories (each agent has its own namespace)
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

HTTP/HTTPS proxy for agent containers. Agents can't make direct outbound connections — all web traffic routes through this proxy.

### Dashboard (:8765/:8766)

Web interface for platform management:
- Task queue and status
- Service health monitoring
- Credential management (add/update/test integrations)
- Agent management (create, configure, restart)
- Security audit results
- System resource usage

### Cron Engine

Host-level crontab entries that maintain the memory system:
- **Memory lifecycle** (every 6h): confidence decay, archival, purge
- **Memory backup** (every 6h): SQLite snapshot
- **Memory extraction** (every 10/40 min): extract facts from session logs
- **Session state** (every 15 min): auto-capture agent state as safety net
- **Context regeneration** (every 30 min): rebuild context files from memory
- **Memory promotion** (every 12h): promote high-confidence memories to context files

## Networking

All services bind to the Docker bridge network (`172.17.0.1`), not public interfaces. Agent containers access services via bridge IP + port.

```
Agent Container → 172.17.0.1:9100  → Auth Proxy → External APIs
Agent Container → 172.17.0.1:8897  → Memory DB
Agent Container → 172.17.0.1:8899  → Web Proxy → Internet
```

The dashboard binds to `0.0.0.0:8765` by default but should be access-controlled (Tailscale, firewall rules, or reverse proxy with auth).

## Configuration

`platform.yaml` is the single source of truth. The setup wizard generates it, and it drives:
- Which services are enabled
- Port assignments
- Agent definitions
- Integration routes
- Backup schedules
- Security settings

See the [spec](../docs/specs/PLATFORM_SPEC.md) for the full schema.

## Agent Isolation Model

Each agent is a peer — not hierarchical. Isolation is enforced at the Docker level:

- **Filesystem:** Own workspace volume, read-only root
- **Network:** No direct internet; web proxy and auth proxy only
- **Resources:** Memory and CPU limits per container
- **Credentials:** No access to raw API keys
- **Messaging:** Own Discord server (server-per-agent is the only reliable message isolation with OpenClaw)
- **Memory:** Agent-scoped — agents can't read each other's memories

Agents coordinate through shared channels (like this TARS project channel) when needed, but can't access each other's workspaces or data.
