#!/bin/bash
# health-audit.sh — Automated health + security baseline check for T.A.R.S
# Checks: service status, memory usage, disk, zombies, temp cleanup, journal rotation,
#          host security, public port exposure
# Alerts to Discord if issues found. Runs every 6 hours via timer.
set -euo pipefail

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$TARS_HOME/scripts/lib-alert.sh"
LOG_PREFIX="[health-audit]"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $LOG_PREFIX $1"; }

ISSUES=""

# 1. TARS v2 service running (system-level since service-account migration)
if ! systemctl is-active tars.service >/dev/null 2>&1; then
    ISSUES="${ISSUES}\n- **tars.service**: NOT RUNNING"
fi

# 2. Memory DB exists and is readable
DB_PATH="${TARS_DATA_DIR:-$TARS_HOME/data}/memory.db"
if [ -f "$DB_PATH" ]; then
    count=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories;" 2>/dev/null || echo "ERROR")
    if [ "$count" = "ERROR" ]; then
        ISSUES="${ISSUES}\n- **Memory DB**: exists but query failed"
    fi
else
    ISSUES="${ISSUES}\n- **Memory DB**: NOT FOUND at $DB_PATH"
fi

# 4. Disk usage > 80%
disk_pct=$(df / --output=pcent | tail -1 | tr -d ' %')
if [ "$disk_pct" -gt 80 ]; then
    ISSUES="${ISSUES}\n- **Disk**: ${disk_pct}% used"
fi

# 5. Memory usage > 85%
mem_pct=$(free | awk '/Mem:/ {printf "%.0f", ($2-$7)/$2*100}')
if [ "$mem_pct" -gt 85 ]; then
    ISSUES="${ISSUES}\n- **RAM**: ${mem_pct}% used"
fi

# 6. Swap usage > 70%
swap_pct=$(free | awk '/Swap:/ {if ($2>0) printf "%.0f", $3/$2*100; else print "0"}')
if [ "$swap_pct" -gt 70 ]; then
    ISSUES="${ISSUES}\n- **Swap**: ${swap_pct}% used"
fi

# 7. Zombie processes
zombies=$(ps aux | awk '$8 ~ /Z/' | wc -l)
if [ "$zombies" -gt 0 ]; then
    ISSUES="${ISSUES}\n- **Zombies**: $zombies zombie process(es)"
fi

# 8. Clean temp files older than 2 hours
# Scope to tars-owned files so `-delete` never hits files owned by other
# users — would exit non-zero under set -euo pipefail and kill the script.
cleaned=$(find /tmp -maxdepth 1 -name "tars-*" -type f -user tars -mmin +120 -delete -print 2>/dev/null | wc -l)
if [ "$cleaned" -gt 0 ]; then
    log "Cleaned $cleaned old temp files"
fi

# 9. Rotate journal if > 200MB
journal_mb=$(journalctl --disk-usage 2>/dev/null | grep -oP '\d+\.\d+M' | head -1 | tr -d 'M' || echo "0")
if [ "$(echo "$journal_mb > 200" | bc 2>/dev/null || echo 0)" = "1" ]; then
    journalctl --vacuum-size=100M >/dev/null 2>&1
    log "Rotated journal (was ${journal_mb}MB)"
fi

# 10. Load average check (> 3.0 on 4 CPU = concerning)
load=$(cat /proc/loadavg | awk '{print $1}')
if [ "$(echo "$load > 3.0" | bc 2>/dev/null || echo 0)" = "1" ]; then
    ISSUES="${ISSUES}\n- **Load**: $load (high for 4 CPU)"
fi

# 11. Host security baseline — cloud metadata must be blocked (cloud VPS only)
if curl -s --max-time 1 http://169.254.169.254/ >/dev/null 2>&1; then
    if ! grep -q '169.254.169.254' /etc/iptables/rules.v4 2>/dev/null; then
        ISSUES="${ISSUES}\n- **host**: cloud metadata iptables rule MISSING"
    fi
fi

# 12. Public port exposure — flag unexpected listeners
_resolve_expected_ports() {
    local cfg=""
    [ -n "${TARS_OVERLAY:-}" ] && [ -f "$TARS_OVERLAY/config/config.yaml" ] && cfg="$TARS_OVERLAY/config/config.yaml"
    [ -z "$cfg" ] && [ -f "$TARS_HOME/config/config.yaml" ] && cfg="$TARS_HOME/config/config.yaml"
    [ -n "$cfg" ] && "$TARS_HOME/.venv/bin/python" -c "
import yaml, sys
c = yaml.safe_load(open('$cfg'))
print(c.get('security', {}).get('expected_ports', ''), end='')
" 2>/dev/null || echo -n ""
}
EXPECTED_PUBLIC="${TARS_EXPECTED_PORTS:-$(_resolve_expected_ports)}"
EXPECTED_PUBLIC="${EXPECTED_PUBLIC:-22 80 443}"
PUBLIC_PORTS=$(ss -tlnp 2>/dev/null | grep -v '127\.\|172\.1[6-9]\.\|172\.2[0-9]\.\|172\.3[0-1]\.\|::1\|100\.6[4-9]\.\|100\.[7-9][0-9]\.\|100\.1[0-1][0-9]\.\|100\.12[0-7]\.\|fd7a:115c:a1e0' | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$' | sort -nu)
for port in $PUBLIC_PORTS; do
    expected=false
    for ep in $EXPECTED_PUBLIC; do
        [ "$port" = "$ep" ] && expected=true && break
    done
    if [ "$expected" = "false" ]; then
        proc=$(ss -tlnp "sport = :$port" 2>/dev/null | tail -1 | grep -oP 'users:\(\("\K[^"]+' || echo "unknown")
        case "$proc" in tailscaled|tailscale*) continue ;; esac
        ISSUES="${ISSUES}\n- **Port $port** ($proc) — not in expected list"
    fi
done

# Report
if [ -n "$ISSUES" ]; then
    send_alert "HEALTH | Issues detected:$(echo -e "$ISSUES")"
    log "Issues found:$(echo -e "$ISSUES")"
else
    log "All checks passed (disk=${disk_pct}%, ram=${mem_pct}%, swap=${swap_pct}%, load=${load})"
fi
