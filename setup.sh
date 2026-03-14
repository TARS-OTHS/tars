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
    read -r -s -p "  $prompt: " val; echo >&2
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
# SECTION 3: OPENCLAW GATEWAY + CREDENTIALS
# ============================================================
# We handle all credential collection and store secrets in our
# own age-encrypted vault. OpenClaw is installed with --no-onboard
# and configured programmatically via `openclaw config set`.
#
# TARS services use OC's Chat Completions endpoint for LLM
# access. A health check monitors the endpoint and alerts if
# OC updates break it.
# ============================================================
setup_openclaw() {
    print_header "Section 3/6 — OpenClaw Gateway + Credentials"

    # --- Init vault early (needed for storing secrets) ---
    TARS_HOME="${TARS_HOME:-$SCRIPT_DIR}"
    local age_key_path="$TARS_HOME/.secrets/age-key.txt"
    mkdir -p "$TARS_HOME/.secrets"
    if [[ ! -f "$age_key_path" ]]; then
        age-keygen -o "$age_key_path" 2>/dev/null
        chmod 600 "$age_key_path"
        print_success "Age keypair generated"
    else
        print_success "Age keypair found"
    fi
    AGE_KEY_PATH="$age_key_path"
    AGE_PUBKEY=$(age-keygen -y "$age_key_path" 2>/dev/null || grep "^# public key:" "$age_key_path" | awk '{print $NF}')

    # --- Install OpenClaw ---
    install_openclaw

    # --- Claude API key ---
    collect_claude_credentials

    # --- Messaging platform (Discord/Slack) ---
    collect_messaging_credentials

    # --- Configure OpenClaw programmatically ---
    configure_openclaw

    # --- Create vault resolver script for OC ---
    create_vault_resolver

    # --- Enable Chat Completions endpoint for TARS services ---
    enable_gateway_api

    # --- Dashboard access ---
    setup_dashboard_access
}

install_openclaw() {
    if command -v openclaw &>/dev/null; then
        local oc_ver
        oc_ver=$(openclaw --version 2>/dev/null || echo "unknown")
        print_success "OpenClaw found ($oc_ver)"
    else
        echo
        print_info "Installing OpenClaw..."
        echo
        curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard
        echo

        # Find openclaw in common install locations
        for p in "$HOME/.npm-global/bin" "$HOME/.local/bin" "/usr/local/bin"; do
            if [[ -x "$p/openclaw" ]]; then
                export PATH="$p:$PATH"
                break
            fi
        done

        if command -v openclaw &>/dev/null; then
            print_success "OpenClaw installed"
        else
            print_error "OpenClaw installation failed"
            echo "  Install manually: https://docs.openclaw.ai/install"
            exit 1
        fi
    fi
}

