#!/usr/bin/env bash
# Install all T.A.R.S systemd timers.
# Run as root: sudo bash scripts/install-timers.sh
#
# Detects TARS_HOME from script location and substitutes into templates.
set -euo pipefail

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
TIMER_DIR="$TARS_HOME/config/timers"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

if [ ! -d "$TIMER_DIR" ]; then
    echo "ERROR: Timer directory not found: $TIMER_DIR"
    exit 1
fi

echo "Installing T.A.R.S systemd timers..."
echo "  TARS_HOME: $TARS_HOME"

# Install service and timer files, substituting /opt/tars placeholder with actual path
for src in "$TIMER_DIR"/tars-*.service "$TIMER_DIR"/tars-*.timer; do
    [ -f "$src" ] || continue
    name=$(basename "$src")
    sed "s|/opt/tars|$TARS_HOME|g" "$src" > "/etc/systemd/system/$name"
    echo "  Installed: $name"
done

# Reload systemd
systemctl daemon-reload

# Enable and start all timers
for timer in "$TIMER_DIR"/tars-*.timer; do
    [ -f "$timer" ] || continue
    name=$(basename "$timer")
    systemctl enable --now "$name"
    echo "  Enabled: $name"
done

echo ""
echo "All timers installed. Verify with:"
echo "  systemctl list-timers | grep tars"
