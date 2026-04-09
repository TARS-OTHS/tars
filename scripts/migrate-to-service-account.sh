#!/usr/bin/env bash
# Migrate T.A.R.S from root to dedicated tars service account.
# Run as root AFTER completing: sudo -u tars claude auth login
#
# This script:
#   1. Stops the user-level (root) tars service
#   2. Disables it so it doesn't restart
#   3. Transfers ownership of TARS_HOME to tars:tars
#   4. Locks down sensitive files (600)
#   5. Installs the system-level service (runs as User=tars)
#   6. Starts TARS under the new service account
#
# Rollback: run scripts/rollback-service-account.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TARS_DIR="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"

echo -e "${YELLOW}=== T.A.R.S Service Account Migration ===${NC}"
echo ""

# Pre-flight checks
echo "Running pre-flight checks..."

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}ERROR: Must run as root${NC}"
    exit 1
fi

if ! id tars &>/dev/null; then
    echo -e "${RED}ERROR: tars user does not exist${NC}"
    exit 1
fi

if [ ! -f /home/tars/.config/tars-vault-key ]; then
    echo -e "${RED}ERROR: Vault key not found at /home/tars/.config/tars-vault-key${NC}"
    exit 1
fi

if [ ! -f /home/tars/.claude/.credentials.json ]; then
    echo -e "${RED}ERROR: Claude credentials not found for tars user.${NC}"
    echo "Run: sudo -u tars claude auth login"
    exit 1
fi

if [ ! -f /etc/sudoers.d/tars ]; then
    echo -e "${RED}ERROR: Sudoers rule not found at /etc/sudoers.d/tars${NC}"
    exit 1
fi

if ! command -v /usr/local/bin/uv &>/dev/null; then
    echo -e "${RED}ERROR: uv not found at /usr/local/bin/uv${NC}"
    exit 1
fi

echo -e "${GREEN}All pre-flight checks passed.${NC}"
echo ""

# Step 1: Stop user-level service
echo "Step 1: Stopping user-level tars service..."
systemctl --user stop tars.service 2>/dev/null || true
systemctl --user disable tars.service 2>/dev/null || true
# Also kill any stray process
pkill -f "python -m src.main" 2>/dev/null || true
sleep 2
echo -e "${GREEN}  User-level service stopped.${NC}"

# Step 2: Transfer ownership
echo "Step 2: Transferring ownership to tars:tars..."
chown -R tars:tars "$TARS_DIR"
echo -e "${GREEN}  Ownership transferred.${NC}"

# Step 3: Lock down sensitive files
echo "Step 3: Setting file permissions..."
chmod 600 "$TARS_DIR"/config/secrets.enc
chmod 600 "$TARS_DIR"/config/team.json
chmod 600 "$TARS_DIR"/config/config.yaml
chmod 600 "$TARS_DIR"/data/*.db 2>/dev/null || true
chmod 700 "$TARS_DIR"/data
echo -e "${GREEN}  Permissions set.${NC}"

# Step 4: Install system-level service
echo "Step 4: Installing system-level service..."
cp "$TARS_DIR/config/tars.service" /etc/systemd/system/tars.service
systemctl daemon-reload
systemctl enable tars.service
echo -e "${GREEN}  System service installed and enabled.${NC}"

# Step 5: Start TARS as tars user
echo "Step 5: Starting T.A.R.S as tars user..."
systemctl start tars.service
sleep 3

if systemctl is-active --quiet tars.service; then
    echo -e "${GREEN}  T.A.R.S is running as tars user.${NC}"
    echo ""
    echo -e "${GREEN}=== Migration complete ===${NC}"
    echo ""
    systemctl status tars.service --no-pager
else
    echo -e "${RED}  T.A.R.S failed to start. Check logs:${NC}"
    echo "  journalctl -u tars -n 30 --no-pager"
    exit 1
fi