collect_claude_credentials() {
    echo
    print_info "Claude LLM connection"
    echo "  How do you want to connect to Claude?"
    echo "    1) Claude Max/Pro subscription (setup token)"
    echo "    2) Anthropic API key (pay-per-use)"
    local auth_choice
    auth_choice=$(ask "Choice" "1")

    if [[ "$auth_choice" == "2" ]]; then
        CLAUDE_AUTH_METHOD="api-key"
        echo
        echo "  Get an API key at: https://console.anthropic.com/settings/keys"
        echo
        local api_key
        api_key=$(ask_secret "Anthropic API key (starts with sk-ant-)")
        if [[ -z "$api_key" ]]; then
            print_error "API key is required"
            exit 1
        fi

        # Encrypt to vault
        echo "$api_key" | age -r "$AGE_PUBKEY" -o "$TARS_HOME/.secrets/anthropic-key.age"
        chmod 600 "$TARS_HOME/.secrets/anthropic-key.age"
        print_success "API key encrypted to vault"

        # Write auth-profiles.json directly (OC's paste-token is interactive TUI)
        local oc_agent_dir="$HOME/.openclaw/agents/main/agent"
        mkdir -p "$oc_agent_dir"
        cat > "$oc_agent_dir/auth-profiles.json" << AUTHEOF
{
  "profiles": {
    "anthropic:api": {
      "provider": "anthropic",
      "type": "api_key",
      "key": "$api_key"
    }
  },
  "order": {
    "anthropic": ["anthropic:api"]
  }
}
AUTHEOF
        print_success "Auth profile written to OpenClaw"

        # Test connection
        echo -n "  Testing Claude connection..."
        local status
        status=$(curl -sf -o /dev/null -w "%{http_code}" \
            -H "x-api-key: $api_key" \
            -H "anthropic-version: 2023-06-01" \
            -H "content-type: application/json" \
            -d '{"model":"claude-sonnet-4-20250514","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
            https://api.anthropic.com/v1/messages 2>/dev/null || echo "000")

        if [[ "$status" == "200" ]]; then
            print_success "Claude connection verified"
        elif [[ "$status" == "401" || "$status" == "403" ]]; then
            print_warn "Auth failed (HTTP $status) — check your API key"
        else
            print_warn "Claude returned HTTP $status — may work, check later"
        fi
    else
        CLAUDE_AUTH_METHOD="setup-token"
        echo
        echo "  To get a setup token:"
        echo "    1. Install Claude Code if you haven't: npm install -g @anthropic-ai/claude-code"
        echo "    2. Run: claude setup-token"
        echo "    3. Copy the generated token and paste it below"
        echo
        local setup_token
        setup_token=$(ask_secret "Claude setup token")
        if [[ -z "$setup_token" ]]; then
            print_error "Setup token is required"
            exit 1
        fi

        # Encrypt to vault
        echo "$setup_token" | age -r "$AGE_PUBKEY" -o "$TARS_HOME/.secrets/anthropic-key.age"
        chmod 600 "$TARS_HOME/.secrets/anthropic-key.age"
        print_success "Setup token encrypted to vault"

        # Write auth-profiles.json directly (OC's paste-token is interactive TUI)
        local oc_agent_dir="$HOME/.openclaw/agents/main/agent"
        mkdir -p "$oc_agent_dir"
        cat > "$oc_agent_dir/auth-profiles.json" << AUTHEOF
{
  "profiles": {
    "anthropic:default": {
      "provider": "anthropic",
      "type": "token",
      "token": "$setup_token"
    }
  },
  "order": {
    "anthropic": ["anthropic:default"]
  }
}
AUTHEOF
        print_success "Auth profile written to OpenClaw"

        # Test connection (setup tokens use Bearer auth with oauth beta header)
        echo -n "  Testing Claude connection..."
        local status
        status=$(curl -sf -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer $setup_token" \
            -H "anthropic-version: 2023-06-01" \
            -H "anthropic-beta: oauth-2025-04-20" \
            -H "content-type: application/json" \
            -d '{"model":"claude-sonnet-4-20250514","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
            https://api.anthropic.com/v1/messages 2>/dev/null || echo "000")

        if [[ "$status" == "200" ]]; then
            print_success "Claude connection verified"
        elif [[ "$status" == "401" || "$status" == "403" ]]; then
            print_warn "Auth failed (HTTP $status) — token may be invalid or expired"
            print_info "Re-run 'claude setup-token' to generate a fresh token"
        else
            print_warn "Claude returned HTTP $status — may work, check later"
        fi
    fi
}

collect_messaging_credentials() {
    echo
    print_info "Messaging platform"
    echo "  How will users talk to the agent?"
    echo "    1) Discord"
    echo "    2) Slack"
    echo "    3) Telegram"
    echo "    4) WhatsApp"
    echo "    5) Signal"
    echo "    6) Skip for now"
    local platform_choice
    platform_choice=$(ask "Choice" "1")

    case "$platform_choice" in
        1) MESSAGING_PLATFORM="discord"; setup_discord ;;
        2) MESSAGING_PLATFORM="slack"; setup_slack ;;
        3) MESSAGING_PLATFORM="telegram"; setup_telegram ;;
        4) MESSAGING_PLATFORM="whatsapp"; setup_whatsapp ;;
        5) MESSAGING_PLATFORM="signal"; setup_signal ;;
        *) MESSAGING_PLATFORM="none"
           print_warn "No messaging platform — agent will only be accessible via dashboard"
           ;;
    esac
}

setup_discord() {
    echo
    echo -e "  ${BLUE}Discord Bot Setup${RESET}"
    echo "  ─────────────────────────────────────────────────────────"
    echo "  Follow these steps to create your bot:"
    echo
    echo "  1. Go to https://discord.com/developers/applications"
    echo "  2. Click 'New Application' — name it (e.g. your agent name)"
    echo "  3. Go to Bot → click 'Reset Token' → copy the token"
    echo "  4. Under 'Privileged Gateway Intents' enable:"
    echo "     - Message Content Intent (required)"
    echo "     - Server Members Intent (recommended)"
    echo "  5. Go to OAuth2 → URL Generator"
    echo "     - Select scopes: bot, applications.commands"
    echo "     - Select permissions: View Channels, Send Messages,"
    echo "       Read Message History, Embed Links, Attach Files"
    echo "  6. Copy the generated URL → open in browser → add to your server"
    echo "  7. In Discord: right-click your server → Copy Server ID"
    echo "     (Enable Developer Mode in Settings → Advanced first)"
    echo "  8. Create two channels:"
    echo "     - #chat (or similar) — where users talk to the agent"
    echo "     - #ops-alerts — where the bot posts system alerts"
    echo "  9. Right-click each channel → Copy Channel ID"
    echo "  ─────────────────────────────────────────────────────────"
    echo

    DISCORD_BOT_TOKEN=$(ask_secret "Discord bot token")
    if [[ -z "$DISCORD_BOT_TOKEN" ]]; then
        print_error "Discord bot token is required"
        exit 1
    fi

    # Encrypt to vault
    echo "$DISCORD_BOT_TOKEN" | age -r "$AGE_PUBKEY" -o "$TARS_HOME/.secrets/discord-token.age"
    chmod 600 "$TARS_HOME/.secrets/discord-token.age"
    print_success "Discord token encrypted to vault"

    DISCORD_GUILD_ID=$(ask "Discord server (guild) ID" "")
    [[ -z "$DISCORD_GUILD_ID" ]] && { print_error "Server ID is required"; exit 1; }

    DISCORD_OWNER_ID=$(ask "Your Discord user ID" "")

    OPS_ALERTS_CHANNEL=$(ask "Ops-alerts channel ID" "")
    [[ -n "$OPS_ALERTS_CHANNEL" ]] && print_success "Ops alerts: #ops-alerts ($OPS_ALERTS_CHANNEL)"

    print_success "Discord configured"
}

