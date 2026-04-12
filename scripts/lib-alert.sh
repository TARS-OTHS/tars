#!/bin/bash
# Shared alert helper for T.A.R.S v2 cron scripts.
# Sources Discord bot token from Fernet vault via Python helper.
# Usage: source /opt/tars/scripts/lib-alert.sh

TARS_HOME="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
TARS_OVERLAY="${TARS_OVERLAY:-}"
TARS_VENV="$TARS_HOME/.venv/bin/python"

# Read alert channel from config.yaml (single source of truth)
_resolve_alert_channel() {
    local cfg=""
    [ -n "$TARS_OVERLAY" ] && [ -f "$TARS_OVERLAY/config/config.yaml" ] && cfg="$TARS_OVERLAY/config/config.yaml"
    [ -z "$cfg" ] && [ -f "$TARS_HOME/config/config.yaml" ] && cfg="$TARS_HOME/config/config.yaml"
    [ -n "$cfg" ] && "$TARS_VENV" -c "
import yaml, sys
c = yaml.safe_load(open('$cfg'))
print(c.get('security', {}).get('alert_channel', ''), end='')
" 2>/dev/null || echo -n ""
}
ALERT_CHANNEL="${TARS_ALERT_CHANNEL:-$(_resolve_alert_channel)}"

# Resolve secrets.enc — overlay takes priority over core
_SECRETS_ENC="$TARS_HOME/config/secrets.enc"
if [ -n "$TARS_OVERLAY" ] && [ -f "$TARS_OVERLAY/config/secrets.enc" ]; then
    _SECRETS_ENC="$TARS_OVERLAY/config/secrets.enc"
fi

_get_bot_token() {
    "$TARS_VENV" -c "
from src.vault.fernet import FernetVault
from pathlib import Path
v = FernetVault('$_SECRETS_ENC')
v.unlock(Path.home().joinpath('.config/tars-vault-key').read_text().strip())
print(v.get('active-discord-token') or v.get('discord-token') or '', end='')
" 2>/dev/null
}

# Cache token for the script's lifetime
_BOT_TOKEN=""

send_alert() {
    local msg="$1"
    if [ -z "$_BOT_TOKEN" ]; then
        _BOT_TOKEN=$(_get_bot_token)
    fi
    if [ -z "$_BOT_TOKEN" ] || [ -z "$ALERT_CHANNEL" ]; then
        echo "ALERT (no Discord): $msg" >&2
        return
    fi
    curl -s -X POST "https://discord.com/api/v10/channels/$ALERT_CHANNEL/messages" \
        -H "Authorization: Bot $_BOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg content "$msg" '{content: $content}')" > /dev/null 2>&1 || true
}
