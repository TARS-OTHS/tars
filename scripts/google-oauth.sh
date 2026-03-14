#!/usr/bin/env bash
# google-oauth.sh — Run Google OAuth consent flow and store refresh token
# Usage: ./scripts/google-oauth.sh
#
# Reads client_id and client_secret from the encrypted vault,
# starts a temporary local HTTP server to capture the OAuth redirect,
# and updates the vault with the resulting refresh token.

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
OAUTH_PORT="${OAUTH_PORT:-8844}"

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

# OAuth consent flow using localhost redirect
SCOPES="https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/documents"
REDIRECT_URI="http://localhost:${OAUTH_PORT}"

AUTH_URL="https://accounts.google.com/o/oauth2/v2/auth?client_id=${CLIENT_ID}&redirect_uri=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${REDIRECT_URI}'))")&response_type=code&scope=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${SCOPES}'))")&access_type=offline&prompt=consent"

# Check if we can use local browser or need manual flow
echo
echo "  Two options to complete Google authorisation:"
echo
echo "  Option A: If you have browser access to this machine (SSH tunnel or local):"
echo "    1. Open the URL below in your browser"
echo "    2. Authorise the app"
echo "    3. The browser will redirect to localhost:${OAUTH_PORT} — the script catches it automatically"
echo
echo "  Option B: If this is a remote VPS with no browser:"
echo "    1. Set up an SSH tunnel first:  ssh -L ${OAUTH_PORT}:localhost:${OAUTH_PORT} <this-server>"
echo "    2. Open the URL below in YOUR LOCAL browser"
echo "    3. After authorising, the redirect hits your tunnel → this script catches it"
echo
echo -e "  ${BLUE}${AUTH_URL}${RESET}"
echo

# Start a temporary HTTP server to catch the OAuth redirect
AUTH_CODE_FILE=$(mktemp)
trap "rm -f $AUTH_CODE_FILE" EXIT

# Python HTTP server that captures the auth code from the redirect
python3 -c "
import http.server, urllib.parse, sys

class OAuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get('code', [None])[0]
        error = params.get('error', [None])[0]
        if error:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(f'<h1>Error: {error}</h1><p>You can close this tab.</p>'.encode())
            with open('$AUTH_CODE_FILE', 'w') as f:
                f.write(f'ERROR:{error}')
        elif code:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>Authorised!</h1><p>You can close this tab. Return to the terminal.</p>')
            with open('$AUTH_CODE_FILE', 'w') as f:
                f.write(code)
        else:
            self.send_response(400)
            self.end_headers()
            return
        # Shutdown after handling
        import threading
        threading.Thread(target=self.server.shutdown).start()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs

server = http.server.HTTPServer(('127.0.0.1', ${OAUTH_PORT}), OAuthHandler)
print('Waiting for Google authorisation callback on port ${OAUTH_PORT}...')
server.handle_request()
server.server_close()
" &
PYTHON_PID=$!

# Wait for the callback
info "Waiting for authorisation (press Ctrl+C to cancel)..."
wait $PYTHON_PID 2>/dev/null || true

AUTH_CODE=$(cat "$AUTH_CODE_FILE" 2>/dev/null || echo "")
[[ -z "$AUTH_CODE" ]] && err "No authorisation code received"
[[ "$AUTH_CODE" == ERROR:* ]] && err "OAuth error: ${AUTH_CODE#ERROR:}"

ok "Authorisation code received"

# Exchange code for tokens
info "Exchanging authorisation code for tokens..."
TOKEN_RESPONSE=$(curl -sf -X POST "https://oauth2.googleapis.com/token" \
    -d "code=${AUTH_CODE}" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "redirect_uri=${REDIRECT_URI}" \
    -d "grant_type=authorization_code" 2>/dev/null || echo '{}')

REFRESH_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.refresh_token // empty')
ERROR=$(echo "$TOKEN_RESPONSE" | jq -r '.error // empty')

[[ -n "$ERROR" ]] && err "OAuth error: $ERROR — $(echo "$TOKEN_RESPONSE" | jq -r '.error_description // empty')"
[[ -z "$REFRESH_TOKEN" ]] && err "No refresh token received. Full response: $TOKEN_RESPONSE"

ok "Refresh token obtained"

# Update vault with refresh token
info "Updating vault with refresh token..."
UPDATED_VAULT=$(echo "$VAULT_JSON" | jq \
    --arg ref "$REFRESH_TOKEN" \
    '.["secrets/google-mcp-credentials.json"].refresh_token = $ref')

AGE_PUBKEY=$(grep 'public key' "$AGE_KEY_PATH" | awk '{print $NF}')
echo "$UPDATED_VAULT" | age -r "$AGE_PUBKEY" -o "$VAULT_PATH"
chmod 600 "$VAULT_PATH"
ok "Vault updated"

# Update MetaMCP Google server config if gateway is running
GATEWAY_URL="http://${DOCKER_HOST_IP}:${MCP_GATEWAY_PORT}"
if curl -sf "${GATEWAY_URL}/" >/dev/null 2>&1; then
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
