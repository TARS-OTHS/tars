# T.A.R.S — The Agent Routing System

A lightweight, single-process agent framework that connects messaging platforms to persistent LLM sessions with tools, memory, and multi-agent coordination.

```
Discord message → Router → Agent Manager → Claude Code CLI → MCP Tools → Response
```

## What It Does

- **Single process** — no Docker, no microservices, no infrastructure to manage
- **Multi-agent** — multiple bots with different personalities, tools, and permissions
- **Multi-bot** — each agent gets its own Discord bot identity
- **Persistent memory** — SQLite with FTS5 full-text search + semantic embeddings
- **Drop-in tools** — add a `@tool` decorated Python function, it's instantly available
- **Drop-in skills** — YAML prompt templates become Discord slash commands automatically
- **Human-in-the-loop** — sensitive tools require approval via Discord reactions
- **Encrypted vault** — Fernet-encrypted credentials, never in env vars or config files
- **Category routing** — route agents to Discord categories, not just channels
- **Per-agent tool access** — allowlists, denylists, and built-in tool blocking per agent
- **Media pipeline** — image/video analysis (Gemini), audio transcription (Groq)
- **Headless browser** — `browse_url` tool renders JS-heavy pages via Playwright + Chromium
- **Hot reload** — tools and skills update without restarting

## Quick Start

```bash
git clone https://github.com/TARS-OTHS/tars.git
cd tars
uv sync --extra analytics
uv run python setup.py
```

The setup wizard walks you through: vault creation, Discord bot connection, team setup, first agent configuration, HITL approval settings, and (optionally) downloading headless Chromium for the `browse_url` tool.

> **Note on extras:** `--extra analytics` installs DuckDB for the ETL pipeline. Omit it if you don't need analytics/ETL.

> **Note on the browser tool:** `uv sync` installs the Playwright Python package, but the Chromium binary (~170MB) is a separate download. The setup wizard offers to run `playwright install chromium` for you. To install it manually later:
> ```bash
> uv run playwright install chromium
> ```

Then start T.A.R.S:

```bash
uv run python -m src.main
```