setup_slack() {
    echo
    echo -e "  ${BLUE}Slack Bot Setup${RESET}"
    echo "  ─────────────────────────────────────────────────────────"
    echo "  Follow these steps to create your bot:"
    echo
    echo "  1. Go to https://api.slack.com/apps → Create New App"
    echo "  2. Choose 'From scratch' — name it, select your workspace"
    echo "  3. Go to OAuth & Permissions → add Bot Token Scopes:"
    echo "     - chat:write, channels:read, channels:history"
    echo "     - groups:read, groups:history, im:read, im:history"
    echo "     - users:read"
    echo "  4. Install App to Workspace → copy Bot User OAuth Token"
    echo "  5. Create two channels:"
    echo "     - #chat (or similar) — where users talk to the agent"
    echo "     - #ops-alerts — where the bot posts system alerts"
    echo "  6. Invite the bot to both channels: /invite @botname"
    echo "  7. Right-click each channel → Copy link → ID is at the end"
    echo "  ─────────────────────────────────────────────────────────"
    echo

    SLACK_BOT_TOKEN=$(ask_secret "Slack Bot User OAuth Token (xoxb-...)")
    if [[ -z "$SLACK_BOT_TOKEN" ]]; then
        print_error "Slack bot token is required"
        exit 1
    fi

    # Encrypt to vault
    echo "$SLACK_BOT_TOKEN" | age -r "$AGE_PUBKEY" -o "$TARS_HOME/.secrets/slack-token.age"
    chmod 600 "$TARS_HOME/.secrets/slack-token.age"
    print_success "Slack token encrypted to vault"

    OPS_ALERTS_CHANNEL=$(ask "Ops-alerts channel ID" "")
    [[ -n "$OPS_ALERTS_CHANNEL" ]] && print_success "Ops alerts: #ops-alerts ($OPS_ALERTS_CHANNEL)"

    print_success "Slack configured"
}

setup_telegram() {
    echo
    echo -e "  ${BLUE}Telegram Bot Setup${RESET}"
    echo "  ─────────────────────────────────────────────────────────"
    echo "  1. Open Telegram and message @BotFather"
    echo "  2. Send /newbot and follow the prompts"
    echo "  3. Copy the bot token BotFather gives you"
    echo "  ─────────────────────────────────────────────────────────"
    echo

    TELEGRAM_BOT_TOKEN=$(ask_secret "Telegram bot token")
    if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
        print_error "Telegram bot token is required"
        exit 1
    fi

    echo "$TELEGRAM_BOT_TOKEN" | age -r "$AGE_PUBKEY" -o "$TARS_HOME/.secrets/telegram-token.age"
    chmod 600 "$TARS_HOME/.secrets/telegram-token.age"
    print_success "Telegram token encrypted to vault"

    echo
    echo "  For ops alerts, create a group or channel and add the bot."
    OPS_ALERTS_CHANNEL=$(ask "Ops-alerts chat ID (Enter to skip)" "")
    [[ -n "$OPS_ALERTS_CHANNEL" ]] && print_success "Ops alerts configured"

    print_success "Telegram configured"
}

setup_whatsapp() {
    echo
    echo -e "  ${BLUE}WhatsApp Setup${RESET}"
    echo "  ─────────────────────────────────────────────────────────"
    echo "  WhatsApp connects via QR code pairing through OpenClaw."
    echo "  After setup completes, run:"
    echo "    openclaw channels pair whatsapp"
    echo "  and scan the QR code with your phone."
    echo "  ─────────────────────────────────────────────────────────"
    echo

    print_info "WhatsApp will be paired after deployment."
    OPS_ALERTS_CHANNEL=""
    print_success "WhatsApp selected (pair after deploy)"
}

setup_signal() {
    echo
    echo -e "  ${BLUE}Signal Setup${RESET}"
    echo "  ─────────────────────────────────────────────────────────"
    echo "  Signal connects via device linking through OpenClaw."
    echo "  After setup completes, run:"
    echo "    openclaw channels pair signal"
    echo "  and link the device from your Signal app."
    echo "  ─────────────────────────────────────────────────────────"
    echo

    print_info "Signal will be paired after deployment."
    OPS_ALERTS_CHANNEL=""
    print_success "Signal selected (pair after deploy)"
}

