#!/usr/bin/env bash
# TARS — Trusted Agent Runtime Stack — Setup Wizard
# Run this on a fresh Ubuntu 22.04/24.04 VPS to deploy a working TARS instance.
# Usage: ./setup.sh [--non-interactive]

set -euo pipefail

TARS_VERSION="0.1.0-alpha"
OC_GATEWAY_PORT="${OC_GATEWAY_PORT:-18789}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Colours ---
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
print_header()  { echo -e "\n${BLUE}=== $1 ===${RESET}"; }
print_success() { echo -e "${GREEN}✓ $1${RESET}"; }
print_warn()    { echo -e "${YELLOW}⚠ $1${RESET}"; }
print_error()   { echo -e "${RED}✗ $1${RESET}"; }
print_info()    { echo -e "  $1"; }

# --- Non-interactive mode ---
NON_INTERACTIVE=false
[[ "${1:-}" == "--non-interactive" ]] && NON_INTERACTIVE=true

ask() {
    local prompt="$1" default="${2:-}" val
    if $NON_INTERACTIVE; then echo "${default}"; return; fi
    read -r -p "  $prompt [${default}]: " val
    echo "${val:-$default}"
}

ask_secret() {
    local prompt="$1" val
    if $NON_INTERACTIVE; then echo "${2:-}"; return; fi
    read -r -s -p "  $prompt: " val; echo
    echo "$val"
}

ask_yn() {
    local prompt="$1" default="${2:-y}" val
    if $NON_INTERACTIVE; then echo "$default"; return; fi
    read -r -p "  $prompt [${default}]: " val
    echo "${val:-$default}"
}

# ============================================================
# SECTION 1: PREREQUISITES
# ============================================================
check_prerequisites() {
    print_header "Section 1/6 — Prerequisites"
    local missing=() can_install=()

    check_cmd() {
        local cmd="$1" pkg="${2:-$1}"
        if command -v "$cmd" &>/dev/null; then
            print_success "$cmd found"
        else
            print_warn "$cmd not found"
            missing+=("$cmd")
            can_install+=("$pkg")
        fi
    }

    check_cmd docker docker.io
    check_cmd git git
    check_cmd node nodejs
    check_cmd age age
    check_cmd curl curl
    check_cmd jq jq

    # Docker Compose (v2 plugin or standalone)
    if docker compose version &>/dev/null 2>&1; then
        print_success "docker compose found"
    elif command -v docker-compose &>/dev/null; then
        print_success "docker-compose found (v1)"
    else
        print_warn "docker compose not found"
        missing+=("docker-compose-plugin")
        can_install+=("docker-compose-plugin")
    fi

    # Node version check
    if command -v node &>/dev/null; then
        local node_ver
        node_ver=$(node --version | sed 's/v//' | cut -d. -f1)
        if [[ "$node_ver" -lt 22 ]]; then
            print_warn "Node.js v$node_ver found — v22+ required"
            missing+=("nodejs-22+")
        fi
    fi

    # RAM check
    local ram_gb
    ram_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
    if [[ "$ram_gb" -ge 2 ]]; then
        print_success "RAM: ${ram_gb}GB"
    else
        print_warn "RAM: ${ram_gb}GB — 2GB minimum recommended"
    fi

    # Disk check
    local disk_gb
    disk_gb=$(df -BG "$SCRIPT_DIR" | awk 'NR==2 {print $4}' | tr -d 'G')
    if [[ "$disk_gb" -ge 20 ]]; then
        print_success "Disk: ${disk_gb}GB free"
    else
        print_warn "Disk: ${disk_gb}GB free — 20GB recommended"
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo
        print_warn "Missing: ${missing[*]}"
        local ans
        ans=$(ask_yn "Attempt to install missing packages via apt-get?" "y")
        if [[ "$ans" =~ ^[Yy] ]]; then
            sudo apt-get update -qq
            for pkg in "${can_install[@]}"; do
                echo -n "  Installing $pkg... "
                sudo apt-get install -y -qq "$pkg" 2>/dev/null && echo "done" || echo "failed"
            done
        else
            print_error "Install missing packages and re-run setup.sh"
            exit 1
        fi
    fi

    print_success "Prerequisites OK"
}

