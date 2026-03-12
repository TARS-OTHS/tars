#!/usr/bin/env bash
# tars-reauth — Re-import Claude auth token from OpenClaw into TARS secrets.
#
# Run this after 'openclaw setup' to refresh the token, or when you get
# auth failure alerts. Tests the connection before confirming.
#
# Usage: ./scripts/tars-reauth.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARS_HOME="${TARS_HOME:-$(dirname "$SCRIPT_DIR")}"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
print_success() { echo -e "${GREEN}✓ $1${RESET}"; }
print_warn()    { echo -e "${YELLOW}⚠ $1${RESET}"; }
print_error()   { echo -e "${RED}✗ $1${RESET}"; }
print_info()    { echo -e "  $1"; }

echo -e "${BLUE}TARS — Re-authenticate Claude${RESET}"
echo

# Find OC auth-profiles.json
auth_file=""
for candidate in \
    "$HOME/.openclaw/agents/main/agent/auth-profiles.json" \
    "$HOME/.openclaw/auth-profiles.json" \
    "$HOME/.openclaw/agents/default/agent/auth-profiles.json"; do
    if [[ -f "$candidate" ]]; then
        auth_file="$candidate"
        break
    fi
done

if [[ -z "$auth_file" ]]; then
    print_error "OpenClaw auth-profiles.json not found"
    echo
    echo "  Run 'openclaw setup' first to authenticate with Claude,"
    echo "  then run this script again."
    exit 1
fi

print_info "Found OC auth at: $auth_file"

# Extract token
token=$(jq -r '
    .profiles["anthropic:manual"].token //
    .profiles["anthropic:default"].token //
    .profiles["anthropic:default"].key //
    empty
' "$auth_file" 2>/dev/null || true)

if [[ -z "$token" ]]; then
    print_error "No Anthropic token found in OpenClaw auth"
    echo
    echo "  Run 'openclaw setup' and complete the Claude authentication step."
    exit 1
fi

# Write to TARS secrets
mkdir -p "$TARS_HOME/.secrets"
echo "$token" > "$TARS_HOME/.secrets/claude-token"
chmod 600 "$TARS_HOME/.secrets/claude-token"

if [[ "$token" == sk-ant-oat* ]]; then
    print_success "Token imported (OAuth / Max subscription)"
elif [[ "$token" == sk-ant-api* ]]; then
    print_success "Token imported (API key)"
else
    print_warn "Token imported (unknown format)"
fi

# Test connection
echo -n "  Testing Claude connection..."
if [[ "$token" == sk-ant-oat* ]]; then
    status=$(curl -sf -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $token" \
        -H "anthropic-version: 2023-06-01" \
        -H "anthropic-beta: oauth-2025-04-20" \
        -H "content-type: application/json" \
        -d '{"model":"claude-sonnet-4-20250514","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
        https://api.anthropic.com/v1/messages 2>/dev/null || echo "000")
else
    status=$(curl -sf -o /dev/null -w "%{http_code}" \
        -H "x-api-key: $token" \
        -H "anthropic-version: 2023-06-01" \
        -H "content-type: application/json" \
        -d '{"model":"claude-sonnet-4-20250514","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
        https://api.anthropic.com/v1/messages 2>/dev/null || echo "000")
fi

if [[ "$status" == "200" ]]; then
    print_success "Claude connection verified — you're good"
elif [[ "$status" == "401" || "$status" == "403" ]]; then
    print_error "Auth failed (HTTP $status)"
    echo "  Token may be expired. Run 'openclaw setup' to re-authenticate."
    exit 1
else
    print_warn "Got HTTP $status — may still work, monitor logs"
fi

echo
print_success "Done. TARS services will pick up the new token automatically."
