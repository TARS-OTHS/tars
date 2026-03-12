#!/bin/bash
DB_PATH="${MEMORY_DB_PATH:-/app/data/memory.db}"

cd ${AGENT_SERVICES_DIR:-/app}
node -e "
const Database = require('better-sqlite3');
const db = new Database('$DB_PATH');
db.pragma('journal_mode = WAL');
db.pragma('busy_timeout = 5000');

// Category-based decay with access floor
const decay = db.prepare(\`
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
    AND scope != 'archived'
\`).run();
console.log('Decay applied to', decay.changes, 'memories');

// Archive old low-confidence memories
const archive = db.prepare(\`
    UPDATE memories
    SET scope = 'archived'
    WHERE pinned = 0
    AND access_count < 3
    AND confidence <= 0.1
    AND last_accessed < datetime('now', '-60 days')
    AND scope != 'archived'
\`).run();
console.log('Archived', archive.changes, 'memories');

// Purge archived memories older than 30 days (backups retain 7 days of full snapshots)
const purge = db.prepare(\`
    DELETE FROM memories
    WHERE scope = 'archived'
    AND updated_at < datetime('now', '-30 days')
\`).run();
if (purge.changes > 0) console.log('Purged', purge.changes, 'archived memories older than 30 days');

// Also purge changelog entries older than 30 days to prevent unbounded growth
const purgeLog = db.prepare(\`
    DELETE FROM changelog
    WHERE timestamp < datetime('now', '-30 days')
\`).run();
if (purgeLog.changes > 0) console.log('Purged', purgeLog.changes, 'old changelog entries');

db.close();
console.log('Lifecycle completed at', new Date().toISOString());
"
