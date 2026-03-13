#!/usr/bin/env bash
# Regenerate MEMORY_CONTEXT.md for the agent workspace
# Run via cron every 30 minutes: */30 * * * * /opt/tars/scripts/regen-memory-context.sh
set -uo pipefail

DOCKER_HOST_IP="${DOCKER_HOST_IP:-172.17.0.1}"
MEMORY_API="http://${DOCKER_HOST_IP}:${MEMORY_API_PORT:-8897}"
OC_WORKSPACE="${OC_WORKSPACE:-$HOME/.openclaw/workspace}"
OUTPUT="$OC_WORKSPACE/MEMORY_CONTEXT.md"

# Fetch data (fail gracefully)
status_json=$(curl -sf "$MEMORY_API/status" 2>/dev/null || echo '{}')
session_json=$(curl -sf "$MEMORY_API/memory/session-state/main" 2>/dev/null || echo '{}')

# Parse stats
total=$(echo "$status_json" | jq -r '.memory_stats.total // 0')
semantic=$(echo "$status_json" | jq -r '.memory_stats.by_type.semantic // 0')
episodic=$(echo "$status_json" | jq -r '.memory_stats.by_type.episodic // 0')
procedural=$(echo "$status_json" | jq -r '.memory_stats.by_type.procedural // 0')
avg_conf=$(echo "$status_json" | jq -r '.memory_stats.avg_confidence // 0')
pinned=$(echo "$status_json" | jq -r '.memory_stats.pinned // 0')
archived=$(echo "$status_json" | jq -r '.memory_stats.archived // 0')
inserts_24h=$(echo "$status_json" | jq -r '.memory_stats.last_24h.inserts // 0')
updates_24h=$(echo "$status_json" | jq -r '.memory_stats.last_24h.updates // 0')
db_size=$(echo "$status_json" | jq -r '.memory_stats.db_size_mb // 0')

# Parse session state
task_summary=$(echo "$session_json" | jq -r '.state.task_summary // "No session state saved yet"')
session_status=$(echo "$session_json" | jq -r '.state.status // "unknown"')
session_context=$(echo "$session_json" | jq -r '.state.context // ""')
session_updated=$(echo "$session_json" | jq -r '.updated_at // "never"')

# Current time
now_utc=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

# Check service health
check_service() {
    local url="$1" name="$2"
    if curl -sf "$url" > /dev/null 2>&1; then
        echo "- **$name**: healthy"
    else
        echo "- **$name**: DOWN"
    fi
}

cat > "$OUTPUT" << EOF
# MEMORY_CONTEXT.md — Auto-Generated Context

**Generated:** $now_utc
**Updated every 30 minutes by cron**

---

## Last Session State

- **Task:** $task_summary
- **Status:** $session_status
- **Context:** $session_context
- **Last saved:** $session_updated

## Memory Overview

| Metric | Value |
|--------|-------|
| Total memories | $total |
| Semantic | $semantic |
| Episodic | $episodic |
| Procedural | $procedural |
| Avg confidence | $avg_conf |
| Pinned | $pinned |
| Archived | $archived |
| Inserts (24h) | $inserts_24h |
| Updates (24h) | $updates_24h |
| DB size | ${db_size}MB |

## Service Health

$(check_service "http://${DOCKER_HOST_IP}:8897/status" "Memory API (8897)")
$(check_service "http://${DOCKER_HOST_IP}:8896/health" "Embedding Service (8896)")
$(check_service "http://${DOCKER_HOST_IP}:9100/ops/health" "Auth Proxy (9100)")
$(check_service "http://${DOCKER_HOST_IP}:8899/" "Web Proxy (8899)")
$(check_service "http://${DOCKER_HOST_IP}:8766/" "Dashboard API (8766)")
$(check_service "http://${DOCKER_HOST_IP}:8765/" "Dashboard UI (8765)")

## Services Map

| Service | Port | URL |
|---------|------|-----|
| Memory API | 8897 | http://${DOCKER_HOST_IP}:8897 |
| Embedding | 8896 | http://${DOCKER_HOST_IP}:8896 |
| Auth Proxy | 9100 | http://${DOCKER_HOST_IP}:9100 |
| Web Proxy | 8899 | http://${DOCKER_HOST_IP}:8899 |
| Dashboard UI | 8765 | http://${DOCKER_HOST_IP}:8765 |
| Dashboard API | 8766 | http://${DOCKER_HOST_IP}:8766 |
| OC Gateway | 18789 | http://localhost:18789 |

---
*Read TOOLS.md for endpoint details. Read MEMORY.md for memory API usage.*
EOF

echo "MEMORY_CONTEXT.md regenerated at $now_utc"
