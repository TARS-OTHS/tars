#!/usr/bin/env bash
# TARS — Trusted Agent Runtime Stack — Setup Wizard
# Run this on a fresh Ubuntu 22.04/24.04 VPS to deploy a working TARS instance.
# Usage: ./setup.sh [--non-interactive]

set -euo pipefail

TARS_VERSION="0.1.0-alpha"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.3.8}"
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
    print_header "Prerequisites"
    local missing=() can_install=()

    check_cmd() {
        local cmd="$1" pkg="${2:-$1}"
        if command -v "$cmd" &>/dev/null; then
            print_success "$cmd found ($(command -v "$cmd"))"
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
        if [[ "$node_ver" -lt 18 ]]; then
            print_warn "Node.js v$node_ver found — v18+ required"
            missing+=("nodejs-18+")
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
    print_header "Basic Configuration"

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
# SECTION 3: MESSAGING PLATFORM
# ============================================================
collect_messaging() {
    print_header "Messaging Platform"

    echo "  Options:"
    echo "    1) Discord"
    echo "    2) Slack"
    local choice
    choice=$(ask "Choice" "1")

    if [[ "$choice" == "2" ]]; then
        MESSAGING_PLATFORM="slack"
        echo
        echo "  Slack setup:"
        echo "    1. Go to https://api.slack.com/apps → Create New App"
        echo "    2. Choose 'From scratch', give it a name and pick your workspace"
        echo "    3. Under 'OAuth & Permissions', add bot scopes: chat:write, channels:history, im:history"
        echo "    4. Install the app to your workspace"
        echo "    5. Copy the 'Bot User OAuth Token' (starts with xoxb-)"
        echo
        local token
        token=$(ask_secret "Slack bot token (xoxb-...)")
        local status
        status=$(curl -s -H "Authorization: Bearer $token" https://slack.com/api/auth.test | grep -o '"ok":true' || true)
        if [[ -n "$status" ]]; then
            print_success "Slack token valid"
            SLACK_BOT_TOKEN="$token"
        else
            print_error "Slack token validation failed — check the token and try again"
            exit 1
        fi
    else
        MESSAGING_PLATFORM="discord"
        echo
        echo "  Discord bot setup:"
        echo "    1. Go to https://discord.com/developers/applications → New Application"
        echo "    2. Name your bot, then go to Bot → Add Bot"
        echo "    3. Under Token, click 'Reset Token' and copy it"
        echo "    4. Under Privileged Gateway Intents, enable: Message Content Intent"
        echo "    5. Go to OAuth2 → URL Generator → select bot + applications.commands"
        echo "       Add permissions: Send Messages, Read Message History"
        echo "    6. Copy the generated URL and open it to invite the bot to your server"
        echo
        local token
        token=$(ask_secret "Discord bot token")
        local response
        response=$(curl -s -H "Authorization: Bot $token" https://discord.com/api/v10/users/@me)
        if echo "$response" | grep -q '"id"'; then
            local bot_name
            bot_name=$(echo "$response" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)
            print_success "Discord token valid — bot name: $bot_name"
            DISCORD_BOT_TOKEN="$token"
        else
            print_error "Discord token validation failed — check the token and try again"
            exit 1
        fi
    fi

    DISCORD_GUILD_ID=$(ask "Discord server (guild) ID (right-click server → Copy Server ID)" "${DISCORD_GUILD_ID:-}")
    DISCORD_CHANNEL_ID=$(ask "Discord channel ID for agent messages" "${DISCORD_CHANNEL_ID:-}")
}

# ============================================================
# SECTION 4: AI PROVIDER
# ============================================================
collect_ai_provider() {
    print_header "AI Provider"

    echo "  Anthropic API key required (get one at https://console.anthropic.com)"
    local key
    key=$(ask_secret "Anthropic API key (sk-ant-...)")
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "x-api-key: $key" \
        -H "anthropic-version: 2023-06-01" \
        https://api.anthropic.com/v1/models)
    if [[ "$status" == "200" ]]; then
        print_success "Anthropic API key valid"
        ANTHROPIC_API_KEY="$key"
    else
        print_error "Anthropic API key validation failed (HTTP $status)"
        exit 1
    fi

    echo "  Model selection:"
    echo "    1) claude-sonnet-4-6 (recommended — fast, capable, cost-effective)"
    echo "    2) claude-opus-4-6   (most capable — higher cost)"
    local model_choice
    model_choice=$(ask "Choice" "1")
    AGENT_MODEL="claude-sonnet-4-6"
    [[ "$model_choice" == "2" ]] && AGENT_MODEL="claude-opus-4-6"
    print_success "Model: $AGENT_MODEL"

    echo "  OpenAI API key (optional — skip with Enter)"
    OPENAI_API_KEY=$(ask_secret "OpenAI API key" || true)
}

# ============================================================
# SECTION 5: OPTIONAL INTEGRATIONS
# ============================================================
collect_integrations() {
    print_header "Optional Integrations (press Enter to skip any)"

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
# SECTION 6: AGENT IDENTITY
# ============================================================
collect_agent_identity() {
    print_header "Agent Identity"

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
    read -r -p "  > " AGENT_DESCRIPTION
    AGENT_DESCRIPTION="${AGENT_DESCRIPTION:-A general-purpose AI assistant.}"

    print_success "Agent: $AGENT_NAME ($AGENT_ROLE)"
}

# ============================================================
# SECTION 7: GENERATE & DEPLOY
# ============================================================
generate_and_deploy() {
    print_header "Generating Configuration"

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

# OpenClaw
OPENCLAW_VERSION=${OPENCLAW_VERSION}

# Messaging
MESSAGING_PLATFORM=${MESSAGING_PLATFORM}
${DISCORD_BOT_TOKEN:+DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}}
${DISCORD_GUILD_ID:+DISCORD_GUILD_ID=${DISCORD_GUILD_ID}}
${DISCORD_CHANNEL_ID:+DISCORD_CHANNEL_ID=${DISCORD_CHANNEL_ID}}
${SLACK_BOT_TOKEN:+SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}}

# AI
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
AGENT_MODEL=${AGENT_MODEL}
${OPENAI_API_KEY:+OPENAI_API_KEY=${OPENAI_API_KEY}}

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
    cp "$SCRIPT_DIR/templates/default/AGENTS.md.tmpl" "$workspace/AGENTS.md" 2>/dev/null || true
    cp "$SCRIPT_DIR/templates/default/TOOLS.md.tmpl" "$workspace/TOOLS.md" 2>/dev/null || true
    print_success "Agent workspace: $workspace"

    print_header "Building Docker Images"
    echo "  This may take a few minutes on first run..."
    docker compose build --parallel 2>&1 | grep -E 'Successfully|ERROR|error' || true
    print_success "Docker images built"

    print_header "Starting Services"
    docker compose up -d
    print_success "Services started"

    print_header "Health Checks"
    wait_for_service "http://localhost:${AUTH_PROXY_PORT:-9100}/ops/health" "auth-proxy" 60
    wait_for_service "http://localhost:${MEMORY_API_PORT:-8897}/health" "memory-api" 60
    wait_for_service "http://localhost:${EMBEDDING_PORT:-8896}/health" "embedding-service" 90

    print_header "Installing OpenClaw Gateway"
    if ! command -v openclaw &>/dev/null; then
        npm install -g "openclaw@${OPENCLAW_VERSION}" --prefix "$HOME/.npm-global" 2>/dev/null
        export PATH="$HOME/.npm-global/bin:$PATH"
    fi
    print_success "OpenClaw ${OPENCLAW_VERSION} ready"

    print_header "Done!"
    echo
    echo -e "  ${GREEN}TARS is running.${RESET}"
    echo
    echo "  Agent:     $AGENT_NAME ($AGENT_ROLE)"
    echo "  Platform:  $MESSAGING_PLATFORM"
    echo "  Dashboard: http://localhost:${DASHBOARD_PORT:-8765}"
    echo "  Model:     $AGENT_MODEL"
    echo
    echo "  Next steps:"
    echo "    1. Open your $MESSAGING_PLATFORM server/workspace"
    echo "    2. Say hello to $AGENT_NAME"
    echo "    3. Dashboard: http://localhost:${DASHBOARD_PORT:-8765}"
    echo "    4. Add more agents: ./scripts/add-agent.sh"
    echo "    5. Install skills: see docs/SKILLS.md"
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
- Platform: ${MESSAGING_PLATFORM}
- Model: ${AGENT_MODEL}

## Communication Style
Be direct, concise, and helpful. Ask clarifying questions when needed. Proactively flag issues.

## Capabilities
$(declare -f collect_integrations > /dev/null 2>&1 || true)
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

    check_prerequisites
    collect_basics
    collect_messaging
    collect_ai_provider
    collect_integrations
    collect_agent_identity
    generate_and_deploy
}

main "$@"
