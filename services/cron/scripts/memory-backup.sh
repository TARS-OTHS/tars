#!/bin/bash
BACKUP_DIR="${MEMORY_BACKUP_DIR:-/app/data/backups}"
DB_PATH="${MEMORY_DB_PATH:-/app/data/memory.db}"
TIMESTAMP=$(date +%Y-%m-%d-%H%M)

mkdir -p "$BACKUP_DIR"

# Use node + better-sqlite3 for online backup (no sqlite3 CLI)
cd ${AGENT_SERVICES_DIR:-/app}
node -e "
const Database = require('better-sqlite3');
const db = new Database('$DB_PATH', { readonly: true });
db.backup('$BACKUP_DIR/memory-$TIMESTAMP.db')
  .then(() => { console.log('Backup completed: memory-$TIMESTAMP.db'); db.close(); })
  .catch(err => { console.error('Backup failed:', err.message); db.close(); process.exit(1); });
"

# Keep only 7 days of backups
find "$BACKUP_DIR" -name "memory-*.db" -mtime +7 -delete