# ============================================================
# SECTION 2: BASICS
# ============================================================
collect_basics() {
    print_header "Section 2/6 — Basic Configuration"

    OWNER_NAME=$(ask "Your name" "${OWNER_NAME:-}")
    [[ -z "$OWNER_NAME" ]] && { print_error "Owner name is required"; exit 1; }

    # Auto-detect timezone
    local detected_tz=""
    detected_tz=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "UTC")
    TIMEZONE=$(ask "Timezone" "$detected_tz")

    echo "  Deployment purpose:"
    echo "    1) Personal assistant"
    echo "    2) Business assistant"
    echo "    3) Development / testing"
    local purpose_choice
    purpose_choice=$(ask "Choice" "1")
    case "$purpose_choice" in
        2) DEPLOYMENT_PURPOSE="business" ;;
        3) DEPLOYMENT_PURPOSE="development" ;;
        *)  DEPLOYMENT_PURPOSE="personal" ;;
    esac

    print_success "Basics configured"
}

# ============================================================
# SECTION 3: OPENCLAW GATEWAY
# ============================================================
# OpenClaw handles Claude auth (Max subscription or API key),
# messaging platform setup (Discord/Slack/Telegram/etc.),
# model selection, and agent lifecycle.
#
# TARS services use OC's Chat Completions endpoint for LLM
# access (memory summaries, future use cases). A health check
# monitors the endpoint and alerts if OC updates break it.
# ============================================================
setup_openclaw() {
    print_header "Section 3/6 — OpenClaw Gateway"

    # --- Install OpenClaw ---
    if command -v openclaw &>/dev/null; then
        local oc_ver
        oc_ver=$(openclaw --version 2>/dev/null || echo "unknown")
        print_success "OpenClaw found ($oc_ver)"
    else
        echo
        print_info "Installing OpenClaw..."
        print_info "This will install OpenClaw and any Node.js version it requires."
        echo
        curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard
        echo

        if command -v openclaw &>/dev/null; then
            print_success "OpenClaw installed"
        else
            # Check common install locations
            for p in "$HOME/.npm-global/bin" "$HOME/.local/bin" "/usr/local/bin"; do
                if [[ -x "$p/openclaw" ]]; then
                    export PATH="$p:$PATH"
                    break
                fi
            done
            if ! command -v openclaw &>/dev/null; then
                print_error "OpenClaw installation failed"
                echo "  Install manually: https://docs.openclaw.ai/install"
                exit 1
            fi
            print_success "OpenClaw installed"
        fi
    fi

    # --- Onboard (Claude auth + messaging) ---
    local oc_config="$HOME/.openclaw/openclaw.json"
    if [[ -f "$oc_config" ]]; then
        print_success "OpenClaw config found"
        echo
        local ans
        ans=$(ask_yn "Re-run OpenClaw onboarding? (N = keep existing config)" "n")
        if [[ "$ans" =~ ^[Yy] ]]; then
            echo
            print_info "Launching OpenClaw onboarding..."
            print_info "This configures: Claude connection, messaging platform, model selection"
            echo
            openclaw onboard --install-daemon
        fi
    else
        echo
        print_info "OpenClaw needs initial configuration."
        print_info "This will set up your Claude connection, messaging platform, and model."
        echo
        openclaw onboard --install-daemon
    fi

    # Verify config exists after onboarding
    if [[ ! -f "$oc_config" ]]; then
        print_error "OpenClaw config not found after onboarding"
        echo "  Run 'openclaw onboard --install-daemon' manually, then re-run this script"
        exit 1
    fi

    print_success "OpenClaw configured"

    # --- Enable Chat Completions endpoint for TARS services ---
    enable_gateway_api

    # --- Set up ops alerts channel ---
    setup_ops_alerts
}