### What You Need

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Claude Max subscription (for Claude Code CLI)
- Discord bot token (free — [create one here](https://discord.com/developers/applications))
- Linux VPS (2 CPU, 4GB RAM is plenty)

### Install Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

### Running as a Service

```bash
# Copy the systemd template
sudo cp config/tars.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tars.service

# View logs
journalctl -u tars -f
```

### Running with Profiles

```bash
# Default profile
uv run python -m src.main

# Named profile (loads config.<profile>.yaml and agents.<profile>.yaml)
uv run python -m src.main --profile test
```

## Adding Tools

```python
# src/tools/my_tool.py
from src.core.base import ToolContext
from src.core.tools import tool

@tool(name="check_weather", description="Get weather for a city")
async def check_weather(ctx: ToolContext, city: str) -> str:
    # ctx.vault gives you encrypted API keys
    api_key = ctx.vault.get("weather-api-key")
    return f"Weather in {city}: sunny, 25C"
```

Drop it in `src/tools/`, it's auto-discovered and available to all agents via MCP. No imports to add, no registry to update.

## Adding Skills

```yaml
# skills/my_skill.yaml
name: daily_report
description: Generate a daily business report
parameters:
  - name: focus
    type: string
    choices: [sales, marketing, operations]
prompt: |
  Generate a {focus} report for today. Check recent data and summarize key metrics.
tools:
  - web_search
  - memory_search
```

Skills become Discord slash commands automatically. See [skills/README.md](skills/README.md) for the full format reference.

## Adding Business Knowledge (Codex)

The `codex/` directory holds stable business knowledge that agents can't get from an API — brand voice, company profile, supplier contacts, processes, strategy docs.

```
codex/
├── _index.md          <- Master index (agents read this first)
├── business/          <- Brand voice, compliance
├── products/          <- Product info, guidelines
├── strategy/          <- Playbooks, competitor analysis
└── processes/         <- SOPs, workflows
```

Agents reference the codex via their CLAUDE.md. The `_index.md` tells agents what's in the codex vs. what to query from tools, so they don't use stale docs when live data is available.

See [codex/README.md](codex/README.md) for the full guide.

## Included Tools

| Category | File | Tools | Requires |
|----------|------|-------|----------|
| **Memory** | memory.py | store, search, semantic_search, forget | Built-in |
| **Team** | team.py | list, get, add, update, remove | Built-in |
| **Web** | web_search.py | search | Tavily API key |
| **Discord** | discord_tools.py | read_channel, read_message, search, send_file | Discord bot token |
| **Google Workspace** | google.py | Gmail, Calendar, Drive (13 tools) | Google OAuth2 credentials |
| **Trello** | trello.py | boards, lists, cards, create_card, activity | Trello API key + token |
| **Media** | gemini.py, audio.py, video.py | image/video analysis, transcription | Gemini + Groq API keys |
| **Cloudflare** | cloudflare.py | zones, dns_list, dns_update | Cloudflare API token |
| **Notion** | notion.py | search, read, create | Notion API key |
| **Browser** | ingest.py | read_url, browse_url (headless Chromium via Playwright) | `playwright install chromium` |
| **System** | ingest.py, tmux.py, builtin.py | skill creation, tmux, inter-agent messaging | Built-in |

Remove a file = remove those tools. Add your own integrations (Shopify, Stripe, GitHub, Slack, etc.) by dropping a `@tool` decorated Python file into `src/tools/`.

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  T.A.R.S Process                  │
│                                                   │
│  Connector (Discord — Telegram, Slack on roadmap) │
│       → Router (channel/category → agent)         │
│       → Access Control (3-layer)                  │
│       → Agent Manager (context, memory, sessions) │
│       → Claude Code CLI (LLM engine)              │
│       → MCP Server (tools + middleware)            │
│           ├── Rate limit                          │
│           ├── HITL gate                           │
│           ├── Execute tool                        │
│           └── Audit log                           │
│                                                   │
│  Vault (Fernet) │ Memory (SQLite) │ Hot Reload    │
└──────────────────────────────────────────────────┘
```

Everything is a pluggable module with auto-discovery. Drop a file in the right folder, reference it in config, it works.

## Multi-Agent Setup

Each agent gets its own bot identity, tool access, and channel routing:

```yaml
# config/agents.yaml
agents:
  main:
    display_name: "My Agent"
    llm:
      provider: claude_code
      model: opus
    tools: all                    # Full MCP tool access
    disallow_builtins:            # Block file editing and shell access
      - Edit
      - Write
      - Bash
      - MultiEdit
    routing:
      discord:
        account: main             # Uses the 'main' bot
        channels: []              # All channels (wildcard)
        mentions: true

  assistant:
    display_name: "Helper"
    llm:
      provider: claude_code
      model: sonnet
      timeout: 1800
    tools:                        # Restricted tool list
      - memory_search
      - web_search
      - trello_boards
      - trello_cards
    routing:
      discord:
        account: helper           # Separate bot identity
        categories: ["123456"]    # Only responds in this category
        mentions: true
```

Agents with specific channel/category routing take priority over wildcard agents.

## Access Control

Three-layer permission system:

| Layer | Controls | Configured in |
|-------|----------|---------------|
| **Can they talk?** | Sender tier x agent tier — who can message which agent | `config/team.json` |
| **What tools?** | Per-sender tool restrictions computed per message | `config.yaml` |
| **Agent ceiling** | Static per-agent tool allowlist/denylist | `agents.yaml` |

**People tiers:** owner (full access) → admin (safe tools + HITL) → staff (assistant only) → unknown (blocked)

## LLM Provider

T.A.R.S uses **Claude Code CLI** as its LLM engine via a Claude Max subscription (no per-token API costs). Each agent spawns a Claude Code subprocess with:

- Full tool access via MCP (Model Context Protocol)
- Session resume across conversations
- Per-agent model selection (opus, sonnet, haiku)
- Per-agent configurable timeout

Alternative LLM providers (OpenAI-compatible endpoints, Ollama, Groq) are on the roadmap.

## Memory System

SQLite-based persistent memory with dual search:

- **FTS5** — fast keyword search across all memories
- **Semantic embeddings** — BGE-small-en-v1.5 vectors (384-dim, ONNX) for conceptual similarity
- **Agent-scoped** — each agent's memories are isolated; global and group scopes available
- **Auto-recall** — relevant memories injected at session start
- **Decay** — unused memories fade over time; pinned memories are permanent

## Configuration

The setup wizard (`uv run python setup.py`) generates all config files interactively. Or copy the examples:

```bash
cp config/config.yaml.example config/config.yaml
cp config/agents.yaml.example config/agents.yaml
cp config/team.json.example config/team.json
```

All config files are gitignored — your deployment details stay private.

## Operations

```bash
# Run
uv run python -m src.main

# Run with profile
uv run python -m src.main --profile test

# Setup wizard
uv run python setup.py

# Vault management
uv run python vault-manage.py

# Run tool tests
uv run python scripts/test-tools.py
```

## Security

- **Fernet vault** — AES-128-CBC encrypted at rest, PBKDF2 key derivation (100k iterations)
- **HITL gates** — configurable per-tool, Discord reaction approval with timeout
- **Three-layer access control** — sender tier x agent tier, per-message tool filtering, static agent ceiling
- **Rate limiting** — per-tool per-agent sliding window
- **Audit log** — every tool call, HITL decision, auth event (JSONL)
- **Bot loop detection** — sliding window prevents runaway agent-to-agent ping-pong
- **Message dedup** — same content to same channel within 120s is dropped
- **Agent-scoped memory** — agents only see their own memories + shared scope

## Migrating from OpenClaw

See [MIGRATION.md](MIGRATION.md) for the full migration guide. T.A.R.S replaces the OpenClaw gateway — same agents, same memories, same channels, zero data migration.

```bash
# Quick version
systemctl stop openclaw-gateway.service
uv run python -m src.main
```

## Docs

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full system architecture and operations reference |
| [MIGRATION.md](MIGRATION.md) | Migration guide from OpenClaw |
| [ROADMAP.md](ROADMAP.md) | Feature roadmap |
| [SCRIPTS.md](SCRIPTS.md) | All scripts with usage |
| [skills/README.md](skills/README.md) | Skill format reference |
| [codex/README.md](codex/README.md) | Business knowledge guide |

## License

MIT
