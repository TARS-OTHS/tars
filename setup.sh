#!/usr/bin/env bash
# T.A.R.S — Interactive Setup Script
# Takes you from git clone to running agent in 5 minutes.
set -euo pipefail

TARS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$TARS_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_header() { echo -e "\n${BOLD}${CYAN}═══════════════════════════════════════${NC}"; echo -e "${BOLD}  $1${NC}"; echo -e "${BOLD}${CYAN}═══════════════════════════════════════${NC}\n"; }
print_step() { echo -e "${BLUE}▸${NC} $1"; }
print_ok() { echo -e "${GREEN}✓${NC} $1"; }
print_err() { echo -e "${RED}✗${NC} $1"; }
ask() { read -rp "  $1: " "$2"; }
ask_yn() { read -rp "  $1 [y/N]: " ans; [[ "$ans" =~ ^[Yy] ]]; }

print_header "T.A.R.S — The Agent Routing System"
echo "  This script will set up everything you need to run"
echo "  your first AI agent on Discord."

# Step 1: Dependencies
print_header "Step 1: Dependencies"

if command -v python3 &>/dev/null; then
    print_ok "Python $(python3 --version | awk '{print $2}')"
else
    print_err "Python 3.12+ required. Install: sudo apt install python3"
    exit 1
fi

if command -v uv &>/dev/null; then
    print_ok "uv"
else
    print_step "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    print_ok "uv installed"
fi

if ! command -v jq &>/dev/null; then
    print_step "Installing jq..."
    sudo apt-get install -y jq >/dev/null 2>&1
fi

if command -v claude &>/dev/null; then
    print_ok "Claude Code CLI"
else
    print_step "Installing Claude Code CLI..."
    curl -fsSL https://claude.ai/install.sh | sh
    if command -v claude &>/dev/null; then
        print_ok "Claude Code CLI installed"
    else
        print_err "Claude Code CLI installation failed"
        if ! ask_yn "Continue anyway?"; then exit 1; fi
    fi
fi

print_step "Installing Python dependencies..."
"$TARS_DIR/scripts/sync.sh"
print_ok "Dependencies installed"

# Step 2: Overlay Directory
print_header "Step 2: Deployment Overlay"
echo "  The overlay holds your config, agent identities, service files,"
echo "  and generated files — separate from the engine code."
echo "  This keeps Core clean for updates."
echo ""

# Compute sensible default: sibling directory named *-overlay
PARENT_DIR="$(dirname "$TARS_DIR")"
# Strip version suffix (tars-v2 -> tars) for the overlay name
BASE_NAME="$(basename "$TARS_DIR" | sed 's/-v[0-9]*//')"
DEFAULT_OVERLAY="$PARENT_DIR/${BASE_NAME}-overlay"

read -rp "  Overlay directory [$DEFAULT_OVERLAY]: " OVERLAY_INPUT
TARS_OVERLAY="${OVERLAY_INPUT:-$DEFAULT_OVERLAY}"

mkdir -p "$TARS_OVERLAY"/{config,agents,systemd,tmp/{media,docs,scratch}}

# Create overlay .gitignore
[ ! -f "$TARS_OVERLAY/.gitignore" ] && cat > "$TARS_OVERLAY/.gitignore" << 'GITIGNORE'
# Runtime data
agents/*/data/
**/MEMORY_CONTEXT.md

# Agent-generated files
tmp/

# Python
__pycache__/
*.pyc

# Claude Code session state
/.claude/

# Secrets
config/secrets.enc
config/secrets.salt
GITIGNORE

print_ok "Overlay created at $TARS_OVERLAY"

# Step 3: Discord Bot
print_header "Step 3: Discord Bot"
echo "  You need a Discord bot token. If you don't have one:"
echo ""
echo "  1. Go to https://discord.com/developers/applications"
echo "  2. New Application → name it → Create"
echo "  3. Bot tab → Reset Token → copy it"
echo "  4. Enable: Message Content Intent + Server Members Intent"
echo "  5. OAuth2 → URL Generator → scopes: bot, applications.commands"
echo "     Permissions: Send Messages, Read History, Add Reactions,"
echo "     Attach Files, Use Slash Commands"
echo "  6. Copy URL → open → add bot to your server"
echo ""

