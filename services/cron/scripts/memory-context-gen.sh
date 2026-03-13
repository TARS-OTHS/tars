#!/bin/bash
# memory-context-gen.sh — Generates MEMORY_CONTEXT.md for each agent's workspace
# Runs every 30 min via cron. Fully mechanical — no agent cooperation needed.
# OC auto-injects workspace files into agent context at session start.
#
# Dynamically discovers agents from the OC agents directory.
# Each agent gets: session state, memory stats, service health, memory API reference.

API="${MEMORY_API_URL:-http://memory-api:8897}"
OC_DIR="${OPENCLAW_DIR:-/oc-config}"
OC_AGENTS_DIR="$OC_DIR/agents"
MAX_SIZE=6144  # ~6KB cap per file

log() { echo "[$(date -Iseconds)] context-gen: $*"; }

# Discover agents from OC directory structure
if [ ! -d "$OC_AGENTS_DIR" ]; then
    log "ERROR: OC agents directory not found at $OC_AGENTS_DIR"
    exit 1
fi

AGENTS=""
for AGENT_DIR in "$OC_AGENTS_DIR"/*/; do
    [ -d "$AGENT_DIR" ] || continue
    AGENT_NAME=$(basename "$AGENT_DIR")
    # Skip hidden directories
    case "$AGENT_NAME" in .*) continue ;; esac

    # Find workspace — check agent subdir, then OC workspace
    WORKSPACE=""
    if [ -d "$AGENT_DIR/workspace" ]; then
        WORKSPACE="$AGENT_DIR/workspace"
    elif [ -d "$OC_DIR/workspace" ]; then
        WORKSPACE="$OC_DIR/workspace"
    fi

    if [ -n "$WORKSPACE" ]; then
        AGENTS="${AGENTS}${AGENT_NAME}|${WORKSPACE}
"
    fi
done

if [ -z "$AGENTS" ]; then
    log "ERROR: No agents with workspaces found in $OC_AGENTS_DIR"
    exit 1
fi

# Count discovered agents
AGENT_COUNT=$(echo "$AGENTS" | grep -c '|')
log "Discovered $AGENT_COUNT agent(s)"

# --- Gather shared data once ---
log "Gathering system data..."

# Memory API status
STATUS_JSON=$(curl -sf "$API/status" 2>/dev/null || echo '{}')

# Parse stats with jq
total=$(echo "$STATUS_JSON" | jq -r '.memory_stats.total // 0')
semantic=$(echo "$STATUS_JSON" | jq -r '.memory_stats.by_type.semantic // 0')
episodic=$(echo "$STATUS_JSON" | jq -r '.memory_stats.by_type.episodic // 0')
procedural=$(echo "$STATUS_JSON" | jq -r '.memory_stats.by_type.procedural // 0')
avg_conf=$(echo "$STATUS_JSON" | jq -r '.memory_stats.avg_confidence // 0')
pinned=$(echo "$STATUS_JSON" | jq -r '.memory_stats.pinned // 0')
archived=$(echo "$STATUS_JSON" | jq -r '.memory_stats.archived // 0')
inserts_24h=$(echo "$STATUS_JSON" | jq -r '.memory_stats.last_24h.inserts // 0')
updates_24h=$(echo "$STATUS_JSON" | jq -r '.memory_stats.last_24h.updates // 0')
db_size=$(echo "$STATUS_JSON" | jq -r '.memory_stats.db_size_mb // 0')
uptime=$(echo "$STATUS_JSON" | jq -r '.uptime // "unknown"')

# Service health checks
check_service() {
    local url="$1" name="$2"
    if curl -sf "$url" > /dev/null 2>&1; then
        echo "- **$name**: healthy"
    else
        echo "- **$name**: DOWN"
    fi
}

HEALTH=""
HEALTH="${HEALTH}$(check_service "$API/status" "Memory API")
"
HEALTH="${HEALTH}$(check_service "${EMBEDDING_SERVICE_URL:-http://embedding-service:8896}/health" "Embedding Service")
"

# Current time
NOW_UTC=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

# Build agent registry
AGENT_REGISTRY=""
while IFS='|' read -r name ws; do
    [ -z "$name" ] && continue
    AGENT_REGISTRY="${AGENT_REGISTRY}| $name | $ws |
"
done <<< "$AGENTS"

log "System data gathered"

# --- Generate per-agent files ---
while IFS='|' read -r AGENT WORKSPACE; do
    [ -z "$AGENT" ] && continue
    log "Generating for $AGENT -> $WORKSPACE"

    mkdir -p "$WORKSPACE" 2>/dev/null

    # Fetch agent-scoped session state
    STATE_JSON=$(curl -sf "$API/memory/session-state/$AGENT" 2>/dev/null || echo '{}')
    task_summary=$(echo "$STATE_JSON" | jq -r '.state.task_summary // "No session state saved yet"')
    session_status=$(echo "$STATE_JSON" | jq -r '.state.status // "unknown"')
    session_context=$(echo "$STATE_JSON" | jq -r '.state.context // ""')
    session_updated=$(echo "$STATE_JSON" | jq -r '.updated_at // "never"')

    # Fetch agent-scoped context (pinned + recent)
    CONTEXT_JSON=$(curl -sf "$API/memory/context?agent=$AGENT" 2>/dev/null || echo '{}')

    # Format pinned memories
    PINNED_SECTION=""
    pinned_count=$(echo "$CONTEXT_JSON" | jq '.pinned | length // 0')
    if [ "$pinned_count" -gt 0 ]; then
        PINNED_SECTION="## Pinned Memories

"
        PINNED_SECTION="${PINNED_SECTION}$(echo "$CONTEXT_JSON" | jq -r '.pinned[:10][] | "- [\(.category // "general")] \(.content[:120])"')"
        PINNED_SECTION="${PINNED_SECTION}

"
    fi

    # Format recent memories
    RECENT_SECTION=""
    recent_count=$(echo "$CONTEXT_JSON" | jq '.recent | length // 0')
    if [ "$recent_count" -gt 0 ]; then
        RECENT_SECTION="## Recent Memories

"
        RECENT_SECTION="${RECENT_SECTION}$(echo "$CONTEXT_JSON" | jq -r '.recent[:8][] | "- [\(.category // "general")] \(.content[:120])"')"
        RECENT_SECTION="${RECENT_SECTION}

"
    fi

    # Format conflicts
    CONFLICTS_SECTION=""
    conflicts_count=$(echo "$CONTEXT_JSON" | jq '.conflicts | length // 0')
    if [ "$conflicts_count" -gt 0 ]; then
        CONFLICTS_SECTION="## Unresolved Conflicts ($conflicts_count)

"
        CONFLICTS_SECTION="${CONFLICTS_SECTION}$(echo "$CONTEXT_JSON" | jq -r '.conflicts[:5][] | "- \(.description[:120])"')"
        CONFLICTS_SECTION="${CONFLICTS_SECTION}

"
    fi

    OUTFILE="$WORKSPACE/MEMORY_CONTEXT.md"

    cat > "$OUTFILE" << EOF
# Memory Context (Auto-Generated)

_Updated: $NOW_UTC — regenerated every 30 min by cron_

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

${PINNED_SECTION}${RECENT_SECTION}${CONFLICTS_SECTION}## Service Health

$HEALTH

## Agents

| Name | Workspace |
|------|-----------|
$AGENT_REGISTRY

## Memory API

Query memories: \`GET $API/memory/search?q=<query>&agent=$AGENT\`
Semantic search: \`POST $API/memory/search/semantic\` with \`{"query": "...", "agent": "$AGENT"}\`
Save session state: \`POST $API/memory/session-state\` with \`{"agent": "$AGENT", "task_summary": "...", "status": "...", "context": "..."}\`

---
_Read TOOLS.md for endpoint details. Read MEMORY.md for memory API usage._
EOF

    # Size check
    SIZE=$(wc -c < "$OUTFILE")
    if [ "$SIZE" -gt "$MAX_SIZE" ]; then
        log "WARNING: $OUTFILE is ${SIZE}B (>${MAX_SIZE}B)"
    fi

    log "Written $OUTFILE (${SIZE}B)"

done <<< "$AGENTS"

log "Done. Generated context for all agents."