create_vault_resolver() {
    # Script that OC calls to read secrets from our age vault
    local resolver_path="$TARS_HOME/scripts/vault-resolver.sh"
    mkdir -p "$TARS_HOME/scripts"
    cat > "$resolver_path" << 'RESOLVEREOF'
#!/usr/bin/env bash
# TARS vault resolver — called by OpenClaw exec secret provider
# Reads encrypted secrets from age vault and returns them in OC's protocol format
set -euo pipefail

TARS_HOME="${TARS_HOME:-/opt/tars}"
AGE_KEY="${AGE_KEY_PATH:-$TARS_HOME/.secrets/age-key.txt}"

# Read request from stdin
request=$(cat)
provider=$(echo "$request" | jq -r '.provider')
ids=$(echo "$request" | jq -r '.ids[]')

# Map secret IDs to vault files
declare -A secret_map
secret_map["anthropic-api-key"]="$TARS_HOME/.secrets/anthropic-key.age"
secret_map["discord-bot-token"]="$TARS_HOME/.secrets/discord-token.age"
secret_map["slack-bot-token"]="$TARS_HOME/.secrets/slack-token.age"
secret_map["telegram-bot-token"]="$TARS_HOME/.secrets/telegram-token.age"
secret_map["gateway-token"]="$TARS_HOME/.secrets/gateway-token.age"

# Build response
values="{}"
for id in $ids; do
    vault_file="${secret_map[$id]:-}"
    if [[ -n "$vault_file" && -f "$vault_file" ]]; then
        val=$(age -d -i "$AGE_KEY" "$vault_file" 2>/dev/null | tr -d '\n')
        values=$(echo "$values" | jq --arg k "$id" --arg v "$val" '. + {($k): $v}')
    else
        values=$(echo "$values" | jq --arg k "$id" '. + {($k): null}')
    fi
done

echo "{\"protocolVersion\": 1, \"values\": $values}"
RESOLVEREOF
    chmod 700 "$resolver_path"
    print_success "Vault resolver script created"
}

configure_openclaw() {
    echo
    print_info "Configuring OpenClaw..."

    # Resolve Docker bridge IP early — needed for memory plugin config
    DOCKER_HOST_IP=${DOCKER_HOST_IP:-$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || echo "172.17.0.1")}

    mkdir -p "$HOME/.openclaw"

    # Generate gateway auth token
    OC_GATEWAY_TOKEN=$(openssl rand -hex 32 2>/dev/null || head -c 64 /dev/urandom | base64 | tr -d '/+=' | head -c 64)
    echo "$OC_GATEWAY_TOKEN" | age -r "$AGE_PUBKEY" -o "$TARS_HOME/.secrets/gateway-token.age"
    chmod 600 "$TARS_HOME/.secrets/gateway-token.age"

    # Build channel config based on selected platform
    local channel_block=""
    case "$MESSAGING_PLATFORM" in
        discord)
            # Decrypt token for OC config (OC needs plaintext in config or SecretRef)
            local discord_token
            discord_token=$(age -d -i "$AGE_KEY_PATH" "$TARS_HOME/.secrets/discord-token.age" 2>/dev/null)
            local guild_block=""
            if [[ -n "${DISCORD_GUILD_ID:-}" ]]; then
                local users_line=""
                [[ -n "${DISCORD_OWNER_ID:-}" ]] && users_line="\"users\": [\"${DISCORD_OWNER_ID}\"],"
                guild_block="\"guilds\": { \"${DISCORD_GUILD_ID}\": { ${users_line} \"requireMention\": true } },"
            fi
            local allow_from=""
            [[ -n "${DISCORD_OWNER_ID:-}" ]] && allow_from="\"allowFrom\": [\"${DISCORD_OWNER_ID}\"],"
            channel_block="\"discord\": {
        \"enabled\": true,
        \"token\": \"${discord_token}\",
        \"groupPolicy\": \"allowlist\",
        ${guild_block}
        ${allow_from}
        \"dmPolicy\": \"allowlist\"
      }"
            ;;
        slack)
            local slack_token
            slack_token=$(age -d -i "$AGE_KEY_PATH" "$TARS_HOME/.secrets/slack-token.age" 2>/dev/null)
            channel_block="\"slack\": {
        \"enabled\": true,
        \"botToken\": \"${slack_token}\"
      }"
            ;;
        telegram)
            local telegram_token
            telegram_token=$(age -d -i "$AGE_KEY_PATH" "$TARS_HOME/.secrets/telegram-token.age" 2>/dev/null)
            channel_block="\"telegram\": {
        \"enabled\": true,
        \"botToken\": \"${telegram_token}\"
      }"
            ;;
        whatsapp)
            channel_block="\"whatsapp\": { \"enabled\": true }"
            ;;
        signal)
            channel_block="\"signal\": { \"enabled\": true }"
            ;;
    esac

    # Write complete openclaw.json (JSON5 format supported)
    local oc_config="$HOME/.openclaw/openclaw.json"
    # Back up existing config if present
    [[ -f "$oc_config" ]] && cp "$oc_config" "${oc_config}.bak.tars" 2>/dev/null || true

    cat > "$oc_config" << OCEOF
{
  "gateway": {
    "mode": "local",
    "port": ${OC_GATEWAY_PORT:-18789},
    "bind": "lan",
    "auth": {
      "mode": "token",
      "token": "${OC_GATEWAY_TOKEN}"
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "anthropic/claude-sonnet-4-6"
      },
      "compaction": {
        "mode": "safeguard"
      },
      "maxConcurrent": 4,
      "subagents": { "maxConcurrent": 8 },
      "sandbox": {
        "mode": "all",
        "workspaceAccess": "rw",
        "sessionToolsVisibility": "all",
        "scope": "agent",
        "docker": {
          "image": "tars-sandbox:base",
          "readOnlyRoot": true,
          "network": "bridge",
          "user": "node",
          "capDrop": ["ALL"],
          "env": {
            "PYTHONUSERBASE": "/workspace/.local",
            "PATH": "/workspace/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "http_proxy": "http://${DOCKER_HOST_IP}:${WEB_PROXY_PORT:-8899}",
            "https_proxy": "http://${DOCKER_HOST_IP}:${WEB_PROXY_PORT:-8899}",
            "no_proxy": "${DOCKER_HOST_IP},localhost,127.0.0.1"
          },
          "memory": "2g",
          "cpus": 2,
          "extraHosts": ["host.docker.internal:${DOCKER_HOST_IP}"]
        }
      }
    },
    "list": [
      { "id": "main", "default": true }
    ]
  },
  "channels": {
    ${channel_block:-}
  },
  "tools": {
    "exec": {
      "host": "gateway",
      "security": "full",
      "ask": "off"
    }
  },
  "browser": {
    "enabled": true,
    "executablePath": "/usr/bin/google-chrome-stable",
    "headless": true,
    "noSandbox": true,
    "defaultProfile": "openclaw"
  },
  "secrets": {
    "providers": {
      "tars_vault": {
        "source": "exec",
        "command": "${TARS_HOME}/scripts/vault-resolver.sh",
        "passEnv": ["TARS_HOME", "AGE_KEY_PATH"],
        "jsonOnly": true
      }
    }
  },
  "plugins": {
    "load": {
      "paths": ["${TARS_HOME}/plugins/tars-memory", "${TARS_HOME}/plugins/tars-team"]
    },
    "slots": {
      "memory": "tars-memory"
    },
    "entries": {
      "tars-memory": {
        "enabled": true,
        "config": {
          "memoryApiUrl": "http://${DOCKER_HOST_IP}:${MEMORY_API_PORT:-8897}",
          "autoRecall": true,
          "autoSessionState": true,
          "maxRecallResults": 5
        }
      },
      "tars-team": {
        "enabled": true,
        "config": {
          "teamFilePath": "config/team.json"
        }
      }
    }
  }
}
OCEOF
    chmod 600 "$oc_config"
    print_success "openclaw.json written"

    # Exec approvals — single-user deployment, allow all by default
    cat > "$HOME/.openclaw/exec-approvals.json" << 'EXECEOF'
{
  "version": 1,
  "defaults": {
    "security": "full",
    "ask": "off"
  },
  "agents": {
    "main": {
      "security": "full",
      "ask": "off",
      "autoAllowSkills": true
    }
  }
}
EXECEOF
    chmod 600 "$HOME/.openclaw/exec-approvals.json"
    print_success "Exec approvals configured (full access)"

    # Install and start gateway daemon
    openclaw gateway install 2>/dev/null || true

    print_success "OpenClaw fully configured"
}

