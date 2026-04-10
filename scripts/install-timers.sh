#!/usr/bin/env bash
# Install all T.A.R.S systemd timers.
# Run as root: sudo bash scripts/install-timers.sh
#
# Renders timer templates from config/timers/ with correct paths,
# writes them to the overlay's systemd/ dir, and symlinks from
# /etc/systemd/system/. Falls back to direct install if no overlay.
set -euo pipefail

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
TIMER_DIR="$TARS_HOME/config/timers"

# Try to pick up TARS_OVERLAY from env, or from the main service unit
if [ -z "${TARS_OVERLAY:-}" ]; then
    TARS_OVERLAY=$(grep -oP 'TARS_OVERLAY=\K.*' /etc/systemd/system/tars.service 2>/dev/null \
        || true)
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

if [ ! -d "$TIMER_DIR" ]; then
    echo "ERROR: Timer directory not found: $TIMER_DIR"
    exit 1
fi

echo "Installing T.A.R.S systemd timers..."
echo "  TARS_HOME:    $TARS_HOME"
echo "  TARS_OVERLAY: ${TARS_OVERLAY:-<not set>}"

# Determine target directory for rendered files
if [ -n "${TARS_OVERLAY:-}" ]; then
    TARGET_DIR="$TARS_OVERLAY/systemd"
    mkdir -p "$TARGET_DIR"
    USE_SYMLINKS=true
else
    TARGET_DIR="/etc/systemd/system"
    USE_SYMLINKS=false
fi

# Install service and timer files:
# 1. Substitute /opt/tars placeholder with actual TARS_HOME
# 2. Inject TARS_OVERLAY env var if set (after the TARS_HOME line)
for src in "$TIMER_DIR"/tars-*.service "$TIMER_DIR"/tars-*.timer; do
    [ -f "$src" ] || continue
    name=$(basename "$src")
    content=$(sed "s|/opt/tars|$TARS_HOME|g" "$src")
    if [ -n "${TARS_OVERLAY:-}" ] && [[ "$name" == *.service ]]; then
        content=$(echo "$content" | sed "/Environment=TARS_HOME=/a Environment=TARS_OVERLAY=$TARS_OVERLAY")
    fi
    echo "$content" > "$TARGET_DIR/$name"

    # Symlink from /etc/systemd/system/ if using overlay
    if [ "$USE_SYMLINKS" = true ]; then
        ln -sf "$TARGET_DIR/$name" "/etc/systemd/system/$name"
    fi

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
