# T.A.R.S Scripts Reference

## Setup & Operations

### `setup.py` — Interactive Setup Wizard
Complete setup from clone to running agent. Run once after cloning.
```bash
uv run python setup.py
```

**Steps:**
1. **Dependencies** — checks Python, uv, jq, Claude Code CLI; runs `scripts/sync.sh`
2. **Overlay** — creates deployment overlay directory structure
3. **Git hooks** — installs pre-commit hook (compares and updates on re-run)
4. **Modules** — discovers Layer 2 extension modules, lets you select which to enable
5. **Vault** — creates encrypted credential vault (or reuses existing)
6. **Discord** — bot token, guild selection, channel listing
7. **Team** — owner + team member setup
8. **Agent** — first agent config (name, model, personality)
9. **HITL** — approval channel, approvers, gated tools
10. **Compression** — optional context compression settings
11. **Config generation** — writes config.yaml, agents.yaml, team.json, mcp.yaml, agent CLAUDE.md
12. **Ops instance** — optional privileged agent for dev/ops (separate bot + service)
13. **Extras** — add more team members, agents, or bots
14. **Systemd** — generates timer units (automatic), optionally generates main service unit; calls `scripts/install-systemd.sh` to install
15. **Browser** — optional Chromium download for headless browsing

### `setup.sh` — Deprecated Stub
Redirects to `setup.py`. Prints deprecation notice. Kept for backwards compatibility.
```bash
./setup.sh  # equivalent to: uv run python setup.py
```

### `scripts/settings.py` — Interactive Settings Manager
Post-install TUI for viewing and modifying all configuration without re-running setup.
```bash
uv run python scripts/settings.py
```

**Menu options:**
1. **LLM defaults** — provider, model, max tokens, timeout
2. **Connectors** — Discord bot accounts, enable/disable platforms
3. **Session** — max history, summarize threshold
4. **Memory** — backend, semantic search toggle, decay settings, max results
5. **HITL** — approval channel/timeout, approver management, gated tools (add/remove with available tools list)
6. **Rate limits** — mode (enforce/log), per-tool limits, wildcard patterns
7. **Compression** — enable/disable, compression level
8. **Admin users** — Discord user IDs with admin access
9. **Identity** — system name, log level, data directory
10. **Agents** — list all agents, change model, routing, or caveman mode per agent
11. **Create agent** — new agent with tier selection, bot assignment, channel filtering
12. **Timers** — list systemd timers with status/schedule, enable/disable, edit schedules
13. **Ops instance** — create privileged dual-instance setup
14. **Vault secrets** — add, update, delete, list vault entries

### `vault-manage.py` — Interactive Vault Manager
Manage encrypted secrets (add, update, delete, list, check).
```bash
uv run python vault-manage.py
```

## Testing

### `scripts/test-tools.py` — E2E Tool Test Suite
Runs tools with real API calls and reports pass/fail.
```bash
# Run all tests
uv run python scripts/test-tools.py

# Test a specific tool
uv run python scripts/test-tools.py --tool team_list

# Filter by category keyword
uv run python scripts/test-tools.py --category google
uv run python scripts/test-tools.py --category trello
```

## Monitoring Scripts

All monitoring scripts use `scripts/lib-alert.sh` for Discord alerts via the Fernet vault. Install as systemd timers via `scripts/install-timers.sh`.

### `scripts/health-audit.sh` — System Health Check
Service status, disk/RAM/swap usage, zombie processes, temp file cleanup. Runs every 6 hours.

### `scripts/monitor-container-health.sh` — Container Security Monitor
Checks Docker containers for security baseline drift (capabilities, non-root, no-new-privileges). Runs every 6 hours.

### `scripts/monitor-integrity.sh` — File Integrity Monitor
SHA256 checksums of critical files compared against baseline. Alerts on changes. Runs every 12 hours.
```bash
# Update baseline after intentional changes
scripts/monitor-integrity.sh --update-baseline
```

### `scripts/monitor-exposure.sh` — Public Port Monitor
Scans for unexpected public-facing ports. Runs daily.

### `scripts/regen-memory-context.sh` — Memory Context Generator
Regenerates memory stats and service health snapshot. Runs every 30 minutes.

### `scripts/memory-decay.sh` — Memory Lifecycle
Applies confidence decay, archives old memories, purges expired archives. Runs daily.

### `scripts/lib-alert.sh` — Shared Alert Helper
Sourced by all scripts. Provides `send_alert()` function that posts to Discord using the bot token from the Fernet vault.
```bash
source scripts/lib-alert.sh
send_alert "Something happened"
```

### `scripts/install-timers.sh` — Timer Installer (legacy)
Installs all systemd timer+service files from `config/timers/`.
```bash
sudo scripts/install-timers.sh
```

### `scripts/install-systemd.sh` — Systemd Unit Installer
Symlinks generated unit files (timers + service) into `/etc/systemd/system/`, runs daemon-reload, enables timers. Called by `setup.py` automatically.
```bash
# Called by setup.py, or run manually:
sudo bash scripts/install-systemd.sh <overlay-dir> [--enable-service]
```

### `scripts/compress-context.sh` — Context Compression
Batch compress agent context files (codex docs, skill prompts) to reduce input tokens. Idempotent — skips unchanged files. CLAUDE.md files are excluded by default.
```bash
# Dry run — show savings without writing
scripts/compress-context.sh --dry-run

# Compress with specific level
scripts/compress-context.sh --level lite      # filler phrases only
scripts/compress-context.sh --level standard  # filler + contractions (default)
```

### `scripts/google-reauth.py` — Google OAuth2 Re-auth
Helper for re-authenticating Google OAuth2 credentials when tokens expire.
```bash
uv run python scripts/google-reauth.py
```

## Running T.A.R.S

### Production (systemd)
```bash
systemctl start tars.service
systemctl stop tars.service
systemctl status tars.service
journalctl -u tars -f
```

### Development
```bash
uv run python -m src.main
```

### Test Profile
```bash
uv run python -m src.main --profile test
```
