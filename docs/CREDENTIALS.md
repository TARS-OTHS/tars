# TARS Credentials & Secrets Management

How TARS handles authentication, secrets, and API keys.

---

## Architecture

TARS uses a layered approach to credentials:

```
OpenClaw (owns)          TARS (owns)
  Claude auth       -->    .secrets/claude-token  (imported)
  Messaging config         .secrets-vault/secrets.age  (age-encrypted)
  Model selection          .env  (non-secret config)
```

- **OpenClaw** owns Claude authentication and messaging platform config
- **TARS** imports what it needs into its own secrets directory
- Integration API keys (Tavily, Notion, etc.) are stored in an age-encrypted vault

---

## Claude Authentication

TARS connects to Claude via OpenClaw's authentication. The flow:

1. **Initial auth**: User runs `openclaw setup` (during `./setup.sh` Section 3)
2. **Token import**: `setup.sh` copies the Claude token from OC's `auth-profiles.json` into `.secrets/claude-token`
3. **Auto-refresh**: A cron job (every 5 minutes) watches OC's auth file for changes and re-imports
4. **Manual reauth**: Run `scripts/tars-reauth.sh` if auto-refresh fails

### Token types
- `sk-ant-oat*` — OAuth token (Claude Max subscription)
- `sk-ant-api*` — API key (pay-per-use)

### When auth fails
- Services log a loud `AUTH_FAILED` error
- If `OPS_ALERTS_CHANNEL` is configured, an alert posts to Discord/Slack
- Fix: run `openclaw setup` to re-authenticate, then `scripts/tars-reauth.sh`

---

## Age-Encrypted Vault

Integration API keys are stored in `.secrets-vault/secrets.age`, encrypted with [age](https://github.com/FiloSottile/age).

### How it works
- `setup.sh` generates an age keypair at `.secrets/age-key.txt`
- Integration keys entered during setup are encrypted into `secrets.age`
- auth-proxy and credential-proxy decrypt the vault at startup
- Send SIGHUP to reload the vault without restarting

### Adding secrets after setup
```bash
# Decrypt current vault
age -d -i .secrets/age-key.txt .secrets-vault/secrets.age > /tmp/vault.json

# Edit (add your key)
nano /tmp/vault.json

# Re-encrypt
AGE_PUBKEY=$(age-keygen -y .secrets/age-key.txt)
age -r "$AGE_PUBKEY" -o .secrets-vault/secrets.age /tmp/vault.json

# Clean up plaintext
rm /tmp/vault.json

# Reload services (no restart needed)
docker compose kill -s HUP auth-proxy credential-proxy
```

### Vault format
```json
{
  "TAVILY_API_KEY": "tvly-...",
  "NOTION_TOKEN": "ntn_...",
  "TRELLO_KEY": "...",
  "TRELLO_TOKEN": "..."
}
```

---

## File Locations

| File | Purpose | Permissions |
|------|---------|-------------|
| `.env` | Non-secret config (ports, names, paths) | 600 |
| `.secrets/claude-token` | Claude auth token (auto-refreshed) | 600 |
| `.secrets/age-key.txt` | Age private key (never share) | 600 |
| `.secrets-vault/secrets.age` | Encrypted integration keys | 644 (encrypted) |

---

## Docker Volume Mounts

Services access secrets via read-only volume mounts:

- **auth-proxy**: `.secrets-vault` and `.config/age` mounted read-only
- **credential-proxy**: vault mounted read-only
- **memory-api**: `.secrets` mounted read-only (for claude-token)
- **cron**: `.secrets` mounted read-write (to update claude-token on refresh), OC auth dir mounted read-only

---

## Security Notes

- Never commit `.env`, `.secrets/`, or `.secrets-vault/` to git
- The age private key (`age-key.txt`) is the master secret — back it up securely
- Claude tokens expire; the cron refresh handles this automatically
- If the cron job fails, services continue with the last known good token
- All services start gracefully with empty secrets if vault is missing
