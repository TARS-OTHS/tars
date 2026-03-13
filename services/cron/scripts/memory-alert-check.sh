#!/bin/bash
# memory-alert-check.sh — Checks memory cron logs for failures and alerts
# Runs every 30 min via cron.
# Discord alerts are optional — skips silently if not configured.

DISCORD_BOT_TOKEN_PATH="${SECRETS_DIR:-/app/secrets}/discord-bot-token"
CHANNEL="${OPS_ALERTS_CHANNEL:-}"
ALERT_STATE="/tmp/memory-alert-state"
API="${MEMORY_API_URL:-http://memory-api:8897}"
EMBEDDING_URL="${EMBEDDING_SERVICE_URL:-http://embedding-service:8896}"

log() { echo "[$(date -Iseconds)] alert-check: $*"; }

# Discord alerting is optional
CAN_ALERT=false
if [ -n "$CHANNEL" ] && [ -f "$DISCORD_BOT_TOKEN_PATH" ]; then
    DISCORD_TOKEN=$(cat "$DISCORD_BOT_TOKEN_PATH" 2>/dev/null)
    if [ -n "$DISCORD_TOKEN" ]; then
        CAN_ALERT=true
    fi
fi

send_alert() {
    local msg="$1"
    log "ALERT: $msg"
    if [ "$CAN_ALERT" = "true" ]; then
        curl -s -X POST "https://discord.com/api/v10/channels/$CHANNEL/messages" \
            -H "Authorization: Bot $DISCORD_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"content\": \"$msg\"}" > /dev/null 2>&1
    fi
}

mkdir -p "$ALERT_STATE"

check_log() {
    local name="$1"
    local log_file="/var/log/tars-cron.log"

    if [ ! -f "$log_file" ]; then return; fi

    # Get recent entries for this script
    local recent=$(grep "$name" "$log_file" 2>/dev/null | tail -10)
    if [ -z "$recent" ]; then return; fi

    # Check for failures in log prefix (not content within extracted data)
    if echo "$recent" | grep -qiE '(ERROR|FAIL|exception)'; then
        local state_file="$ALERT_STATE/${name}.last"
        local log_hash=$(echo "$recent" | md5sum | cut -d' ' -f1)
        local last_hash=$(cat "$state_file" 2>/dev/null)

        if [ "$log_hash" != "$last_hash" ]; then
            local error_line=$(echo "$recent" | grep -iE '(ERROR|FAIL)' | tail -1 | head -c 200)
            send_alert "⚠️ **${name}**: ${error_line}"
            echo "$log_hash" > "$state_file"
        fi
    fi
}

# Check all memory cron scripts
check_log "context-gen"
check_log "extract-sessions"
check_log "memory-lifecycle"
check_log "session-state"
check_log "memory-backup"
check_log "memory-promote"

# Check if embedding service is down
if ! curl -sf "$EMBEDDING_URL/health" > /dev/null 2>&1; then
    state_file="$ALERT_STATE/embedding-down.last"
    now=$(date +%s)
    last=$(cat "$state_file" 2>/dev/null || echo 0)
    # Only alert once per hour
    if [ $((now - last)) -gt 3600 ]; then
        send_alert "🔴 **Embedding service down** — not responding at $EMBEDDING_URL"
        echo "$now" > "$state_file"
    fi
fi

# Check if memory API is down
if ! curl -sf "$API/status" > /dev/null 2>&1; then
    state_file="$ALERT_STATE/memory-api-down.last"
    now=$(date +%s)
    last=$(cat "$state_file" 2>/dev/null || echo 0)
    if [ $((now - last)) -gt 3600 ]; then
        send_alert "🔴 **Memory API down** — not responding at $API"
        echo "$now" > "$state_file"
    fi
fi

log "Alert check completed"
