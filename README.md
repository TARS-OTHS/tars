# T.A.R.S. — Trusted Agent Runtime Stack

A self-contained AI agent platform built on [OpenClaw](https://github.com/openclaw/openclaw). Deploy a fully functional agent infrastructure — memory, security, multi-agent sandboxing, credential management — on any Linux machine in under 30 minutes.

```bash
git clone https://github.com/TARS-OTHS/tars.git
cd tars
./setup.sh
docker compose up -d
```

## What You Get

- **Persistent Memory** — SQLite + FTS5 with semantic embeddings. Your agents remember across sessions.
- **Secure Credential Injection** — API keys never reach agent containers. Auth proxy injects them on the wire.
- **Multi-Agent Sandboxing** — Each agent runs in its own Docker container with capability drops, resource limits, and read-only root.
- **Self-Service Ops** — Agents can expose ports, manage crons, create subdomains — all through a controlled proxy.
- **Web Dashboard** — Task management, service health, credential management, security audits.
- **Skill System** — Modular capabilities (web search, calendar, coding, weather) that agents can use.
- **Setup Wizard** — Interactive CLI that asks the right questions and generates all config.

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  TARS Stack                       │
│                                                  │
│  OpenClaw Gateway    Dashboard    Setup Wizard   │
│        │                │                        │
│  ┌─────┴────────────────┴──────────────────────┐ │
│  │            Service Layer                     │ │
│  │  Auth Proxy · Memory DB · Embeddings         │ │
│  │  Web Proxy  · Ops Proxy · Cron Engine        │ │
│  └──────────────────────────────────────────────┘ │
│                                                  │
│  ┌──────────────────────────────────────────────┐ │
│  │         Agent Sandboxes (Docker)              │ │
│  │   Agent 1 (main)  ·  Agent 2  ·  Agent N    │ │
│  └──────────────────────────────────────────────┘ │
│                                                  │
│  ┌──────────────────────────────────────────────┐ │
│  │         Optional Services                     │ │
│  │   Joplin · Tailscale · Cloudflare Tunnel     │ │
│  └──────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

## Core Services

| Service | Port | Description |
|---------|------|-------------|
| **Auth Proxy** | 9100 | Credential injection for all outbound API calls |
| **Memory DB** | 8897 | Persistent memory with FTS5 + semantic search |
| **Embedding Service** | 8896 | Local BGE-small-en-v1.5 (ONNX, 384-dim) |
| **Web Proxy** | 8899 | Outbound HTTP/S proxy for sandboxed containers |
| **Ops Proxy** | 9100 | Self-service infrastructure (ports, crons, agents, routes) |
| **Dashboard** | 8765 | Web UI for management and monitoring |

## Quick Start

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for the full guide.

**Prerequisites:**
- Ubuntu 22.04+ (or Debian 12+)
- Docker + Docker Compose
- Node.js 20+
- 4GB+ RAM, 20GB+ disk

**Setup:**
```bash
./setup.sh
```

The wizard walks you through:
1. Environment checks
2. Purpose and agent identity
3. Messaging platform (Discord, Signal, Telegram)
4. API integrations
5. Skill selection
6. Config generation and deployment

## Adding Agents

```bash
./scripts/add-agent.sh
```

Each agent gets its own Docker sandbox, workspace, and identity. Agents are peers — each owns its domain, communicates on its own channels.

## Skills

Skills are modular capability packages. Install during setup or add later.

| Category | Skills |
|----------|--------|
| **Productivity** | daily-planning, daily-review, whats-next, book-event |
| **Research** | tavily-search, serpapi-search, analyze-paper |
| **Development** | coding-agent, healthcheck, tmux |
| **Utility** | weather, video-frames, skill-creator |
| **Health** | health-query, protocol |

## Security

Applied automatically on every deployment:
- Cloud metadata endpoint blocked from containers
- All capabilities dropped (`cap_drop: ALL`)
- Read-only root filesystem on agent containers
- Resource limits (memory + CPU) per container
- Credentials never enter agent containers
- Network segmentation (services on Docker bridge only)
- Dashboard local-only by default (Tailscale-gated if enabled)

Run `./scripts/security-audit.sh` to verify.

## Backup & Restore

```bash
# Backup everything (config, secrets, memory, workspaces)
./scripts/backup.sh

# Restore on same or new machine
./scripts/restore.sh backup-2026-03-10.tar.gz
```

## Documentation

- [Quick Start](docs/QUICKSTART.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Credential Setup](docs/CREDENTIALS.md)
- [Agent Management](docs/AGENTS.md)
- [Skill Development](docs/SKILLS.md)
- [Security Model](docs/SECURITY.md)
- [Backup & Restore](docs/BACKUP.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Upgrading](docs/UPGRADING.md)

## License

TBD

## Built With

- [OpenClaw](https://github.com/openclaw/openclaw) — Agent gateway and lifecycle management
- SQLite + FTS5 — Persistent memory
- BGE-small-en-v1.5 — Local embeddings (ONNX Runtime)
- Docker — Agent sandboxing and service isolation
