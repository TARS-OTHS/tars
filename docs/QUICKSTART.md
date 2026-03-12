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
- Lets you pick skills
- Generates all config files
- Stores credentials securely

### What It Creates

```
config/
├── platform.yaml          # Master config (single source of truth)
├── auth-proxy.json        # API route definitions
└── openclaw.json          # Gateway + agent definitions

.secrets/
├── discord.env            # Bot token, guild ID
├── anthropic.env          # API key
├── tavily.env             # Search API key
└── ...                    # Other integrations you configured
```

## Step 3: Start

```bash
docker compose up -d
```

All services start and health-check automatically:

```
✅ auth-proxy        (healthy)
✅ memory-db         (healthy)
✅ embedding-service  (healthy)
✅ web-proxy         (healthy)
✅ dashboard         (healthy)
✅ openclaw-gateway   (healthy)
```

## Step 4: Verify

```bash
./scripts/status.sh
```

Or open the dashboard: `http://localhost:8765`

## Step 5: Talk to Your Agent

Open Discord (or whichever messaging platform you configured) and send a message. Your agent is listening.

## What's Next

- **Add integrations:** Dashboard → Settings, or `./scripts/add-integration.sh`
- **Add agents:** `./scripts/add-agent.sh`
- **Install skills:** Dashboard → Skills
- **Set up backups:** `crontab -e` → add `0 */6 * * * /path/to/tars/scripts/backup.sh`
- **Harden security:** `./scripts/security-audit.sh`
- **Read the docs:** [Architecture](ARCHITECTURE.md) · [Security](SECURITY.md) · [Skills](SKILLS.md)

## Troubleshooting

If a service doesn't start:
```bash
docker compose logs <service-name>
```

If the agent doesn't respond:
```bash
# Check gateway
docker compose logs openclaw-gateway

# Check the bot is connected
curl http://localhost:9100/discord-api/users/@me
```

See [Troubleshooting](TROUBLESHOOTING.md) for more.