BOT_TOKEN="" GUILD_ID="" USER_ID="" BOT_NAME="YourBot"
if ask_yn "Got a bot token ready?"; then
    ask "Bot token" BOT_TOKEN
    ask "Server (guild) ID" GUILD_ID
    ask "Your Discord user ID" USER_ID
    BOT_INFO=$(curl -sf -H "Authorization: Bot $BOT_TOKEN" https://discord.com/api/v10/users/@me 2>/dev/null || echo '{}')
    BOT_NAME=$(echo "$BOT_INFO" | jq -r '.username // "Unknown"')
    if [ "$BOT_NAME" = "Unknown" ]; then
        print_err "Invalid token. Check and try again."
        exit 1
    fi
    print_ok "Bot: $BOT_NAME"
fi

# Step 4: Vault
print_header "Step 4: Encrypted Vault"
echo "  Your credentials are stored encrypted. Choose a passphrase."
echo ""

if [ ! -f "$TARS_OVERLAY/config/secrets.enc" ]; then
    read -rsp "  Vault passphrase: " VAULT_PASS; echo ""
    .venv/bin/python3 -c "
from src.vault.fernet import FernetVault
v = FernetVault('$TARS_OVERLAY/config/secrets.enc')
v.unlock('$VAULT_PASS')
if '$BOT_TOKEN': v.set('discord-token', '$BOT_TOKEN')
print(f'  Vault created: {len(v.list_keys())} secret(s)')
"
    mkdir -p ~/.config
    echo "$VAULT_PASS" > ~/.config/tars-vault-key
    chmod 600 ~/.config/tars-vault-key
    print_ok "Vault created"
else
    print_ok "Vault already exists"
fi

# Step 5: Configuration
print_header "Step 5: Agent Configuration"

AGENT_NAME="main"
ask "Name your agent (default: main)" AGENT_NAME_INPUT
[ -n "${AGENT_NAME_INPUT:-}" ] && AGENT_NAME="$AGENT_NAME_INPUT"

[ ! -f "$TARS_OVERLAY/config/config.yaml" ] && cat > "$TARS_OVERLAY/config/config.yaml" << YAML
tars:
  name: "T.A.R.S"
  log_level: info
  data_dir: ./data

connectors:
  discord:
    enabled: true
    accounts:
      ${AGENT_NAME}:
        token_key: discord-token

defaults:
  llm:
    provider: claude_code
    model: sonnet

security:
  hitl:
    connector: discord
    channel: "${GUILD_ID:-YOUR_CHANNEL_ID}"
    approvers: ["${USER_ID:-YOUR_USER_ID}"]
    timeout: 1800
    fail_mode: closed
    gated_tools:
      - send_email
      - install_mcp

  rate_limits:
    mode: log
    defaults:
      max_per_hour: 100

admin_users:
  discord: ["${USER_ID:-YOUR_USER_ID}"]
YAML

[ ! -f "$TARS_OVERLAY/config/agents.yaml" ] && cat > "$TARS_OVERLAY/config/agents.yaml" << YAML
agents:
  ${AGENT_NAME}:
    display_name: "${AGENT_NAME^}"
    description: "Primary agent"
    project_dir: ${TARS_OVERLAY}/agents/${AGENT_NAME}
    llm:
      provider: claude_code
      model: sonnet
    tools: all
    skills: all
    routing:
      discord:
        account: ${AGENT_NAME}
        channels: []
        mentions: true
YAML

[ ! -f "$TARS_OVERLAY/config/team.json" ] && cp config/team.json.example "$TARS_OVERLAY/config/team.json"

AGENT_DIR="$TARS_OVERLAY/agents/${AGENT_NAME}"
mkdir -p "$AGENT_DIR/.claude"

[ ! -f "$AGENT_DIR/CLAUDE.md" ] && cat > "$AGENT_DIR/CLAUDE.md" << MD
# ${AGENT_NAME^}

You are **${AGENT_NAME^}**, a helpful AI assistant on Discord.

- Be concise and direct
- Search memory before asking for context you might already have
- Store important things to memory for future reference
MD

[ ! -f "$AGENT_DIR/.mcp.json" ] && cat > "$AGENT_DIR/.mcp.json" << JSON
{
  "mcpServers": {
    "tars-tools": {
      "command": "${TARS_DIR}/.venv/bin/python3",
      "args": ["-m", "src.mcp_server"],
      "cwd": "${TARS_DIR}",
      "env": { "TARS_PROFILE": "\${TARS_PROFILE:-}" }
    }
  }
}
JSON

[ ! -f "$AGENT_DIR/.claude/settings.json" ] && cat > "$AGENT_DIR/.claude/settings.json" << JSON
{
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Glob(*)", "Grep(*)", "WebSearch(*)", "WebFetch(*)", "mcp__tars-tools__*"]
  },
  "env": { "PATH": "${TARS_DIR}/.venv/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin" }
}
JSON

