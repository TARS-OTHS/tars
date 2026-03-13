#!/bin/bash
# memory-backup.sh — Backup memory database
# Runs every 6 hours via cron. Uses sqlite3 CLI .backup command.

BACKUP_DIR="${MEMORY_BACKUP_DIR:-/app/data/backups}"
DB_PATH="${MEMORY_DB_PATH:-/app/data/memory.db}"
TIMESTAMP=$(date +%Y-%m-%d-%H%M)

log() { echo "[$(date -Iseconds)] backup: $*"; }

if [ ! -f "$DB_PATH" ]; then
    log "WARNING: DB not found at $DB_PATH — skipping"
    exit 0
fi

mkdir -p "$BACKUP_DIR"

# Use sqlite3 .backup for online-safe backup
BACKUP_FILE="$BACKUP_DIR/memory-$TIMESTAMP.db"
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"

if [ $? -eq 0 ] && [ -f "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log "Backup completed: memory-$TIMESTAMP.db ($SIZE)"
else
    log "ERROR: Backup failed"
    exit 1
fi

# Keep only 7 days of backups
find "$BACKUP_DIR" -name "memory-*.db" -mtime +7 -delete
log "Old backups cleaned"
