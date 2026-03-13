#!/usr/bin/env bash
# add-agent.sh — Add a new persistent agent to TARS
# Called by T.A.R.S via exec, or manually.
#
# Usage: ./scripts/add-agent.sh --id sourcing --name "Sourcing Agent" --role specialist \
#          --domain "Product research, supplier discovery" --model <model-id> \
#          --channel "#sourcing"

set -euo pipefail

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; RESET='\033[0m'
info()    { echo -e "${GREEN}✓ $1${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $1${RESET}"; }
error()   { echo -e "${RED}✗ $1${RESET}"; exit 1; }

# ============================================================================
# Parse arguments
# ============================================================================

AGENT_ID=""
AGENT_NAME=""
AGENT_ROLE="specialist"
AGENT_DOMAIN=""
AGENT_MODEL=""
AGENT_CHANNEL=""
SOUL_TEXT=""
CAPABILITIES="web search,memory"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)         AGENT_ID="$2"; shift 2 ;;
        --name)       AGENT_NAME="$2"; shift 2 ;;
        --role)       AGENT_ROLE="$2"; shift 2 ;;
        --domain)     AGENT_DOMAIN="$2"; shift 2 ;;
        --model)      AGENT_MODEL="$2"; shift 2 ;;
        --channel)    AGENT_CHANNEL="$2"; shift 2 ;;
        --soul)       SOUL_TEXT="$2"; shift 2 ;;
        --capabilities) CAPABILITIES="$2"; shift 2 ;;
        *)            error "Unknown argument: $1" ;;
    esac
done

[[ -z "$AGENT_ID" ]]   && error "Missing required --id"
[[ -z "$AGENT_NAME" ]]  && error "Missing required --name"
[[ -z "$AGENT_DOMAIN" ]] && error "Missing required --domain"

# ============================================================================
# Resolve paths
# ============================================================================

TARS_HOME="${TARS_HOME:-/opt/tars}"
OC_CONFIG="$HOME/.openclaw/openclaw.json"
TEAM_FILE="$TARS_HOME/config/team.json"
OC_WORKSPACE_BASE="$HOME/.openclaw"
AGENT_WORKSPACE="$OC_WORKSPACE_BASE/workspaces/$AGENT_ID"

[[ ! -f "$OC_CONFIG" ]] && error "openclaw.json not found at $OC_CONFIG"
[[ ! -f "$TEAM_FILE" ]] && error "team.json not found at $TEAM_FILE"

# Check agent doesn't already exist
if jq -e ".agents.list[] | select(.id == \"$AGENT_ID\")" "$OC_CONFIG" >/dev/null 2>&1; then
    error "Agent '$AGENT_ID' already exists in openclaw.json"
fi

# ============================================================================
# 1. Create agent workspace
# ============================================================================

mkdir -p "$AGENT_WORKSPACE"

# SOUL.md — from --soul text, or from template
if [[ -n "$SOUL_TEXT" ]]; then
    cat > "$AGENT_WORKSPACE/SOUL.md" << EOF
# SOUL.md — ${AGENT_NAME}

${SOUL_TEXT}

---

_This file is yours to evolve. As you learn who you are, update it._
EOF
elif [[ -f "$TARS_HOME/templates/default/SOUL.md.tmpl" ]]; then
    sed "s/{{AGENT_NAME}}/$AGENT_NAME/g" "$TARS_HOME/templates/default/SOUL.md.tmpl" \
        > "$AGENT_WORKSPACE/SOUL.md"
else
    cat > "$AGENT_WORKSPACE/SOUL.md" << EOF
# SOUL.md — ${AGENT_NAME}

## Identity
- **Name:** ${AGENT_NAME}
- **Role:** ${AGENT_ROLE}
- **Domain:** ${AGENT_DOMAIN}

## Core Truths
Be genuinely helpful. Be direct. Be resourceful before asking.

---

_This file is yours to evolve. As you learn who you are, update it._
EOF
fi

info "Workspace created: $AGENT_WORKSPACE"

# IDENTITY.md
cat > "$AGENT_WORKSPACE/IDENTITY.md" << EOF
# IDENTITY.md — ${AGENT_NAME}

- **ID:** ${AGENT_ID}
- **Name:** ${AGENT_NAME}
- **Role:** ${AGENT_ROLE}
- **Domain:** ${AGENT_DOMAIN}
- **Model:** ${AGENT_MODEL}
EOF