print_ok "Agent '${AGENT_NAME}' configured"

# Step 6: Systemd
print_header "Step 6: Auto-Start"
if ask_yn "Install systemd service? (auto-start on boot)"; then
    UV_PATH=$(which uv 2>/dev/null || echo "${HOME}/.local/bin/uv")

    # Generate service file into overlay
    sed -e "s|/opt/tars|${TARS_DIR}|g" \
        -e "s|ExecStart=.*|ExecStart=${UV_PATH} run --no-sync python -m src.main|" \
        -e "/^Environment=PATH=/a Environment=TARS_OVERLAY=${TARS_OVERLAY}" \
        "${TARS_DIR}/config/tars.service" > "$TARS_OVERLAY/systemd/tars.service"

    # Update ReadWritePaths to include overlay paths
    sed -i "s|ReadWritePaths=.*|ReadWritePaths=${TARS_DIR}/data ${TARS_OVERLAY}/agents ${TARS_OVERLAY}/config ${TARS_OVERLAY}/tmp /tmp /home/tars/.cache /home/tars/.claude|" \
        "$TARS_OVERLAY/systemd/tars.service"
    sed -i "s|ReadOnlyPaths=.*|ReadOnlyPaths=${TARS_DIR} ${TARS_OVERLAY}|" \
        "$TARS_OVERLAY/systemd/tars.service"

    # Symlink to /etc/systemd/system/
    sudo ln -sf "$TARS_OVERLAY/systemd/tars.service" /etc/systemd/system/tars.service

    # Install core timers (symlink directly to Core)
    for f in "$TARS_DIR"/config/timers/tars-*.service "$TARS_DIR"/config/timers/tars-*.timer; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        # Render with correct paths
        content=$(sed "s|/opt/tars|$TARS_DIR|g" "$f")
        if [[ "$name" == *.service ]] && [ -n "$TARS_OVERLAY" ]; then
            content=$(echo "$content" | sed "/Environment=TARS_HOME=/a Environment=TARS_OVERLAY=$TARS_OVERLAY")
        fi
        echo "$content" > "$TARS_OVERLAY/systemd/$name"
        sudo ln -sf "$TARS_OVERLAY/systemd/$name" "/etc/systemd/system/$name"
    done

    sudo systemctl daemon-reload
    systemctl enable tars.service
    print_ok "Service installed (sudo systemctl start tars)"

    # Enable timers
    for timer in "$TARS_DIR"/config/timers/tars-*.timer; do
        [ -f "$timer" ] || continue
        name=$(basename "$timer")
        sudo systemctl enable --now "$name" 2>/dev/null || true
    done
    print_ok "Timers installed"
fi

# Done
print_header "Setup Complete!"
echo -e "  ${BOLD}Core:${NC}        $TARS_DIR"
echo -e "  ${BOLD}Overlay:${NC}     $TARS_OVERLAY"
echo -e "  ${BOLD}Start:${NC}       uv run python -m src.main"
echo -e "  ${BOLD}Or:${NC}          sudo systemctl start tars"
echo -e "  ${BOLD}Discord:${NC}     @${BOT_NAME} hello!"
echo -e "  ${BOLD}Add keys:${NC}    .venv/bin/python vault-manage.py"
echo -e "  ${BOLD}Tests:${NC}       .venv/bin/python scripts/test-tools.py"
echo -e "  ${BOLD}Docs:${NC}        README.md / ARCHITECTURE.md / SCRIPTS.md"
echo ""
