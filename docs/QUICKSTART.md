# Quick Start Guide

Get a working TARS deployment in under 30 minutes.

## Prerequisites

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| OS | Ubuntu 22.04 / Debian 12 | Ubuntu 24.04 |
| Docker | 24.0+ | Latest |
| Docker Compose | 2.20+ | Latest |
| Node.js | 20.x | 22.x |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 50 GB |
| CPU | 2 cores | 4 cores |

## Step 1: Clone

```bash
git clone https://github.com/TARS-OTHS/tars.git
cd tars
```

## Step 2: Run Setup

```bash
./setup.sh
```

The wizard handles everything:
- Checks prerequisites (installs missing ones with your permission)
- Asks about your use case and agent identity
- Walks through messaging platform setup (Discord recommended)
- Prompts for API keys you want to connect
- Lets you pick skills (some are bundled, others optional)
- Generates all config files
- Encrypts credentials into age vault
- Installs plugin dependencies
- Builds the agent sandbox image (`tars-sandbox:base`)
- Builds and starts all Docker services
- Configures sandboxing so agents run isolated from credentials and host

### What It Creates

```
~/.openclaw/
├── openclaw.json              # Gateway + agent + plugin config
├── exec-approvals.json        # Per-agent exec permissions
└── workspace/                 # Main agent workspace
    ├── SOUL.md                # Agent personality
    ├── IDENTITY.md            # Agent identity
    ├── TOOLS.md               # Available services
    ├── AGENTS.md              # Operating rules
    └── MEMORY.md              # Memory system reference

$TARS_HOME/
├── config/
│   └── team.json              # Team registry (humans + agents)
├── .secrets-vault/
│   └── secrets.age            # Age-encrypted credentials
├── .secrets/
│   └── age-key.txt            # Age decryption key
└── plugins/
    ├── tars-memory/           # Memory auto-recall plugin
    └── tars-team/             # Team context injection plugin
```

## Step 3: Start

```bash
docker compose up -d
```

All services start and health-check automatically:

```
auth-proxy        (healthy)
memory-db         (healthy)
embedding-service (healthy)
web-proxy         (healthy)
dashboard         (healthy)
cron              (healthy)
```

Then start the gateway:

```bash
openclaw gateway install
openclaw gateway start
```

## Step 4: Verify

Open the dashboard: `http://localhost:8765`

Or check services directly:
```bash
docker compose ps
journalctl --user -u openclaw-gateway -n 20
```

## Step 5: Talk to Your Agent

Open Discord (or whichever messaging platform you configured) and send a message. Your agent is listening.

## What's Next

- **Add team members:** Tell T.A.R.S in Discord: "Add Alice as admin..."
- **Add agents:** Tell T.A.R.S: "Create a sourcing agent..." or run `./scripts/add-agent.sh`
- **Update TARS:** `./scripts/update.sh` (pulls latest, rebuilds if needed)
- **Read the docs:** [Architecture](ARCHITECTURE.md) · [Security](SECURITY.md) · [Team Spec](TEAM-SPEC.md) · [Multi-Agent Spec](MULTI-AGENT-SPEC.md)

## Troubleshooting

If a service doesn't start:
```bash
docker compose logs <service-name>
```

If the agent doesn't respond:
```bash
# Check gateway logs
journalctl --user -u openclaw-gateway -n 50

# Check the bot is connected
curl http://localhost:9100/ops/health
```

See [Troubleshooting](TROUBLESHOOTING.md) for more.
