#!/bin/bash
# memory-alert-check.sh — Checks memory cron logs for failures and alerts to Discord
# Runs every 30 min via cron.

DISCORD_TOKEN=$(cat ${TARS_HOME:-/app}/.secrets/rescue-discord-token)
CHANNEL="1478653539004710954"
ALERT_STATE="/tmp/memory-alert-state"

send_alert() {
    local msg="$1"
    curl -s -X POST "https://discord.com/api/v10/channels/$CHANNEL/messages" \
        -H "Authorization: Bot $DISCORD_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"content\": \"$msg\"}" > /dev/null 2>&1
}

mkdir -p "$ALERT_STATE"

check_log() {
    local name="$1"
    local log="/tmp/${name}.log"
    local state_file="$ALERT_STATE/${name}.last"

    if [ ! -f "$log" ]; then return; fi

    # Get last 5 lines
    local recent=$(tail -5 "$log" 2>/dev/null)

    # Check for failures — only match log-level prefixes, not content within extracted data
    # Match patterns like: "ERROR:", "WARNING:", "FAIL", "failed for" in the log prefix part
    if echo "$recent" | grep -qiE '^\[.*\].*(ERROR|WARNING|FAIL|exception)' ; then
        # Check if we already alerted for this
        local log_hash=$(echo "$recent" | md5sum | cut -d' ' -f1)
        local last_hash=$(cat "$state_file" 2>/dev/null)

        if [ "$log_hash" != "$last_hash" ]; then
            local error_line=$(echo "$recent" | grep -iE '^\[.*\].*(ERROR|WARNING|FAIL)' | tail -1 | head -c 200)
            send_alert "⚠️ **${name}**: ${error_line}"
            echo "$log_hash" > "$state_file"
        fi
    fi
}

# Check all memory crons
check_log "memory-tree-regen"
check_log "memory-extract-sessions"
check_log "memory-context-gen"
check_log "memory-lifecycle"
check_log "session-state-cron"
check_log "memory-backup"
check_log "memory-promote"

# Also check if embedding service is down
if ! curl -s http://127.0.0.1:8896/health | grep -q '"ok"'; then
    state_file="$ALERT_STATE/embedding-down.last"
    now=$(date +%s)
    last=$(cat "$state_file" 2>/dev/null || echo 0)
    # Only alert once per hour
    if [ $((now - last)) -gt 3600 ]; then
        send_alert "🔴 **Embedding service down** — BGE-small-en-v1.5 on port 8896 is not responding"
        echo "$now" > "$state_file"
    fi
fi

# Check if broker is down
if ! curl -s ${MEMORY_API_URL:-http://memory-api:8897}/status | grep -q '"uptime"'; then
    state_file="$ALERT_STATE/broker-down.last"
    now=$(date +%s)
    last=$(cat "$state_file" 2>/dev/null || echo 0)
    if [ $((now - last)) -gt 3600 ]; then
        send_alert "🔴 **Broker down** — agent-services on port 8897 is not responding"
        echo "$now" > "$state_file"
    fi
fi