enable_gateway_api() {
    echo
    print_info "Enabling OpenClaw gateway API for TARS services..."

    # Enable the OpenAI-compatible chat completions endpoint
    openclaw config set gateway.http.endpoints.chatCompletions.enabled true --json 2>/dev/null || {
        print_warn "Could not enable chat completions endpoint via CLI"
        print_info "Add this to your openclaw.json manually:"
        print_info '  "gateway": { "http": { "endpoints": { "chatCompletions": { "enabled": true } } } }'
    }

    # Read or generate the gateway auth token
    OC_GATEWAY_TOKEN=$(openclaw config get gateway.auth.token 2>/dev/null | tr -d '"' || true)
    if [[ -z "$OC_GATEWAY_TOKEN" || "$OC_GATEWAY_TOKEN" == "null" ]]; then
        # Check env var
        OC_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-}"
    fi
    if [[ -z "$OC_GATEWAY_TOKEN" ]]; then
        # Generate a token for the gateway
        OC_GATEWAY_TOKEN=$(openssl rand -hex 32 2>/dev/null || head -c 64 /dev/urandom | base64 | tr -d '/+=' | head -c 64)
        openclaw config set gateway.auth.token "\"$OC_GATEWAY_TOKEN\"" --json 2>/dev/null || {
            print_warn "Could not set gateway token via CLI — set OPENCLAW_GATEWAY_TOKEN in your environment"
        }
    fi

    # Detect gateway port
    OC_GATEWAY_PORT=$(openclaw config get gateway.port 2>/dev/null | tr -d '"' || echo "18789")
    [[ "$OC_GATEWAY_PORT" == "null" || -z "$OC_GATEWAY_PORT" ]] && OC_GATEWAY_PORT=18789

    # Store the LLM endpoint URL for TARS services
    OC_LLM_URL="http://localhost:${OC_GATEWAY_PORT}/v1/chat/completions"

    print_success "Gateway API enabled on port $OC_GATEWAY_PORT"

    # Test the endpoint (gateway may not be running yet during first setup)
    echo -n "  Testing gateway connection..."
    local status
    status=$(curl -sf -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $OC_GATEWAY_TOKEN" \
        "http://localhost:${OC_GATEWAY_PORT}/v1/models" 2>/dev/null || echo "000")

    if [[ "$status" == "200" ]]; then
        print_success "Gateway API responding"
    elif [[ "$status" == "000" ]]; then
        print_warn "Gateway not running yet — will start after deployment"
        print_info "Run 'openclaw gateway' or 'systemctl --user start openclaw-gateway' to start it"
    else
        print_warn "Gateway returned HTTP $status — check 'openclaw doctor'"
    fi
}

setup_ops_alerts() {
    echo
    print_info "TARS posts system alerts (LLM connection failures, health issues) to a channel."
    echo "  Provide the channel ID where TARS should post alerts."
    echo "  (You can find this in Discord/Slack by right-clicking a channel → Copy ID)"
    echo
    OPS_ALERTS_CHANNEL=$(ask "Ops alerts channel ID (Enter to skip)" "")

    if [[ -n "${OPS_ALERTS_CHANNEL:-}" ]]; then
        print_success "Ops alerts channel: ${OPS_ALERTS_CHANNEL}"
    else
        print_warn "No ops-alerts channel set — alerts will only appear in logs"
    fi
}

# ============================================================
# SECTION 4: AGENT IDENTITY
# ============================================================
collect_agent_identity() {
    print_header "Section 4/6 — Agent Identity"

    AGENT_NAME=$(ask "Agent name" "TARS")
    AGENT_ID=$(echo "$AGENT_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')

    echo "  Agent role:"
    echo "    1) assistant         — general-purpose personal or business assistant"
    echo "    2) researcher        — research, analysis, news, web search"
    echo "    3) developer         — coding, technical tasks, ops"
    echo "    4) business-assistant — business workflows, client management, scheduling"
    local role_choice
    role_choice=$(ask "Choice" "1")
    case "$role_choice" in
        2) AGENT_ROLE="researcher" ;;
        3) AGENT_ROLE="developer" ;;
        4) AGENT_ROLE="business-assistant" ;;
        *)  AGENT_ROLE="assistant" ;;
    esac

    echo "  Brief description: What is $AGENT_NAME for? (1-2 sentences)"
    if $NON_INTERACTIVE; then
        AGENT_DESCRIPTION="A general-purpose AI assistant."
    else
        read -r -p "  > " AGENT_DESCRIPTION
        AGENT_DESCRIPTION="${AGENT_DESCRIPTION:-A general-purpose AI assistant.}"
    fi

    print_success "Agent: $AGENT_NAME ($AGENT_ROLE)"
}

