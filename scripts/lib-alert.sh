#!/bin/bash
# Shared alert helper for T.A.R.S v2 cron scripts.
# Sources Discord bot token from Fernet vault via Python helper.
# Usage: source /opt/tars/scripts/lib-alert.sh

TARS_HOME="${TARS_HOME:-/opt/tars}"
TARS_VENV="$TARS_HOME/.venv/bin/python"
ALERT_CHANNEL="${TARS_ALERT_CHANNEL:-}"  # set via config/channels.env

_get_bot_token() {
    "$TARS_VENV" -c "
from src.vault.fernet import FernetVault
from pathlib import Path
v = FernetVault('$TARS_HOME/config/secrets.enc')
v.unlock(Path.home().joinpath('.config/tars-vault-key').read_text().strip())
print(v.get('discord-token') or '', end='')
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
