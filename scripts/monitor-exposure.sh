#!/bin/bash
# monitor-exposure.sh — Check for unexpected public exposure.
# Verifies no unexpected ports are listening on public interfaces.
# Runs on HOST daily via cron.
set -euo pipefail

TARS_HOME="${TARS_HOME:-/opt/tars}"
source "$TARS_HOME/scripts/lib-alert.sh"
LOG_PREFIX="[exposure-monitor]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }

# Expected public-facing ports — adjust for your deployment
# Common: 22=SSH, 53=DNS, 80=HTTP, 443=HTTPS, 41641=Tailscale
EXPECTED_PUBLIC="${TARS_EXPECTED_PORTS:-22 80 443}"

ISSUES=""

# Get all listening TCP ports on public interfaces
# Exclude: loopback, RFC1918, Tailscale CGNAT (100.64-127.*), Tailscale IPv6 (fd7a:115c:a1e0)
PUBLIC_PORTS=$(ss -tlnp 2>/dev/null | grep -v '127.0.0.1\|172\.1[6-9]\.\|172\.2[0-9]\.\|172\.3[0-1]\.\|::1\|100\.6[4-9]\.\|100\.[7-9][0-9]\.\|100\.1[0-1][0-9]\.\|100\.12[0-7]\.\|fd7a:115c:a1e0' | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$' | sort -nu)

for port in $PUBLIC_PORTS; do
    expected=false
    for ep in $EXPECTED_PUBLIC; do
        if [ "$port" = "$ep" ]; then
            expected=true
            break
        fi
    done
    if [ "$expected" = "false" ]; then
        proc=$(ss -tlnp "sport = :$port" 2>/dev/null | tail -1 | grep -oP 'users:\(\("\K[^"]+' || echo "unknown")
        case "$proc" in
            tailscaled|tailscale*) continue ;;
        esac
        ISSUES="${ISSUES}\n- Port **$port** ($proc) — not in expected list"
    fi
done

if [ -n "$ISSUES" ]; then
    send_alert "SECURITY | Unexpected public port(s) detected$(echo -e "$ISSUES")

Expected ports: ${EXPECTED_PUBLIC}"
    log "Issues:$(echo -e "$ISSUES")"
else
    log "All public ports as expected"
fi
