#!/bin/bash
# monitor-container-health.sh — Verify container security baseline hasn't drifted.
# Checks: capabilities, non-root, no Docker socket, metadata blocked.
# Runs on HOST every 6 hours via cron.
set -euo pipefail

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$TARS_HOME/scripts/lib-alert.sh"
LOG_PREFIX="[container-health]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }

ISSUES=""

# Legacy Docker containers removed — memory and embeddings are now inline.
# This script now only checks host-level security.

# Check cloud metadata is blocked (check saved rules file — works without root)
if ! grep -q '169.254.169.254' /etc/iptables/rules.v4 2>/dev/null; then
    ISSUES="${ISSUES}\n- **host**: cloud metadata iptables rule MISSING"
fi

if [ -n "$ISSUES" ]; then
    send_alert "SECURITY | Container health drift detected$(echo -e "$ISSUES")"
    log "Issues found:$(echo -e "$ISSUES")"
else
    log "All checks passed"
fi
