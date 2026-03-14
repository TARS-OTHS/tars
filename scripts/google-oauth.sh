#!/usr/bin/env bash
# google-oauth.sh — Run Google OAuth consent flow and store refresh token
# Usage: ./scripts/google-oauth.sh
#
# Reads client_id and client_secret from the encrypted vault,
# runs a local OAuth flow, and updates the MCP gateway config
# with the resulting refresh token.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARS_HOME="${TARS_HOME:-$(dirname "$SCRIPT_DIR")}"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
info()  { echo -e "${BLUE}  $1${RESET}"; }
ok()    { echo -e "${GREEN}✓ $1${RESET}"; }
warn()  { echo -e "${YELLOW}⚠ $1${RESET}"; }
err()   { echo -e "${RED}✗ $1${RESET}"; exit 1; }

# Source .env for paths
[[ -f "$TARS_HOME/.env" ]] && { set -a; source "$TARS_HOME/.env"; set +a; }

AGE_KEY_PATH="${AGE_KEY_PATH:-$TARS_HOME/.config/age/key.txt}"
VAULT_PATH="${SECRETS_VAULT_PATH:-$TARS_HOME/.secrets-vault/secrets.age}"
DOCKER_HOST_IP="${DOCKER_HOST_IP:-172.17.0.1}"
MCP_GATEWAY_PORT="${MCP_GATEWAY_PORT:-12008}"

[[ ! -f "$AGE_KEY_PATH" ]] && err "Age key not found at $AGE_KEY_PATH"
[[ ! -f "$VAULT_PATH" ]] && err "Vault not found at $VAULT_PATH"

# Decrypt vault and extract Google credentials
info "Reading Google credentials from vault..."
VAULT_JSON=$(age -d -i "$AGE_KEY_PATH" "$VAULT_PATH")
GOOGLE_CREDS=$(echo "$VAULT_JSON" | jq -r '.["secrets/google-mcp-credentials.json"] // empty')
[[ -z "$GOOGLE_CREDS" ]] && err "No Google credentials found in vault. Run setup.sh first."

CLIENT_ID=$(echo "$GOOGLE_CREDS" | jq -r '.client_id')
CLIENT_SECRET=$(echo "$GOOGLE_CREDS" | jq -r '.client_secret')
[[ -z "$CLIENT_ID" || "$CLIENT_ID" == "null" ]] && err "Google client_id is empty"

ok "Credentials loaded (client_id: ${CLIENT_ID:0:20}...)"

# OAuth consent flow
SCOPES="https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/documents"
REDIRECT_URI="urn:ietf:wg:oauth:2.0:oob"

AUTH_URL="https://accounts.google.com/o/oauth2/v2/auth?client_id=${CLIENT_ID}&redirect_uri=${REDIRECT_URI}&response_type=code&scope=$(echo "$SCOPES" | sed 's/ /%20/g')&access_type=offline&prompt=consent"

echo
echo "Open this URL in your browser to authorise Google access:"
echo
echo -e "${BLUE}${AUTH_URL}${RESET}"
echo
read -r -p "  Paste the authorisation code here: " AUTH_CODE
[[ -z "$AUTH_CODE" ]] && err "No authorisation code provided"

# Exchange code for tokens
info "Exchanging authorisation code for tokens..."
TOKEN_RESPONSE=$(curl -sf -X POST "https://oauth2.googleapis.com/token" \
    -d "code=${AUTH_CODE}" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "redirect_uri=${REDIRECT_URI}" \
    -d "grant_type=authorization_code" 2>/dev/null)

REFRESH_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.refresh_token // empty')
ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty')
ERROR=$(echo "$TOKEN_RESPONSE" | jq -r '.error // empty')

[[ -n "$ERROR" ]] && err "OAuth error: $ERROR — $(echo "$TOKEN_RESPONSE" | jq -r '.error_description // empty')"
[[ -z "$REFRESH_TOKEN" ]] && err "No refresh token received. Ensure prompt=consent is set."

ok "Refresh token obtained"

# Update vault with refresh token
info "Updating vault with refresh token..."
UPDATED_VAULT=$(echo "$VAULT_JSON" | jq \
    --arg ref "$REFRESH_TOKEN" \
    '.["secrets/google-mcp-credentials.json"].refresh_token = $ref')
echo "$UPDATED_VAULT" | age -r "$(grep 'public key' "$AGE_KEY_PATH" | awk '{print $NF}')" -o "$VAULT_PATH"
chmod 600 "$VAULT_PATH"
ok "Vault updated"

# Update MetaMCP Google server config if gateway is running
GATEWAY_URL="http://${DOCKER_HOST_IP}:${MCP_GATEWAY_PORT}"
if curl -sf "${GATEWAY_URL}/" >/dev/null 2>&1; then
    info "Updating MCP gateway Google config with refresh token..."
    # The exact API call depends on MetaMCP's update endpoint
    # For now, inform the user to update via the UI
    echo
    ok "MCP gateway is running at ${GATEWAY_URL}"
    info "Update the google-workspace server's GOOGLE_REFRESH_TOKEN env var"
    info "via the MetaMCP UI at ${GATEWAY_URL}"
else
    warn "MCP gateway not running — refresh token saved in vault only"
    info "Start services with: cd $TARS_HOME && docker compose up -d"
fi

echo
ok "Google OAuth setup complete"
echo "  Refresh token stored in encrypted vault"
echo "  Scopes: Gmail, Calendar, Drive, Sheets, Docs"