# ============================================================
# SECTION 5: OPTIONAL INTEGRATIONS
# ============================================================
# These are TARS-specific service integrations that extend
# what the agent can do beyond Claude + messaging.
# ============================================================
collect_integrations() {
    print_header "Section 5/6 — Optional Integrations (Enter to skip any)"

    TAVILY_API_KEY=$(ask_secret "Tavily API key (web search)" || true)
    [[ -n "${TAVILY_API_KEY:-}" ]] && print_success "Tavily: enabled"

    NOTION_TOKEN=$(ask_secret "Notion integration token" || true)
    [[ -n "${NOTION_TOKEN:-}" ]] && print_success "Notion: enabled"

    TRELLO_KEY=$(ask_secret "Trello API key" || true)
    if [[ -n "${TRELLO_KEY:-}" ]]; then
        TRELLO_TOKEN=$(ask_secret "Trello token")
        print_success "Trello: enabled"
    fi

    echo
    echo "  Google OAuth (Calendar, Gmail, Drive):"
    echo "    1. Go to https://console.cloud.google.com → APIs & Services → Credentials"
    echo "    2. Create OAuth 2.0 Client ID (Desktop app)"
    echo "    3. Enable Calendar API, Gmail API, Drive API"
    echo
    GOOGLE_CLIENT_ID=$(ask "Google client ID (optional)" "")
    if [[ -n "${GOOGLE_CLIENT_ID:-}" ]]; then
        GOOGLE_CLIENT_SECRET=$(ask_secret "Google client secret")
        print_success "Google OAuth: enabled"
    fi
}

# ============================================================
# SECTION 6: GENERATE & DEPLOY
# ============================================================
generate_and_deploy() {
    print_header "Section 6/6 — Generate & Deploy"

    TARS_HOME="${TARS_HOME:-$SCRIPT_DIR}"
    DOCKER_HOST_IP=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || echo "172.17.0.1")

    # Generate age keypair if not present
    local age_key_path="$TARS_HOME/.secrets/age-key.txt"
    mkdir -p "$TARS_HOME/.secrets" "$TARS_HOME/.secrets-vault"
    if [[ ! -f "$age_key_path" ]]; then
        age-keygen -o "$age_key_path" 2>/dev/null
        chmod 600 "$age_key_path"
        print_success "Age keypair generated"
    else
        print_success "Age keypair found"
    fi
    local age_pubkey
    age_pubkey=$(age-keygen -y "$age_key_path" 2>/dev/null || grep "^# public key:" "$age_key_path" | awk '{print $NF}')

    # Write .env
    cat > "$SCRIPT_DIR/.env" << ENVEOF
# TARS Environment — generated by setup.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Do not commit this file.

TARS_HOME=${TARS_HOME}
DOCKER_HOST_IP=${DOCKER_HOST_IP}
OWNER_NAME=${OWNER_NAME}
TIMEZONE=${TIMEZONE}
DEPLOYMENT_PURPOSE=${DEPLOYMENT_PURPOSE}

# Network ports
AUTH_PROXY_PORT=9100
MEMORY_API_PORT=8897
EMBEDDING_PORT=8896
WEB_PROXY_PORT=8899
DASHBOARD_PORT=8765
DASHBOARD_API_PORT=8766

# OpenClaw gateway (manages Claude auth, messaging, model selection)
OC_GATEWAY_PORT=${OC_GATEWAY_PORT}
OC_GATEWAY_TOKEN=${OC_GATEWAY_TOKEN}
OC_LLM_URL=${OC_LLM_URL}

# Ops alerts
${OPS_ALERTS_CHANNEL:+OPS_ALERTS_CHANNEL=${OPS_ALERTS_CHANNEL}}

# Agent
AGENT_NAME=${AGENT_NAME}
AGENT_ID=${AGENT_ID}
AGENT_ROLE=${AGENT_ROLE}

# Integrations
${TAVILY_API_KEY:+TAVILY_API_KEY=${TAVILY_API_KEY}}
${NOTION_TOKEN:+NOTION_TOKEN=${NOTION_TOKEN}}
${TRELLO_KEY:+TRELLO_KEY=${TRELLO_KEY}}
${TRELLO_TOKEN:+TRELLO_TOKEN=${TRELLO_TOKEN}}
${GOOGLE_CLIENT_ID:+GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}}
${GOOGLE_CLIENT_SECRET:+GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}}

