# T.A.R.S Scripts Reference

## Setup & Operations

### `setup.py` — Interactive Setup Wizard
Guides you through first-time configuration: vault, Discord bot, team, first agent, HITL settings.
```bash
uv run python setup.py
```

### `setup.sh` — System-Level Setup
Creates venv, installs deps, sets up service account and systemd service.
```bash
./setup.sh
```

### `scripts/settings.py` — Interactive Settings Manager
Post-install TUI for viewing and modifying all T.A.R.S configuration. Menu-driven — covers LLM defaults, connectors, session, memory, HITL, rate limits, compression, admin users, vault secrets, agent overview, and agent creation.
```bash
uv run python scripts/settings.py
```

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

### `scripts/install-timers.sh` — Timer Installer
Installs all systemd timer+service files from `config/timers/`.
```bash
sudo scripts/install-timers.sh
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
