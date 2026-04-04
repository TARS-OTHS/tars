#!/usr/bin/env bash
# Regenerate MEMORY_CONTEXT.md for T.A.R.S v2 agent workspace.
# Gives agents a snapshot of memory stats for session persistence.
# Run every 30 minutes via cron.
set -uo pipefail

TARS_HOME="${TARS_HOME:-/opt/tars-v2}"
DB_PATH="$TARS_HOME/data/memory.db"
# Output to first agent dir found, or default to agents/main/
FIRST_AGENT=$(ls -d "$TARS_HOME"/agents/*/CLAUDE.md 2>/dev/null | head -1 | xargs dirname 2>/dev/null || echo "$TARS_HOME/agents/main")
OUTPUT="$FIRST_AGENT/MEMORY_CONTEXT.md"

if [ ! -f "$DB_PATH" ]; then
    echo "Memory DB not found at $DB_PATH"
    exit 0
fi

# Query stats directly from SQLite
total=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
semantic=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE type='semantic' AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
episodic=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE type='episodic' AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
procedural=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE type='procedural' AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
pinned=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE pinned=1;" 2>/dev/null || echo "0")
embedded=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
archived=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE scope LIKE 'archived%';" 2>/dev/null || echo "0")
inserts_24h=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE created_at > datetime('now', '-1 day');" 2>/dev/null || echo "0")
at_risk=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE pinned=0 AND confidence < 0.15 AND scope NOT LIKE 'archived%';" 2>/dev/null || echo "0")
db_size=$(du -m "$DB_PATH" 2>/dev/null | awk '{print $1}' || echo "0")

now_utc=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

cat > "$OUTPUT" << EOF
# Memory Context — Auto-Generated

**Generated:** $now_utc
**Updated every 30 minutes**
**Backend:** SQLite (inline, no Docker)

## Memory Stats

| Metric | Value |
|--------|-------|
| Active memories | $total |
| Semantic | $semantic |
| Episodic | $episodic |
| Procedural | $procedural |
| Pinned (immune) | $pinned |
| Embedded | $embedded |
| Archived | $archived |
| At risk (<0.15) | $at_risk |
| Inserts (24h) | $inserts_24h |
| DB size | ${db_size}MB |
EOF

echo "MEMORY_CONTEXT.md regenerated at $now_utc"
