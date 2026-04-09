#!/usr/bin/env bash
# Rollback: revert T.A.R.S from tars service account back to root.
# Run as root.
set -euo pipefail

TARS_DIR="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"

echo "Rolling back to root... (TARS_DIR=$TARS_DIR)"

# Stop system service
systemctl stop tars.service 2>/dev/null || true
systemctl disable tars.service 2>/dev/null || true
pkill -f "python -m src.main" 2>/dev/null || true
sleep 2

# Restore ownership
chown -R root:root "$TARS_DIR"

# Restore permissions
chmod 644 "$TARS_DIR"/config/team.json
chmod 644 "$TARS_DIR"/config/config.yaml
chmod 644 "$TARS_DIR"/data/*.db 2>/dev/null || true
chmod 755 "$TARS_DIR"/data

# Re-enable user-level service
systemctl --user enable tars.service 2>/dev/null || true
systemctl --user start tars.service 2>/dev/null || true

echo "Rolled back. TARS running as root via user-level service."
