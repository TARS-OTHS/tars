#!/usr/bin/env bash
# Rollback: revert T.A.R.S from tars service account back to root.
# Run as root.
set -euo pipefail

echo "Rolling back to root..."

# Stop system service
systemctl stop tars.service 2>/dev/null || true
systemctl disable tars.service 2>/dev/null || true
pkill -f "python -m src.main" 2>/dev/null || true
sleep 2

# Restore ownership
chown -R root:root /opt/tars-v2

# Restore permissions
chmod 644 /opt/tars-v2/config/team.json
chmod 644 /opt/tars-v2/config/config.yaml
chmod 644 /opt/tars-v2/data/*.db 2>/dev/null || true
chmod 755 /opt/tars-v2/data

# Re-enable user-level service
systemctl --user enable tars.service 2>/dev/null || true
systemctl --user start tars.service 2>/dev/null || true

echo "Rolled back. TARS running as root via user-level service."
