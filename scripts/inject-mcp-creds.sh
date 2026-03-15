#!/usr/bin/env bash
# inject-mcp-creds.sh — Decrypt MCP credentials from vault into tmpfs (RAM-only)
#
# Writes Google MCP credential files to /run/tars/mcp-creds/ which is a tmpfs
# mount (RAM-only, never hits disk). Called at service startup and after re-auth.
#
# Usage: ./scripts/inject-mcp-creds.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARS_HOME="${TARS_HOME:-$(dirname "$SCRIPT_DIR")}"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
info()  { echo -e "${BLUE}  $1${RESET}"; }
ok()    { echo -e "${GREEN}✓ $1${RESET}"; }
warn()  { echo -e "${YELLOW}⚠ $1${RESET}"; }
err()   { echo -e "${RED}✗ $1${RESET}"; exit 1; }

[[ -f "$TARS_HOME/.env" ]] && { set -a; source "$TARS_HOME/.env"; set +a; }

AGE_KEY_PATH="${AGE_KEY_PATH:-$TARS_HOME/.config/age/key.txt}"
VAULT_PATH="${SECRETS_VAULT_PATH:-$TARS_HOME/.secrets-vault/secrets.age}"
MCP_CREDS_DIR="/run/tars/mcp-creds"

[[ ! -f "$AGE_KEY_PATH" ]] && err "Age key not found at $AGE_KEY_PATH"
[[ ! -f "$VAULT_PATH" ]] && err "Vault not found at $VAULT_PATH"

# Ensure tmpfs mount exists
if ! mountpoint -q "$MCP_CREDS_DIR" 2>/dev/null; then
    mkdir -p "$MCP_CREDS_DIR"
    mount -t tmpfs -o size=1M,mode=0755 tmpfs "$MCP_CREDS_DIR"
    ok "tmpfs mounted at $MCP_CREDS_DIR"
fi

# Decrypt vault
VAULT_JSON=$(age -d -i "$AGE_KEY_PATH" "$VAULT_PATH")
GOOGLE_CREDS=$(echo "$VAULT_JSON" | jq -r '.["secrets/google-mcp-credentials.json"] // empty')

if [[ -z "$GOOGLE_CREDS" ]]; then
    warn "No Google MCP credentials in vault — skipping"
    exit 0
fi

CLIENT_ID=$(echo "$GOOGLE_CREDS" | jq -r '.client_id')
CLIENT_SECRET=$(echo "$GOOGLE_CREDS" | jq -r '.client_secret')
REFRESH_TOKEN=$(echo "$GOOGLE_CREDS" | jq -r '.refresh_token // empty')

[[ -z "$CLIENT_ID" || "$CLIENT_ID" == "null" ]] && err "Google client_id is empty"

# The @pegasusheavy/google-mcp server creates a google-mcp/ subdirectory
# inside XDG_CONFIG_HOME and XDG_DATA_HOME
CONFIG_DIR="$MCP_CREDS_DIR/config/google-mcp"
DATA_DIR="$MCP_CREDS_DIR/data/google-mcp"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"

# Write credentials (RAM-only, never hits disk)
echo "{\"installed\":{\"client_id\":\"${CLIENT_ID}\",\"client_secret\":\"${CLIENT_SECRET}\",\"redirect_uris\":[\"http://localhost\"]}}" \
    > "$CONFIG_DIR/credentials.json"
chmod 644 "$CONFIG_DIR/credentials.json"

if [[ -n "$REFRESH_TOKEN" ]]; then
    SCOPES="https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/documents"
    echo "{\"access_token\":\"\",\"refresh_token\":\"${REFRESH_TOKEN}\",\"scope\":\"${SCOPES}\",\"token_type\":\"Bearer\",\"expiry_date\":0}" \
        > "$DATA_DIR/tokens.json"
    chmod 644 "$DATA_DIR/tokens.json"
    ok "Google MCP credentials injected into tmpfs (RAM-only)"
else
    warn "No refresh token — credentials.json written but tokens.json skipped"
    info "Run: $TARS_HOME/scripts/google-oauth.sh to complete OAuth"
fi
