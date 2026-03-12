#!/usr/bin/env bash
# claude-token-refresh.sh — Auto-refresh Claude token from OpenClaw auth-profiles.
#
# Watches OC's auth-profiles.json (mounted read-only). When the file changes
# (OC refreshed the token), extracts the new token and writes it to TARS secrets.
# Posts to ops-alerts on success or failure.
#
# Runs every 5 minutes via cron. Resource cost: ~10ms per run.

set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-/app/secrets}"
TOKEN_PATH="${SECRETS_DIR}/claude-token"
OC_AUTH_PATH="${OC_AUTH_PROFILES:-/oc-auth/auth-profiles.json}"
MTIME_PATH="${SECRETS_DIR}/.claude-token-mtime"
OPS_ALERTS_CHANNEL="${OPS_ALERTS_CHANNEL:-}"
DISCORD_BOT_TOKEN_PATH="${SECRETS_DIR}/discord-bot-token"
LOG_PREFIX="[claude-token-refresh]"

log()  { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }
warn() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX WARNING: $1" >&2; }
err()  { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX ERROR: $1" >&2; }

# Post to ops-alerts Discord channel
post_alert() {
    local message="$1"
    if [[ -z "$OPS_ALERTS_CHANNEL" ]] || [[ ! -f "$DISCORD_BOT_TOKEN_PATH" ]]; then
        return 0
    fi
    local bot_token
    bot_token=$(cat "$DISCORD_BOT_TOKEN_PATH" 2>/dev/null) || return 0
    curl -sf -X POST "https://discord.com/api/v10/channels/${OPS_ALERTS_CHANNEL}/messages" \
        -H "Authorization: Bot $bot_token" \
        -H "Content-Type: application/json" \
        -d "{\"content\": \"**TARS Auth**\\n${message}\"}" > /dev/null 2>&1 || true
}

# Check OC auth file exists
if [[ ! -f "$OC_AUTH_PATH" ]]; then
    # Not an error on first run — OC may not be configured yet
    log "OC auth-profiles.json not found at $OC_AUTH_PATH — skipping"
    exit 0
fi

# Check if file changed since last run
current_mtime=$(stat -c %Y "$OC_AUTH_PATH" 2>/dev/null || echo "0")
last_mtime=$(cat "$MTIME_PATH" 2>/dev/null || echo "0")

if [[ "$current_mtime" == "$last_mtime" ]] && [[ -f "$TOKEN_PATH" ]] && [[ -s "$TOKEN_PATH" ]]; then
    # No change and token exists — nothing to do
    exit 0
fi

log "Auth file changed (or token missing) — extracting token"

# Extract token from OC auth-profiles.json
# Tries: anthropic:manual, anthropic:default, then any anthropic profile
token=""
if command -v jq &>/dev/null; then
    token=$(jq -r '
        .profiles["anthropic:manual"].token //
        .profiles["anthropic:default"].token //
        .profiles["anthropic:default"].key //
        (.profiles | to_entries | map(select(.key | startswith("anthropic"))) | .[0].value.token // .[0].value.key // empty)
    ' "$OC_AUTH_PATH" 2>/dev/null || true)
elif command -v python3 &>/dev/null; then
    token=$(python3 -c "
import json
with open('$OC_AUTH_PATH') as f:
    d = json.load(f)
p = d.get('profiles', {})
for k in ['anthropic:manual', 'anthropic:default']:
    if k in p:
        print(p[k].get('token', p[k].get('key', '')))
        break
" 2>/dev/null || true)
fi

if [[ -z "$token" ]]; then
    err "Could not extract Anthropic token from $OC_AUTH_PATH"
    post_alert "Failed to extract Claude token from OpenClaw auth. Run \`openclaw setup\` to re-authenticate."
    exit 1
fi

# Validate token format
if [[ ! "$token" =~ ^sk-ant- ]]; then
    warn "Token doesn't match expected format (sk-ant-*) — writing anyway"
fi

# Write token
echo "$token" > "$TOKEN_PATH"
chmod 600 "$TOKEN_PATH"
echo "$current_mtime" > "$MTIME_PATH"

log "Token updated successfully (${#token} chars, type: $(echo "$token" | cut -c1-10)...)"
post_alert "Claude token refreshed successfully."