# Paths
AGE_KEY_PATH=${age_key_path}
AGE_PUBKEY=${age_pubkey}
SECRETS_VAULT_PATH=${TARS_HOME}/.secrets-vault/secrets.age
NPM_GLOBAL_BIN=${HOME}/.npm-global/bin
ENVEOF
    chmod 600 "$SCRIPT_DIR/.env"
    print_success ".env written"

    # Generate agent workspace
    local workspace="$TARS_HOME/workspace-${AGENT_ID}"
    mkdir -p "$workspace"
    generate_soul_md > "$workspace/SOUL.md"
    print_success "Agent workspace: $workspace"

    print_header "Building Docker Images"
    echo "  This may take a few minutes on first run..."
    docker compose build --network=host --parallel 2>&1 | grep -E 'Successfully|ERROR|error' || true
    print_success "Docker images built"

    print_header "Starting Services"
    docker compose up -d
    print_success "Services started"

    print_header "Health Checks"
    wait_for_service "http://localhost:${AUTH_PROXY_PORT:-9100}/ops/health" "auth-proxy" 60
    wait_for_service "http://localhost:${MEMORY_API_PORT:-8897}/health" "memory-api" 60
    wait_for_service "http://localhost:${EMBEDDING_PORT:-8896}/health" "embedding-service" 90

    print_header "Done!"
    echo
    echo -e "  ${GREEN}TARS is running.${RESET}"
    echo
    echo "  Agent:     $AGENT_NAME ($AGENT_ROLE)"
    echo "  Dashboard: http://localhost:${DASHBOARD_PORT:-8765}"
    echo
    echo "  Claude auth and messaging are managed by OpenClaw."
    echo "  LLM endpoint: ${OC_LLM_URL}"
    echo
    echo "  Next steps:"
    echo "    1. Say hello to $AGENT_NAME on your messaging platform"
    echo "    2. Dashboard: http://localhost:${DASHBOARD_PORT:-8765}"
    echo "    3. Add more agents: ./scripts/add-agent.sh"
    echo "    4. Reconfigure OpenClaw: openclaw onboard"
    echo
}

wait_for_service() {
    local url="$1" name="$2" timeout="${3:-60}" elapsed=0
    echo -n "  Waiting for $name"
    while [[ $elapsed -lt $timeout ]]; do
        if curl -sf "$url" > /dev/null 2>&1; then
            print_success "$name healthy"
            return 0
        fi
        sleep 2; elapsed=$((elapsed+2)); echo -n "."
    done
    echo
    print_warn "$name not healthy after ${timeout}s — check: docker compose logs $name"
}

generate_soul_md() {
    local role_desc=""
    case "$AGENT_ROLE" in
        researcher)         role_desc="You specialise in research, analysis, and finding information." ;;
        developer)          role_desc="You specialise in coding, technical problem-solving, and operations." ;;
        business-assistant) role_desc="You specialise in business workflows, scheduling, client management, and productivity." ;;
        *)                  role_desc="You are a general-purpose assistant, helping with a wide range of tasks." ;;
    esac

    cat << SOULEOF
# ${AGENT_NAME}

${AGENT_DESCRIPTION}

## Role
${role_desc}

## Owner
Your owner is ${OWNER_NAME}. They set you up using TARS v${TARS_VERSION}.

## Deployment
- Purpose: ${DEPLOYMENT_PURPOSE}

## Communication Style
Be direct, concise, and helpful. Ask clarifying questions when needed. Proactively flag issues.

## Capabilities
- Memory: persistent across conversations
- Web search: $([ -n "${TAVILY_API_KEY:-}" ] && echo "enabled (Tavily)" || echo "not configured")
- Google Workspace: $([ -n "${GOOGLE_CLIENT_ID:-}" ] && echo "enabled (Calendar, Gmail, Drive)" || echo "not configured")
- Notion: $([ -n "${NOTION_TOKEN:-}" ] && echo "enabled" || echo "not configured")
- Trello: $([ -n "${TRELLO_KEY:-}" ] && echo "enabled" || echo "not configured")
SOULEOF
}

# ============================================================
# MAIN
# ============================================================
main() {
    echo
    echo -e "${BLUE}╔══════════════════════════════════════════╗${RESET}"
    echo -e "${BLUE}║  TARS — Trusted Agent Runtime Stack      ║${RESET}"
    echo -e "${BLUE}║  Setup Wizard v${TARS_VERSION}               ║${RESET}"
    echo -e "${BLUE}╚══════════════════════════════════════════╝${RESET}"
    echo

    # Resume check
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        print_warn "Existing .env found"
        local ans
        ans=$(ask_yn "Reconfigure from scratch? (N = redeploy with existing config)" "n")
        if [[ ! "$ans" =~ ^[Yy] ]]; then
            # Source existing config and jump to deploy
            set -a; source "$SCRIPT_DIR/.env"; set +a
            generate_and_deploy
            exit 0
        fi
    fi

    check_prerequisites       # 1. System requirements
    collect_basics            # 2. Owner name, timezone, purpose
    setup_openclaw            # 3. Install & configure OpenClaw (Claude auth + messaging)
    collect_agent_identity    # 4. Agent name, role, description
    collect_integrations      # 5. Tavily, Notion, Trello, Google
    generate_and_deploy       # 6. .env, Docker build, start services
}

main "$@"