# Copy AGENTS.md and MEMORY.md templates if available
DOCKER_HOST_IP="${DOCKER_HOST_IP:-172.17.0.1}"
for tmpl in AGENTS.md MEMORY.md; do
    if [[ -f "$TARS_HOME/templates/$tmpl" ]]; then
        sed -e "s|DOCKER_HOST_IP|${DOCKER_HOST_IP}|g" \
            -e "s|{{AGENT_ID}}|${AGENT_ID}|g" \
            -e "s|DASHBOARD_API_PORT|${DASHBOARD_API_PORT:-8766}|g" \
            "$TARS_HOME/templates/$tmpl" > "$AGENT_WORKSPACE/$tmpl"
    fi
done

# TOOLS.md — copy from main workspace as baseline (agent can evolve it)
MAIN_WORKSPACE="$OC_WORKSPACE_BASE/workspace"
if [[ -f "$MAIN_WORKSPACE/TOOLS.md" ]]; then
    cp "$MAIN_WORKSPACE/TOOLS.md" "$AGENT_WORKSPACE/TOOLS.md"
fi

# Generate initial MEMORY_CONTEXT.md if regen script exists
if [[ -f "$TARS_HOME/scripts/regen-memory-context.sh" ]]; then
    DOCKER_HOST_IP="${DOCKER_HOST_IP}" OC_WORKSPACE="${AGENT_WORKSPACE}" \
        "$TARS_HOME/scripts/regen-memory-context.sh" 2>/dev/null || true
    info "MEMORY_CONTEXT.md generated"
fi

info "Workspace files written"

# ============================================================================
# 2. Add agent to openclaw.json agents.list
# ============================================================================

# Build the agent entry
AGENT_ENTRY=$(jq -n \
    --arg id "$AGENT_ID" \
    --arg workspace "$AGENT_WORKSPACE" \
    '{id: $id, workspace: $workspace}')

# Insert into agents.list array
jq ".agents.list += [$AGENT_ENTRY]" "$OC_CONFIG" > "${OC_CONFIG}.tmp" \
    && mv "${OC_CONFIG}.tmp" "$OC_CONFIG"
chmod 600 "$OC_CONFIG"

info "Added '$AGENT_ID' to openclaw.json agents.list"

# ============================================================================
# 3. Add agent to exec-approvals.json
# ============================================================================

EXEC_FILE="$HOME/.openclaw/exec-approvals.json"
if [[ -f "$EXEC_FILE" ]]; then
    jq ".agents[\"$AGENT_ID\"] = {\"security\": \"full\", \"ask\": \"off\", \"autoAllowSkills\": true}" \
        "$EXEC_FILE" > "${EXEC_FILE}.tmp" \
        && mv "${EXEC_FILE}.tmp" "$EXEC_FILE"
    info "Added '$AGENT_ID' to exec-approvals.json"
fi

# ============================================================================
# 4. Add agent to team.json
# ============================================================================

# Build capabilities array from comma-separated string
CAPS_JSON=$(echo "$CAPABILITIES" | tr ',' '\n' | jq -R . | jq -s .)

TEAM_ENTRY=$(jq -n \
    --arg id "$AGENT_ID" \
    --arg name "$AGENT_NAME" \
    --arg role "$AGENT_ROLE" \
    --arg domain "$AGENT_DOMAIN" \
    --arg model "$AGENT_MODEL" \
    --arg channel "$AGENT_CHANNEL" \
    --argjson caps "$CAPS_JSON" \
    '{id: $id, name: $name, type: "agent", role: $role, domain: $domain, model: $model, channel: $channel, capabilities: $caps}')

# Only add if not already in team.json
if ! jq -e ".agents[] | select(.id == \"$AGENT_ID\")" "$TEAM_FILE" >/dev/null 2>&1; then
    jq ".agents += [$TEAM_ENTRY]" "$TEAM_FILE" > "${TEAM_FILE}.tmp" \
        && mv "${TEAM_FILE}.tmp" "$TEAM_FILE"
    info "Added '$AGENT_ID' to team.json"
else
    warn "Agent '$AGENT_ID' already in team.json — skipped"
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
info "Agent '${AGENT_NAME}' (${AGENT_ID}) is ready"
echo "  Workspace: $AGENT_WORKSPACE"
echo "  Mention @${AGENT_NAME} in Discord to talk to it"