setup_dashboard_access() {
    echo
    print_info "Dashboard access"
    echo "  The dashboard should not be publicly accessible."
    echo "  How should your team access it?"
    echo "    1) Tailscale (recommended — private network, easy setup)"
    echo "    2) SSH tunnel only (no extra setup, manual per user)"
    local access_choice
    access_choice=$(ask "Choice" "1")

    case "$access_choice" in
        1) DASHBOARD_ACCESS="tailscale"; setup_tailscale ;;
        *) DASHBOARD_ACCESS="ssh-tunnel"
           echo
           print_info "Access the dashboard via SSH tunnel:"
           print_info "  ssh -L 8765:127.0.0.1:8765 root@$(hostname -I | awk '{print $1}')"
           print_info "  Then open http://localhost:8765 in your browser"
           TAILSCALE_IP=""
           print_success "Dashboard: SSH tunnel only"
           ;;
    esac
}

setup_tailscale() {
    echo

    # Already connected?
    if command -v tailscale &>/dev/null; then
        local ts_status
        ts_status=$(tailscale status --json 2>/dev/null | jq -r '.Self.TailscaleIPs[0]' 2>/dev/null || true)
        if [[ -n "$ts_status" && "$ts_status" != "null" ]]; then
            TAILSCALE_IP="$ts_status"
            print_success "Tailscale already connected ($TAILSCALE_IP)"
            return
        fi
    fi

    # Install Tailscale
    if ! command -v tailscale &>/dev/null; then
        print_info "Installing Tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh
        if command -v tailscale &>/dev/null; then
            print_success "Tailscale installed"
        else
            print_warn "Tailscale install failed — dashboard will be SSH tunnel only"
            DASHBOARD_ACCESS="ssh-tunnel"
            TAILSCALE_IP=""
            return
        fi
    fi

    # Interactive login (no auth key needed, no expiry)
    echo
    echo -e "  ${BLUE}Tailscale Login${RESET}"
    echo "  ─────────────────────────────────────────────────────────"
    echo "  A login URL will appear below. Open it in your browser"
    echo "  to connect this server to your Tailscale network."
    echo "  ─────────────────────────────────────────────────────────"
    echo
    tailscale up --hostname="tars-$(hostname -s | tr '[:upper:]' '[:lower:]')"

    # Get Tailscale IP
    sleep 2
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || true)
    if [[ -n "$TAILSCALE_IP" ]]; then
        print_success "Tailscale connected ($TAILSCALE_IP)"
        print_info "Dashboard will be at: http://${TAILSCALE_IP}:8765"

        # Allow Tailscale port through firewall
        if command -v ufw &>/dev/null; then
            ufw allow in on tailscale0 to any port 8765 2>/dev/null || true
            ufw allow in on tailscale0 to any port 8766 2>/dev/null || true
            print_success "Firewall: dashboard allowed on Tailscale interface"
        fi
    else
        print_warn "Could not get Tailscale IP — dashboard will be SSH tunnel only"
        DASHBOARD_ACCESS="ssh-tunnel"
        TAILSCALE_IP=""
    fi
}

