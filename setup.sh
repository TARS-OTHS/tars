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

# Step 3: Tools & Skills
print_header "Step 3: Tools & Skills"

echo -e "  ${BOLD}Core tools (always included):${NC}"
for py in "$TARS_DIR"/src/tools/*.py; do
    [ -f "$py" ] || continue
    name=$(basename "$py" .py)
    [[ "$name" == __* ]] && continue
    echo -e "    ${GREEN}✓${NC} $name"
done

CORE_SKILLS=()
for yml in "$TARS_DIR"/skills/*.yaml "$TARS_DIR"/skills/*.yml; do
    [ -f "$yml" ] || continue
    name=$(basename "$yml" .yaml)
    name=$(basename "$name" .yml)
    CORE_SKILLS+=("$name")
done
if [ ${#CORE_SKILLS[@]} -gt 0 ]; then
    echo -e "\n  ${BOLD}Core skills (always included):${NC}"
    for s in "${CORE_SKILLS[@]}"; do
        echo -e "    ${GREEN}✓${NC} $s"
    done
fi

# Scan for Layer 2 modules
TARS_OTHS_ROOT=""
TARS_OTHS=""
SELECTED_MODULES=()

# Check common locations for Layer 2 modules
for candidate in "$(dirname "$TARS_DIR")/tars-oths" "$(dirname "$TARS_DIR")/oths"; do
    if [ -d "$candidate" ]; then
        TARS_OTHS_ROOT="$candidate"
        break
    fi
done

if [ -z "$TARS_OTHS_ROOT" ]; then
    echo ""
    read -rp "  Path to extension modules (leave blank to skip): " TARS_OTHS_ROOT
fi

if [ -n "$TARS_OTHS_ROOT" ] && [ -d "$TARS_OTHS_ROOT" ]; then
    echo -e "\n  ${BOLD}Available extension modules:${NC}"

    AVAILABLE_MODULES=()
    idx=1
    for mod_dir in "$TARS_OTHS_ROOT"/*/; do
        [ -d "$mod_dir" ] || continue
        mod_name=$(basename "$mod_dir")

        # Collect tools
        mod_tools=""
        if [ -d "$mod_dir/tools" ]; then
            mod_tools=$(ls "$mod_dir/tools/"*.py 2>/dev/null | while read -r f; do
                t=$(basename "$f" .py)
                [[ "$t" == __* ]] && continue
                echo -n "$t "
            done)
        fi

        # Collect skills
        mod_skills=""
        if [ -d "$mod_dir/skills" ]; then
            mod_skills=$(ls "$mod_dir/skills/"*.yaml "$mod_dir/skills/"*.yml 2>/dev/null | while read -r f; do
                s=$(basename "$f" .yaml)
                s=$(basename "$s" .yml)
                echo -n "$s "
            done)
        fi

        AVAILABLE_MODULES+=("$mod_name")
        echo -e "\n    ${BOLD}[$idx] $mod_name${NC}"
        [ -n "$mod_tools" ] && echo "        Tools:  $mod_tools"
        [ -n "$mod_skills" ] && echo "        Skills: $mod_skills"
        ((idx++))
    done

    if [ ${#AVAILABLE_MODULES[@]} -gt 0 ]; then
        echo ""
        echo "  Enter module numbers to enable (comma-separated), or 'all'."
        read -rp "  Modules [none]: " MOD_CHOICE

        if [ -n "$MOD_CHOICE" ]; then
            if [ "$MOD_CHOICE" = "all" ]; then
                SELECTED_MODULES=("${AVAILABLE_MODULES[@]}")
            else
                IFS=',' read -ra CHOICES <<< "$MOD_CHOICE"
                for c in "${CHOICES[@]}"; do
                    c=$(echo "$c" | tr -d ' ')
                    if [[ "$c" =~ ^[0-9]+$ ]] && [ "$c" -ge 1 ] && [ "$c" -le ${#AVAILABLE_MODULES[@]} ]; then
                        SELECTED_MODULES+=("${AVAILABLE_MODULES[$((c-1))]}")
                    fi
                done
            fi
        fi

        if [ ${#SELECTED_MODULES[@]} -gt 0 ]; then
            # Build TARS_OTHS as colon-separated paths
            oths_paths=()
            for mod in "${SELECTED_MODULES[@]}"; do
                oths_paths+=("$TARS_OTHS_ROOT/$mod")
            done
            TARS_OTHS=$(IFS=:; echo "${oths_paths[*]}")
            echo ""
            for mod in "${SELECTED_MODULES[@]}"; do
                print_ok "Module: $mod"
            done
        else
            print_ok "No extension modules selected (core tools only)"
        fi
    fi
else
    print_ok "No extension modules found (core tools only)"
fi

# Step 4: Discord Bot
print_header "Step 4: Discord Bot"
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

# Step 5: Vault
print_header "Step 5: Encrypted Vault"
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

# Step 6: Context Compression
print_header "Step 6: Context Compression"
echo "  T.A.R.S can compress verbose context files (codex docs, skill prompts)"
echo "  to reduce input tokens per agent message. Rule-based, no LLM calls."
echo "  CLAUDE.md files are excluded — they're carefully tuned prompts."
echo ""

COMPRESSION_ENABLED="false"
COMPRESSION_LEVEL="standard"
MEMORY_RECALL="false"

if ask_yn "Enable context compression?"; then
    COMPRESSION_ENABLED="true"
    echo ""
    echo "  Compression levels:"
    echo "    1) lite     — filler phrases only"
    echo "    2) standard — filler + contractions (recommended)"
    read -rp "  Level [standard]: " COMP_LEVEL_INPUT
    case "${COMP_LEVEL_INPUT:-2}" in
        1|lite) COMPRESSION_LEVEL="lite" ;;
        *) COMPRESSION_LEVEL="standard" ;;
    esac
    print_ok "Context compression: $COMPRESSION_LEVEL"

    echo ""
    echo "  Memory recall compression strips filler from memories before"
    echo "  injecting them into agent context. Same rules, applied at runtime."
    echo ""
    if ask_yn "Enable memory recall compression?"; then
        MEMORY_RECALL="true"
        print_ok "Memory recall compression enabled"
    fi
else
    print_ok "Compression skipped (can enable later in config.yaml)"
fi

# Step 7: Configuration
print_header "Step 7: Agent Configuration"

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

  compression:
    enabled: ${COMPRESSION_ENABLED}
    level: ${COMPRESSION_LEVEL}
    memory_recall: ${MEMORY_RECALL}

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

# Step 8: Maintenance Timers
print_header "Step 8: Maintenance Timers"
print_step "Installing core timers (memory, health, integrity)..."

for f in "$TARS_DIR"/config/timers/tars-*.service "$TARS_DIR"/config/timers/tars-*.timer; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    content=$(sed "s|/opt/tars|$TARS_DIR|g" "$f")
    if [[ "$name" == *.service ]] && [ -n "$TARS_OVERLAY" ]; then
        content=$(echo "$content" | sed "/Environment=TARS_HOME=/a Environment=TARS_OVERLAY=$TARS_OVERLAY")
    fi
    echo "$content" > "$TARS_OVERLAY/systemd/$name"
    sudo ln -sf "$TARS_OVERLAY/systemd/$name" "/etc/systemd/system/$name"
done

sudo systemctl daemon-reload

for timer in "$TARS_DIR"/config/timers/tars-*.timer; do
    [ -f "$timer" ] || continue
    name=$(basename "$timer")
    sudo systemctl enable --now "$name" 2>/dev/null || true
done
print_ok "Core timers installed"

# Step 9: Auto-Start Service
print_header "Step 9: Auto-Start"
if ask_yn "Install systemd service? (auto-start on boot)"; then
    UV_PATH=$(which uv 2>/dev/null || echo "${HOME}/.local/bin/uv")

    # Generate service file into overlay
    sed -e "s|/opt/tars|${TARS_DIR}|g" \
        -e "s|ExecStart=.*|ExecStart=${UV_PATH} run --no-sync python -m src.main|" \
        -e "/^Environment=PATH=/a Environment=TARS_OVERLAY=${TARS_OVERLAY}" \
        "${TARS_DIR}/config/tars.service" > "$TARS_OVERLAY/systemd/tars.service"

    # Add TARS_OTHS if extension modules were selected
    if [ -n "$TARS_OTHS" ]; then
        sed -i "/^Environment=TARS_OVERLAY=/a Environment=TARS_OTHS=${TARS_OTHS}" \
            "$TARS_OVERLAY/systemd/tars.service"
    fi

    # Update ReadWritePaths to include overlay paths
    TARS_USER_HOME=$(eval echo ~tars)
    sed -i "s|ReadWritePaths=.*|ReadWritePaths=${TARS_DIR}/data ${TARS_OVERLAY}/agents ${TARS_OVERLAY}/config ${TARS_OVERLAY}/tmp /tmp ${TARS_USER_HOME}/.cache ${TARS_USER_HOME}/.claude|" \
        "$TARS_OVERLAY/systemd/tars.service"
    sed -i "s|ReadOnlyPaths=.*|ReadOnlyPaths=${TARS_DIR} ${TARS_OVERLAY}|" \
        "$TARS_OVERLAY/systemd/tars.service"

    # Symlink to /etc/systemd/system/
    sudo ln -sf "$TARS_OVERLAY/systemd/tars.service" /etc/systemd/system/tars.service
    sudo systemctl daemon-reload
    systemctl enable tars.service
    print_ok "Service installed (sudo systemctl start tars)"
fi

# Done
print_header "Setup Complete!"
echo -e "  ${BOLD}Core:${NC}        $TARS_DIR"
echo -e "  ${BOLD}Overlay:${NC}     $TARS_OVERLAY"
if [ -n "$TARS_OTHS" ]; then
    echo -e "  ${BOLD}Modules:${NC}     ${SELECTED_MODULES[*]}"
fi
echo -e "  ${BOLD}Start:${NC}       uv run python -m src.main"
echo -e "  ${BOLD}Or:${NC}          sudo systemctl start tars"
echo -e "  ${BOLD}Discord:${NC}     @${BOT_NAME} hello!"
echo -e "  ${BOLD}Add keys:${NC}    .venv/bin/python vault-manage.py"
echo -e "  ${BOLD}Tests:${NC}       .venv/bin/python scripts/test-tools.py"
echo -e "  ${BOLD}Docs:${NC}        README.md / ARCHITECTURE.md / SCRIPTS.md"
echo ""
