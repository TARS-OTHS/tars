#!/bin/bash
# memory-lifecycle.sh — Decay, archive, and purge old memories
# Runs every 6 hours via cron. Uses sqlite3 CLI directly.

DB_PATH="${MEMORY_DB_PATH:-/app/data/memory.db}"

log() { echo "[$(date -Iseconds)] lifecycle: $*"; }

if [ ! -f "$DB_PATH" ]; then
    log "WARNING: DB not found at $DB_PATH — skipping"
    exit 0
fi

# Category-based decay with access floor
decay=$(sqlite3 "$DB_PATH" << 'SQL'
UPDATE memories
SET confidence = MAX(
    CASE
        WHEN access_count >= 3 THEN 0.3
        WHEN category IN ('user', 'people') THEN 0.2
        WHEN category IN ('system') THEN 0.1
        ELSE 0.05
    END,
    confidence - CASE
        WHEN category = 'user' THEN 0.002
        WHEN category = 'people' THEN 0.003
        WHEN category = 'system' THEN 0.005
        ELSE 0.01
    END
),
updated_at = CURRENT_TIMESTAMP
WHERE pinned = 0
AND last_accessed < datetime('now', '-7 days')
AND scope != 'archived';
SELECT changes();
SQL
)
log "Decay applied to $decay memories"

# Archive old low-confidence memories
archive=$(sqlite3 "$DB_PATH" << 'SQL'
UPDATE memories
SET scope = 'archived'
WHERE pinned = 0
AND access_count < 3
AND confidence <= 0.1
AND last_accessed < datetime('now', '-60 days')
AND scope != 'archived';
SELECT changes();
SQL
)
log "Archived $archive memories"

# Purge archived memories older than 30 days
purge=$(sqlite3 "$DB_PATH" << 'SQL'
DELETE FROM memories
WHERE scope = 'archived'
AND updated_at < datetime('now', '-30 days');
SELECT changes();
SQL
)
if [ "$purge" -gt 0 ] 2>/dev/null; then
    log "Purged $purge archived memories older than 30 days"
fi

# Purge old changelog entries
purge_log=$(sqlite3 "$DB_PATH" << 'SQL'
DELETE FROM changelog
WHERE timestamp < datetime('now', '-30 days');
SELECT changes();
SQL
)
if [ "$purge_log" -gt 0 ] 2>/dev/null; then
    log "Purged $purge_log old changelog entries"
fi

log "Lifecycle completed"
