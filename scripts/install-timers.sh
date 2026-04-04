#!/usr/bin/env bash
# Install all T.A.R.S systemd timers.
# Run as root: sudo bash /opt/tars-v2/scripts/install-timers.sh
set -euo pipefail

TIMER_DIR="/opt/tars-v2/config/timers"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

echo "Installing T.A.R.S systemd timers..."

# Copy all service and timer files
cp "$TIMER_DIR"/tars-*.service /etc/systemd/system/
cp "$TIMER_DIR"/tars-*.timer /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable and start all timers
for timer in "$TIMER_DIR"/tars-*.timer; do
    name=$(basename "$timer")
    systemctl enable --now "$name"
    echo "  Enabled: $name"
done

echo ""
echo "All timers installed. Verify with:"
echo "  systemctl list-timers | grep tars"
