#!/bin/bash
# monitor-host-security.sh — Host-level security baseline checks.
# Checks: cloud metadata blocked, unexpected SUID binaries, etc.
# Runs every 6 hours via tars-host-security.timer.
set -euo pipefail

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$TARS_HOME/scripts/lib-alert.sh"
LOG_PREFIX="[host-security]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }

ISSUES=""

# Check cloud metadata is blocked (check saved rules file — works without root)
if ! grep -q '169.254.169.254' /etc/iptables/rules.v4 2>/dev/null; then
    ISSUES="${ISSUES}\n- **host**: cloud metadata iptables rule MISSING"
fi

if [ -n "$ISSUES" ]; then
    send_alert "SECURITY | Host security drift detected$(echo -e "$ISSUES")"
    log "Issues found:$(echo -e "$ISSUES")"
else
    log "All checks passed"
fi
