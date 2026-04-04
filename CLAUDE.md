# T.A.R.S

## What Is This?

T.A.R.S is a lightweight, LLM-agnostic agent system that connects messaging apps (Discord, with more on the roadmap) to persistent LLM sessions running inside project contexts. No OpenClaw dependency.

## Design Philosophy

- **Lightweight over feature-rich** — minimal dependencies, minimal config, minimal moving parts
- **Zero maintenance** — SQLite for everything, no external DBs, no Docker required, no cron jobs to manage
- **LLM agnostic** — Claude API, Groq, Ollama, any OpenAI-compatible endpoint
- **Project-scoped sessions** — each agent runs inside a project directory with relevant files as context
- **Easy to extend** — adding tools, APIs, skills = adding a decorated Python function
- **Simple security** — encrypted credential vault (Fernet), no proxy chains, no containers needed
- **Inter-agent communication** — agents can message each other through a simple message bus
- **Easy migration** — TARS/OpenClaw users can migrate with one command

## Architecture — Everything Is a Module

Single async Python process. Everything is a pluggable module with auto-discovery.
Drop a file in the right folder → reference it in config → it works.

```
Registry (auto-discovers all modules)
    │
    ├── Connectors   src/connectors/    discord, telegram, slack, http...
    ├── LLM          src/llm/           claude, groq, ollama, openai...
    ├── Memory       src/memory/        sqlite, postgres, redis...
    ├── Vault        src/vault/         fernet (default)
    ├── Tools        src/tools/         @tool decorated functions
    ├── Skills       skills/            YAML (prompt + tool list)
    ├── MCP          config/mcp.yaml    auto-surfaces as native tools
    └── APIs         src/apis/          inbound webhooks
```

An agent is a project folder + config that picks modules. See [ARCHITECTURE.md](ARCHITECTURE.md) for full system design.

## Key Directories

```
src/
  core/          — registry, agent manager, router, middleware, base interfaces
  connectors/    — one file per platform (discord.py, telegram.py, etc.)
  llm/           — one file per provider (claude_code.py, etc.)
  memory/        — one file per backend (sqlite.py, etc.)
  vault/         — credential encryption (fernet.py)
  tools/         — @tool decorated functions, auto-discovered
  auth/          — OAuth2 token refresh, etc.
  apis/          — @api decorated inbound webhooks
config/          — YAML: config.yaml, agents.yaml, mcp.yaml
agents/          — one folder per agent (SOUL.md + workspace + local data)
skills/          — YAML skill definitions (prompt + tool list)
data/            — SQLite databases, audit logs, state (gitignored)
```

## Tech Stack

- **Python 3.12+** with asyncio
- **SQLite** (WAL mode) for all persistence
- **Claude Code CLI** for primary LLM engine (Max subscription)
- Direct HTTP client for alternative LLM providers (roadmap — any OpenAI-compatible endpoint)
- **discord.py** for Discord
# - **python-telegram-bot** for Telegram (roadmap)
- **cryptography** (Fernet) for credential vault
# - **FastAPI** for HTTP API / webhooks (roadmap)

## Development

```bash
uv sync                    # install deps
uv run python -m src.main  # run locally
```

## Deployment

When updating a running install (`git pull` + restart), follow the ritual in [ARCHITECTURE.md → Operations → Updating a Running Install](ARCHITECTURE.md#updating-a-running-install). The `git pull` → `systemctl restart` shortcut is **wrong** — service units use `uv run --no-sync`, so you must run `sudo -u tars uv sync` between the pull and the restart or the service will crash on a new dep or silently run stale code.

## File Ownership

All files under the install directory (typically `/opt/tars-v2/`) are owned by `tars:tars` (the service user). The main service and timers run as `tars`, so root-owned files inside the tree are latent breakage — readable but not writable/deletable by the service, and `uv sync` / `uv run` will fail on any root-owned file in `.venv/`.

**When editing files in this repo as root** (e.g. from a Claude Code session running as root), `chown tars:tars <file>` back after every save. Most editors — including the `Edit`/`Write` tools — rewrite files and the new file inherits the editing user's ownership, not the original's. After a batch of edits, verify with:

```bash
find /opt/tars-v2 -not -user tars 2>/dev/null
```

Zero output = clean. Any output = run `sudo chown -R tars:tars <paths>` on the listed files.

## Key Conventions

- **Modules are files** — drop a .py in the right folder, it's available. No imports to add, no registry to update.
- **Agents are config** — an agent is a YAML block that picks: an LLM, a memory backend, tools, skills, routing.
- **Tools are decorated functions** — `@tool` on an async function. Schema auto-generated from type hints.
- **Skills are YAML** — a prompt + a list of tools. No code needed.
- **MCP auto-surfaces** — connect an MCP server, its tools appear as native tools. LLM doesn't know the difference.
- **Secrets in vault** — encrypted at rest, passphrase entered at startup, never in env vars or config files.
- **No ORMs** — raw SQL with simple helper functions
- **Type hints everywhere**, minimal abstractions

## Docs

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — Full system architecture and operations reference
- **[MIGRATION.md](MIGRATION.md)** — Migration guide from OpenClaw to T.A.R.S
- **[ROADMAP.md](ROADMAP.md)** — Feature roadmap
- **[SCRIPTS.md](SCRIPTS.md)** — All scripts with usage
- **[skills/README.md](skills/README.md)** — Skill format reference

## Migration from legacy TARS

T.A.R.S replaces the OpenClaw dependency. See [MIGRATION.md](MIGRATION.md) for the generic migration guide.

**Design rule**: if a feature can't be migrated easily, redesign the feature, not the migration.

## Lessons from Prior Projects

Learned from building TARS (full platform) and Claude Commander (minimal bot):
- TARS: great memory system (SQLite+FTS5+embeddings) but too many services (9 Docker containers, 26 scripts)
- Commander: great simplicity (3 files) but no memory, no multi-agent, single-user only
- T.A.R.S targets the sweet spot: persistent memory + multi-agent + multi-connector in one process