enable_gateway_api() {
    echo
    print_info "Enabling gateway LLM endpoint for TARS services..."

    openclaw config set gateway.http.endpoints.chatCompletions.enabled true --json 2>/dev/null || true

    # Detect gateway port
    OC_GATEWAY_PORT=$(openclaw config get gateway.port 2>/dev/null | tr -d '"' || echo "18789")
    [[ "$OC_GATEWAY_PORT" == "null" || -z "$OC_GATEWAY_PORT" ]] && OC_GATEWAY_PORT=18789

    OC_LLM_URL="http://localhost:${OC_GATEWAY_PORT}/v1/chat/completions"

    print_success "Gateway API enabled on port $OC_GATEWAY_PORT"
    print_info "LLM endpoint: $OC_LLM_URL"

    # Start gateway and test
    echo -n "  Starting gateway..."
    systemctl --user start openclaw-gateway 2>/dev/null || openclaw gateway &>/dev/null &
    sleep 3

    local status
    status=$(curl -sf -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $OC_GATEWAY_TOKEN" \
        "http://localhost:${OC_GATEWAY_PORT}/v1/models" 2>/dev/null || echo "000")

    if [[ "$status" == "200" ]]; then
        print_success "Gateway responding"
    elif [[ "$status" == "000" ]]; then
        print_warn "Gateway not responding yet — may need manual start"
        print_info "Try: openclaw gateway"
    else
        print_warn "Gateway returned HTTP $status — check: openclaw doctor"
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

    # Age keypair already created in Section 3
    local age_key_path="$AGE_KEY_PATH"
    local age_pubkey="$AGE_PUBKEY"
    mkdir -p "$TARS_HOME/.secrets-vault"

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
CLAUDE_AUTH_METHOD=${CLAUDE_AUTH_METHOD}
OC_GATEWAY_PORT=${OC_GATEWAY_PORT}
OC_GATEWAY_TOKEN=${OC_GATEWAY_TOKEN}
OC_LLM_URL=${OC_LLM_URL}
MESSAGING_PLATFORM=${MESSAGING_PLATFORM}

# Dashboard access
DASHBOARD_ACCESS=${DASHBOARD_ACCESS}
DASHBOARD_BIND=$(if [[ -n "${TAILSCALE_IP:-}" ]]; then echo "0.0.0.0"; else echo "127.0.0.1"; fi)
${TAILSCALE_IP:+TAILSCALE_IP=${TAILSCALE_IP}}

# Ops alerts
${OPS_ALERTS_CHANNEL:+OPS_ALERTS_CHANNEL=${OPS_ALERTS_CHANNEL}}

# Agent
AGENT_NAME=${AGENT_NAME}
AGENT_ID=${AGENT_ID}
AGENT_ROLE=${AGENT_ROLE}

# Integration keys are stored in the encrypted vault, not here.

# Paths
AGE_KEY_PATH=${age_key_path}
AGE_PUBKEY=${age_pubkey}
SECRETS_VAULT_PATH=${TARS_HOME}/.secrets-vault/secrets.age
NPM_GLOBAL_BIN=${HOME}/.npm-global/bin
ENVEOF
    chmod 600 "$SCRIPT_DIR/.env"
    print_success ".env written"

    # Build encrypted vault for integration API keys
    mkdir -p "$TARS_HOME/.config/age"
    cp "$age_key_path" "$TARS_HOME/.config/age/key.txt"
    chmod 600 "$TARS_HOME/.config/age/key.txt"

    local vault_json="{}"
    [[ -n "${TAVILY_API_KEY:-}" ]] && vault_json=$(echo "$vault_json" | jq --arg v "$TAVILY_API_KEY" '. + {"secrets/tavily-api-key": $v}')
    [[ -n "${NOTION_TOKEN:-}" ]] && vault_json=$(echo "$vault_json" | jq --arg v "$NOTION_TOKEN" '. + {"secrets/notion-token": $v}')
    if [[ -n "${TRELLO_KEY:-}" && -n "${TRELLO_TOKEN:-}" ]]; then
        vault_json=$(echo "$vault_json" | jq --arg k "$TRELLO_KEY" --arg t "$TRELLO_TOKEN" '. + {"secrets/trello-credentials.json": {"key": $k, "token": $t}}')
    fi
    [[ -n "${GOOGLE_CLIENT_ID:-}" ]] && vault_json=$(echo "$vault_json" | jq --arg id "$GOOGLE_CLIENT_ID" --arg sec "${GOOGLE_CLIENT_SECRET:-}" '. + {"secrets/google-token.json": {"client_id": $id, "client_secret": $sec}}')

    echo "$vault_json" | age -r "$age_pubkey" -o "$TARS_HOME/.secrets-vault/secrets.age"
    chmod 600 "$TARS_HOME/.secrets-vault/secrets.age"
    local vault_count=$(echo "$vault_json" | jq 'length')
    print_success "Encrypted vault created ($vault_count secrets)"

    # Generate agent workspace
    local workspace="$TARS_HOME/workspace-${AGENT_ID}"
    mkdir -p "$workspace"
    generate_soul_md > "$workspace/SOUL.md"
    print_success "Agent workspace: $workspace"

    # Copy identity into OC's workspace so the bot knows who it is
    local oc_workspace="$HOME/.openclaw/workspace"
    mkdir -p "$oc_workspace"
    cp "$workspace/SOUL.md" "$oc_workspace/SOUL.md"
    cat > "$oc_workspace/IDENTITY.md" << IDEOF
# IDENTITY.md - Who Am I?

- **Name:** ${AGENT_NAME}
- **Creature:** AI agent — Trusted Agent Runtime Stack
- **Vibe:** Direct, competent, slightly dry. Gets things done.
- **Emoji:** ⚡

---

I am ${AGENT_NAME}, deployed by ${OWNER_NAME} using TARS v${TARS_VERSION}.
Role: ${AGENT_ROLE}.
${AGENT_DESCRIPTION}
IDEOF
    print_success "Agent identity written to OpenClaw workspace"

    # Generate TOOLS.md with actual service URLs and configured integrations
    generate_tools_md > "$oc_workspace/TOOLS.md"
    print_success "Tools manifest written to OpenClaw workspace"

    # Copy and template workspace docs (AGENTS.md, MEMORY.md)
    for tmpl in AGENTS.md MEMORY.md; do
        if [[ -f "$TARS_HOME/templates/$tmpl" ]]; then
            sed -e "s|DOCKER_HOST_IP|${DOCKER_HOST_IP}|g" \
                -e "s|DASHBOARD_API_PORT|${DASHBOARD_API_PORT:-8766}|g" \
                "$TARS_HOME/templates/$tmpl" > "$oc_workspace/$tmpl"
        fi
    done
    print_success "Agent operating docs written (AGENTS.md, MEMORY.md)"

    # USER.md with owner info
    cat > "$oc_workspace/USER.md" << USEREOF
# USER.md — Owner Profile

- **Name:** ${OWNER_NAME}
- **Timezone:** ${TIMEZONE}
- **Style:** Direct, concise. Dont waffle.
USEREOF
    print_success "USER.md written"

    # Install MEMORY_CONTEXT.md cron job
    chmod +x "$TARS_HOME/scripts/regen-memory-context.sh" 2>/dev/null || true
    # Add cron if not already present
    if ! crontab -l 2>/dev/null | grep -q "regen-memory-context"; then
        (crontab -l 2>/dev/null; echo "*/30 * * * * DOCKER_HOST_IP=${DOCKER_HOST_IP} OC_WORKSPACE=${oc_workspace} ${TARS_HOME}/scripts/regen-memory-context.sh >> /var/log/tars-context-regen.log 2>&1") | crontab -
        print_success "MEMORY_CONTEXT.md cron job installed (every 30 min)"
    fi
    # Run once now to create initial context
    DOCKER_HOST_IP="${DOCKER_HOST_IP}" OC_WORKSPACE="${oc_workspace}" "$TARS_HOME/scripts/regen-memory-context.sh" 2>/dev/null || true

    print_header "Installing Plugin Dependencies"
    for plugin_dir in "$TARS_HOME"/plugins/*/; do
        if [[ -f "${plugin_dir}package.json" ]]; then
            echo "  Installing deps for $(basename "$plugin_dir")..."
            (cd "$plugin_dir" && npm install --omit=dev --silent 2>&1) || print_warn "Failed to install deps for $(basename "$plugin_dir")"
        fi
    done
    print_success "Plugin dependencies installed"

    print_header "Building Sandbox Image"
    echo "  Building agent sandbox (tars-sandbox:base)..."
    docker build -t tars-sandbox:base -f "$TARS_HOME/templates/Dockerfile.sandbox" "$TARS_HOME" 2>&1 | grep -E 'Successfully|ERROR|error|DONE' || true
    print_success "Sandbox image built"

    print_header "Building Docker Images"
    echo "  This may take a few minutes on first run..."
    docker compose build --parallel 2>&1 | grep -E 'Successfully|ERROR|error' || true
    print_success "Docker images built"

    print_header "Starting Services"
    docker compose up -d
    print_success "Services started"

    print_header "Health Checks"
    wait_for_service "http://${DOCKER_HOST_IP}:${AUTH_PROXY_PORT:-9100}/ops/health" "auth-proxy" 90
    wait_for_service "http://${DOCKER_HOST_IP}:${MEMORY_API_PORT:-8897}/status" "memory-api" 90
    wait_for_service "http://${DOCKER_HOST_IP}:${EMBEDDING_PORT:-8896}/health" "embedding-service" 120

    print_header "Done!"
    echo
    echo -e "  ${GREEN}TARS is running.${RESET}"
    echo
    echo "  Agent:     $AGENT_NAME ($AGENT_ROLE)"
    if [[ -n "${TAILSCALE_IP:-}" ]]; then
        echo "  Dashboard: http://${TAILSCALE_IP}:${DASHBOARD_PORT:-8765} (Tailscale)"
    else
        echo "  Dashboard: http://localhost:${DASHBOARD_PORT:-8765} (SSH tunnel required)"
    fi
    echo
    echo "  Claude auth and messaging are managed by OpenClaw."
    echo "  LLM endpoint: ${OC_LLM_URL}"
    echo
    local dash_url
    if [[ -n "${TAILSCALE_IP:-}" ]]; then
        dash_url="http://${TAILSCALE_IP}:${DASHBOARD_PORT:-8765}"
    else
        dash_url="http://localhost:${DASHBOARD_PORT:-8765} (SSH tunnel required)"
    fi
    echo "  Next steps:"
    echo "    1. Say hello to $AGENT_NAME on your messaging platform"
    echo "    2. Dashboard: $dash_url"
    echo "    3. Add more agents: ./scripts/add-agent.sh"
    echo "    4. Reconfigure OpenClaw: openclaw onboard"
    if [[ "$MESSAGING_PLATFORM" == "discord" && -n "${DISCORD_OWNER_ID:-}" ]]; then
        echo
        echo "  Discord owner (${DISCORD_OWNER_ID}) is pre-authorized — no pairing needed."
        echo "  Additional users can DM the bot and you approve with:"
        echo "    openclaw pairing approve discord <CODE>"
    fi
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

generate_tools_md() {
    cat << TOOLSEOF
# TOOLS.md - Available Tools & Services

## TARS Services (Docker)

All services run in Docker on this host. Internal access via Docker bridge IP \`${DOCKER_HOST_IP}\`.

### Memory API
- **URL:** \`http://${DOCKER_HOST_IP}:${MEMORY_API_PORT:-8897}\`
- **Health:** \`GET /status\`
- **Native Tools:** \`memory_search\`, \`memory_semantic_search\`, \`memory_store\`, \`memory_context\`, \`session_state_save\`, \`session_state_get\`
- Persistent SQLite database with 384-dimension vector search
- **Use the native tools** — they handle HTTP for you. See MEMORY.md for details.

### Embedding Service
- **URL:** \`http://${DOCKER_HOST_IP}:${EMBEDDING_PORT:-8896}\`
- **Health:** \`GET /health\`
- **Model:** BGE-small-en-v1.5 (ONNX Runtime, 384 dimensions)
- **Endpoints:** \`POST /embed\`, \`POST /similarity\`, \`POST /search\`, \`POST /batch-embed\`

### Auth Proxy
- **URL:** \`http://${DOCKER_HOST_IP}:${AUTH_PROXY_PORT:-9100}\`
- **Health:** \`GET /ops/health\`
- Handles authenticated requests to external services

### Web Proxy
- **URL:** \`http://${DOCKER_HOST_IP}:${WEB_PROXY_PORT:-8899}\`
- Fetch and parse web pages, bypass CORS restrictions

### Credential Proxy
- Manages credential lifecycle for external service access

### Dashboard
- **UI:** port ${DASHBOARD_PORT:-8765}
- **API:** port ${DASHBOARD_API_PORT:-8766}
- **Endpoints:** \`/send\`, \`/tasks\`, \`/tasks/add\`, \`/tasks/update\`, \`/ops-alerts\`, \`/system-stats\`, \`/memory-health\`
- Send messages, manage tasks, view system health, ops alerts

### Cron
- Memory lifecycle: decay, archive, purge (every 6h)
- Memory backup (every 6h)
- Session state auto-capture (every 15min)
- Memory context regeneration (every 30min)
- Session fact extraction (every 10/40min)
- Memory promotion (every 12h)
- Alert monitoring (every 30min)
- Claude token refresh (every 5min)

## External Integrations

$([ -n "${TAVILY_API_KEY:-}" ] && echo "### Tavily (Web Search)
- API key configured
- Use for real-time web search, research, fact-checking
")
$([ -n "${TRELLO_KEY:-}" ] && echo "### Trello (Task Management)
- API key + token configured
- Create boards, lists, cards; manage project tasks
")
$([ -n "${NOTION_TOKEN:-}" ] && echo "### Notion
- Integration token configured
- Read/write Notion pages and databases
")
$([ -n "${GOOGLE_CLIENT_ID:-}" ] && echo "### Google Workspace
- OAuth configured (Calendar, Gmail, Drive)
")

## OpenClaw Gateway

- **LLM Endpoint:** \`${OC_LLM_URL}\`
- **Model:** \`anthropic/claude-sonnet-4-6\`
- OpenAI-compatible chat completions API
- Handles auth rotation, rate limits, model fallback

## Browser

OpenClaw provides a headless browser for web interaction:
- Browse websites, fill forms, take screenshots
- Controlled via OC browser tools

## Communication

- **${MESSAGING_PLATFORM:-none}** — Primary messaging channel
- **Dashboard** — Web UI for direct interaction and task management

## Secrets

All secrets are encrypted with age in \`${TARS_HOME}/.secrets/\`.
Decrypted at runtime by vault resolver script. Never store plaintext secrets.
TOOLSEOF
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
