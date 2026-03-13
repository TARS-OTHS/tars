#!/usr/bin/env bash
# remove-agent.sh — Remove a persistent agent from TARS
# Archives the workspace instead of deleting it.
#
# Usage: ./scripts/remove-agent.sh --id sourcing [--purge]

set -euo pipefail

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; RESET='\033[0m'
info()    { echo -e "${GREEN}✓ $1${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $1${RESET}"; }
error()   { echo -e "${RED}✗ $1${RESET}"; exit 1; }

AGENT_ID=""
PURGE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)    AGENT_ID="$2"; shift 2 ;;
        --purge) PURGE=true; shift ;;
        *)       error "Unknown argument: $1" ;;
    esac
done

[[ -z "$AGENT_ID" ]] && error "Missing required --id"
[[ "$AGENT_ID" == "main" ]] && error "Cannot remove the main agent"

TARS_HOME="${TARS_HOME:-/opt/tars}"
OC_CONFIG="$HOME/.openclaw/openclaw.json"
TEAM_FILE="$TARS_HOME/config/team.json"
OC_WORKSPACE_BASE="$HOME/.openclaw"
AGENT_WORKSPACE="$OC_WORKSPACE_BASE/workspaces/$AGENT_ID"
ARCHIVE_DIR="$TARS_HOME/archive/agents"

[[ ! -f "$OC_CONFIG" ]] && error "openclaw.json not found at $OC_CONFIG"

# ============================================================================
# 1. Remove from openclaw.json agents.list
# ============================================================================

if jq -e ".agents.list[] | select(.id == \"$AGENT_ID\")" "$OC_CONFIG" >/dev/null 2>&1; then
    jq "del(.agents.list[] | select(.id == \"$AGENT_ID\"))" "$OC_CONFIG" > "${OC_CONFIG}.tmp" \
        && mv "${OC_CONFIG}.tmp" "$OC_CONFIG"
    chmod 600 "$OC_CONFIG"
    info "Removed '$AGENT_ID' from openclaw.json"
else
    warn "Agent '$AGENT_ID' not found in openclaw.json"
fi

# ============================================================================
# 2. Remove from exec-approvals.json
# ============================================================================

EXEC_FILE="$HOME/.openclaw/exec-approvals.json"
if [[ -f "$EXEC_FILE" ]] && jq -e ".agents[\"$AGENT_ID\"]" "$EXEC_FILE" >/dev/null 2>&1; then
    jq "del(.agents[\"$AGENT_ID\"])" "$EXEC_FILE" > "${EXEC_FILE}.tmp" \
        && mv "${EXEC_FILE}.tmp" "$EXEC_FILE"
    info "Removed '$AGENT_ID' from exec-approvals.json"
fi

# ============================================================================
# 3. Remove from team.json
# ============================================================================

if [[ -f "$TEAM_FILE" ]] && jq -e ".agents[] | select(.id == \"$AGENT_ID\")" "$TEAM_FILE" >/dev/null 2>&1; then
    jq "del(.agents[] | select(.id == \"$AGENT_ID\"))" "$TEAM_FILE" > "${TEAM_FILE}.tmp" \
        && mv "${TEAM_FILE}.tmp" "$TEAM_FILE"
    info "Removed '$AGENT_ID' from team.json"
else
    warn "Agent '$AGENT_ID' not found in team.json"
fi

# ============================================================================
# 4. Archive or purge workspace
# ============================================================================

if [[ -d "$AGENT_WORKSPACE" ]]; then
    if $PURGE; then
        rm -rf "$AGENT_WORKSPACE"
        info "Workspace deleted: $AGENT_WORKSPACE"
    else
        mkdir -p "$ARCHIVE_DIR"
        ARCHIVE_NAME="${AGENT_ID}_$(date +%Y%m%d_%H%M%S)"
        mv "$AGENT_WORKSPACE" "$ARCHIVE_DIR/$ARCHIVE_NAME"
        info "Workspace archived: $ARCHIVE_DIR/$ARCHIVE_NAME"
    fi
else
    warn "No workspace found at $AGENT_WORKSPACE"
fi

# ============================================================================
# 5. Restart gateway
# ============================================================================

echo ""
if command -v openclaw &>/dev/null; then
    openclaw gateway restart 2>/dev/null && info "Gateway restarted" || warn "Gateway restart failed — may need manual restart"
else
    warn "openclaw CLI not found — restart gateway manually"
fi

echo ""
info "Agent '$AGENT_ID' removed"
