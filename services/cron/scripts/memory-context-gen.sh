#!/bin/bash
# memory-context-gen.sh — Generates MEMORY_CONTEXT.md for each agent's workspace
# Runs every 30 min via cron. Fully mechanical — no agent cooperation needed.
# OC auto-injects workspace files into agent context at session start.
#
# Each agent gets tailored context:
#   talkie  — full infrastructure, all Discord channels, all services, all crons
#   newsbot — memory tree (newsbot-scoped), research Discord channels, core services
#   luna    — no shared memory DB, Ops + Learning Discord channels, minimal services
#   nova    — no shared memory DB, Ops + Learning Discord channels, minimal services

API="${MEMORY_API_URL:-http://memory-api:8897}"
AGENTS_JSON="${AGENT_SERVICES_DIR:-/app}/agents.json"
MAX_SIZE=6144  # ~6KB cap per file

log() { echo "[$(date -Iseconds)] context-gen: $*"; }

# Read agents from agents.json
AGENTS=$(python3 -c "
import json
with open('$AGENTS_JSON') as f:
    data = json.load(f)
for name, info in data['agents'].items():
    ws = info.get('workspace')
    if ws:
        print(f'{name}|{ws}')
")

if [ -z "$AGENTS" ]; then
    log "ERROR: No agents with workspaces found"
    exit 1
fi

# Fetch tree (global, shared across agents — but only included for agents that use shared memory)
TREE=$(curl -sf "$API/memory/tree" 2>/dev/null)
if [ -z "$TREE" ]; then
    log "WARNING: Could not fetch tree, using placeholder"
    TREE='{"tree": [], "memory_count": 0}'
fi

# Generate tree stats
TREE_SUMMARY=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
except:
    data = {}
tree = data.get('tree', [])
count = data.get('memory_count', 0)
generated = data.get('generated_at', 'unknown')[:19]

if isinstance(tree, list):
    branches = len(tree)
    names = [b.get('branch', '?') for b in tree]
elif isinstance(tree, dict):
    branches = len(tree)
    names = list(tree.keys())
else:
    branches = 0
    names = []

print(f'{count} memories in {branches} branches (generated {generated})')
print(f'Branches: {\", \".join(names[:15])}')
print(f'Full tree: GET ${MEMORY_API_URL:-http://memory-api:8897}/memory/tree')
" 2>/dev/null <<< "$TREE")

# --- Gather shared raw data once ---
log "Gathering system map data..."

# Discord channels (rescue bot token) — fetch once, filter per agent
DISCORD_TOKEN_FILE="$HOME/.secrets/rescue-discord-token"
DISCORD_GUILD="1468961665390350336"
DISCORD_RAW=""
if [ -f "$DISCORD_TOKEN_FILE" ]; then
    DISCORD_RAW=$(curl -sf "https://discord.com/api/v10/guilds/$DISCORD_GUILD/channels" \
        -H "Authorization: Bot $(cat "$DISCORD_TOKEN_FILE")" 2>/dev/null)
fi

# Services (from ss) — fetch once, filter per agent
SERVICES_RAW=$(ss -tlnp 2>/dev/null | grep -v 'State')

# Cron schedule — fetch once
CRON_RAW=$(crontab -l 2>/dev/null | grep -v '^#' | grep -v '^$')

# Agent registry — same for all
AGENT_REGISTRY=$(python3 -c "
import json
with open('$AGENTS_JSON') as f:
    data = json.load(f)
for name, info in data['agents'].items():
    role = info.get('role', '?')
    groups = ', '.join(info.get('groups', []))
    desc = info.get('description', '')[:60]
    print(f'| {name} | {role} | {groups} | {desc} |')
" 2>/dev/null)

log "System map data gathered"

# --- Per-agent channel category filters ---
# talkie: all channels
# newsbot: Ops channels only (research, system, ops-alerts)
# luna: Ops channels + LEARNING channels
# nova: Ops channels + LEARNING channels

get_discord_channels() {
    local agent="$1"
    if [ -z "$DISCORD_RAW" ]; then
        echo "_No Discord data available_"
        return
    fi
    echo "$DISCORD_RAW" | python3 -c "
import json, sys

agent = '$agent'
try:
    channels = json.load(sys.stdin)
except:
    print('_Could not parse channels_')
    sys.exit(0)

cats = {}
texts = []
for ch in channels:
    if ch['type'] == 4:
        cats[ch['id']] = ch['name']
    elif ch['type'] == 0:
        texts.append(ch)

# Define category filters per agent
# None means 'all channels'
filters = {
    'talkie': None,
    'rescue': None,
    'newsbot': ['Ops'],
    'luna': ['Ops', 'LEARNING'],
    'nova': ['Ops', 'LEARNING'],
}

allowed_cats = filters.get(agent)

for ch in sorted(texts, key=lambda c: (c.get('parent_id','') or '', c.get('position',0))):
    cat = cats.get(ch.get('parent_id',''), 'uncategorized')
    # Strip emoji prefix for matching (e.g. '🚨 Ops' -> 'Ops')
    cat_clean = cat.split(' ', 1)[-1] if ' ' in cat else cat
    if allowed_cats is not None and cat_clean not in allowed_cats:
        continue
    print(f'| #{ch[\"name\"]} | \`{ch[\"id\"]}\` | {cat} |')
" 2>/dev/null
}

# --- Per-agent service filters ---
# talkie/rescue: all known services
# newsbot: memory API, auth proxy, credential proxy
# luna/nova: auth proxy, credential proxy

get_services() {
    local agent="$1"
    python3 -c "
import sys, re

agent = '$agent'

all_known = {
    '18789': ('OpenClaw Gateway', 'lan'),
    '18791': ('OC Gateway internal', 'loopback'),
    '18792': ('OC Gateway internal', 'loopback'),
    '8080': ('Signal CLI daemon', 'loopback'),
    '8897': ('Agent Services API (memory)', 'docker bridge'),
    '8898': ('Agent Services (secondary)', 'docker bridge'),
    '8899': ('Credential Proxy', 'all'),
    '8765': ('Dashboard (static)', 'tailscale'),
    '8766': ('Dashboard (API)', 'tailscale'),
    '9100': ('Auth Proxy', 'docker bridge'),
    '2222': ('SSH', 'all'),
}

# Which ports each agent cares about
port_filters = {
    'talkie': None,   # all
    'rescue': None,   # all
    'newsbot': ['8897', '8898', '9100', '8899'],
    'luna':    ['9100', '8899'],
    'nova':   ['9100', '8899'],
}

allowed = port_filters.get(agent)
raw = sys.stdin.read()

for line in raw.strip().split('
'):
    if not line.strip():
        continue
    m = re.search(r'(\S+):(\d+)\s', line)
    if m:
        bind, port = m.group(1), m.group(2)
        if port in all_known:
            if allowed is not None and port not in allowed:
                continue
            name, scope = all_known[port]
            print(f'| {name} | \`{port}\` | {scope} |')
" <<< "$SERVICES_RAW" 2>/dev/null
}

# --- Per-agent cron filters ---
# talkie/rescue: all crons
# newsbot: news-related crons + memory crons
# luna/nova: no crons (not relevant to tutoring)

get_crons() {
    local agent="$1"
    python3 -c "
import sys

agent = '$agent'

# newsbot only sees news-related and memory crons
newsbot_keywords = ['news', 'breaking', 'memory', 'context-gen']

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    parts = line.split(None, 5)
    if len(parts) < 6:
        continue
    sched = ' '.join(parts[:5])
    cmd = parts[5].split('>>')[0].split('2>&1')[0].strip()
    name = cmd.split('/')[-1]

    if agent in ('luna', 'nova'):
        continue  # tutors don't need cron info
    elif agent == 'newsbot':
        if not any(kw in name.lower() for kw in newsbot_keywords):
            continue

    print(f'| \`{sched}\` | {name} |')
" <<< "$CRON_RAW" 2>/dev/null
}

# --- Generate per-agent files ---
while IFS='|' read -r AGENT WORKSPACE; do
    log "Generating for $AGENT -> $WORKSPACE"

    # Ensure workspace exists
    mkdir -p "$WORKSPACE" 2>/dev/null

    # Fetch agent-scoped context from memory API
    CONTEXT=$(curl -sf "$API/memory/context?agent=$AGENT" 2>/dev/null)

    # Fetch session state
    STATE=$(curl -sf "$API/memory/session-state/$AGENT" 2>/dev/null)

    # Agent-specific Discord channels
    DISCORD_CHANNELS=$(get_discord_channels "$AGENT")
    if [ -z "$DISCORD_CHANNELS" ]; then
        DISCORD_CHANNELS="_No channels available_"
    fi

    # Agent-specific services
    SERVICES=$(get_services "$AGENT")

    # Agent-specific crons
    CRON_SCHEDULE=$(get_crons "$AGENT")

    # Decide whether to include memory tree
    # Luna and Nova use local file memory, not shared DB
    INCLUDE_MEMORY_TREE=true
    if [ "$AGENT" = "luna" ] || [ "$AGENT" = "nova" ]; then
        INCLUDE_MEMORY_TREE=false
    fi

    # Build the file
    OUTFILE="$WORKSPACE/MEMORY_CONTEXT.md"

    python3 << PYEOF > "$OUTFILE"
import json, sys
from datetime import datetime, timezone, timedelta

agent = "$AGENT"
include_tree = "$INCLUDE_MEMORY_TREE" == "true"

utc_now = datetime.now(timezone.utc)
cambodia_tz = timezone(timedelta(hours=7))
cambodia_now = utc_now.astimezone(cambodia_tz)
hour = cambodia_now.hour
if 5 <= hour < 12:
    period = "Morning"
elif 12 <= hour < 17:
    period = "Afternoon"
elif 17 <= hour < 22:
    period = "Evening"
else:
    period = "Night"

print("# Memory Context (Auto-Generated)")
print(f"_Updated: {utc_now.strftime('%Y-%m-%d %H:%M UTC')} — regenerated every 30 min by cron_")
print()
print("## Current Time Context")
print(f"UTC: {utc_now.strftime('%Y-%m-%d %H:%M')} | Cambodia (UTC+7): {cambodia_now.strftime('%H:%M')} | Period: {period}")
print()

# Session state
try:
    state = json.loads('''$(echo "$STATE" | sed "s/'/\\'/g")''')
    if state.get("state"):
        s = state["state"]
        print("## Last Session State")
        print(f"- **Task:** {s.get('task_summary', 'unknown')}")
        print(f"- **Status:** {s.get('status', 'unknown')}")
        if s.get("context"):
            print(f"- **Context:** {s['context']}")
        print(f"- _Saved: {state.get('updated_at', 'unknown')}_")
        print()
except:
    pass

# Memory tree (only for agents that use shared memory DB)
if include_tree:
    print("## Memory Tree")
    tree_text = '''$TREE_SUMMARY'''
    if tree_text.strip():
        print(tree_text)
    else:
        print("_No tree available — query: GET ${MEMORY_API_URL:-http://memory-api:8897}/memory/tree_")
    print()

    # Context (pinned, recent, conflicts)
    try:
        ctx = json.loads('''$(echo "$CONTEXT" | sed "s/'/\\'/g")''')

        pinned = ctx.get("pinned", [])
        if pinned:
            print("## Pinned Memories")
            for m in pinned[:10]:
                print(f"- [{m.get('category','')}] {m.get('content','')[:120]}")
            print()

        recent = ctx.get("recent", [])
        if recent:
            print("## Recent Memories")
            for m in recent[:8]:
                print(f"- [{m.get('category','')}] {m.get('content','')[:120]}")
            print()

        conflicts = ctx.get("conflicts", [])
        if conflicts:
            print(f"## Unresolved Conflicts ({len(conflicts)})")
            for c in conflicts[:5]:
                print(f"- {c.get('description','')[:120]}")
            print()
    except:
        pass
else:
    print("## Memory")
    print("_This agent uses local file-based memory (not shared memory DB)._")
    print()

# System Map
print("## System Map (Auto-Generated)")
print()

discord_channels = '''$DISCORD_CHANNELS'''
if discord_channels.strip() and discord_channels.strip() != "_No channels available_":
    print("### Discord Channels")
    print("| Channel | ID | Category |")
    print("|---------|----|---------:|")
    print(discord_channels)
    print()

services = '''$SERVICES'''
if services.strip():
    print("### Services")
    print("| Service | Port | Bind |")
    print("|---------|------|-----:|")
    print(services)
    print()

cron_schedule = '''$CRON_SCHEDULE'''
if cron_schedule.strip():
    print("### Cron Schedule")
    print("| Schedule | Script |")
    print("|----------|-------:|")
    print(cron_schedule)
    print()

print("### Agents")
print("| Name | Role | Groups | Description |")
print("|------|------|--------|------------:|")
print('''$AGENT_REGISTRY''')
print()

# Memory API — only for agents that use shared memory
if include_tree:
    print("## Memory API")
    print(f"Query memories: \`GET ${MEMORY_API_URL:-http://memory-api:8897}/memory/search?q=<query>&agent={agent}\`")
    print(f"Save session state: \`POST ${MEMORY_API_URL:-http://memory-api:8897}/memory/session-state\` with \`{{agent, task_summary, status, context}}\`")
PYEOF

    # Size check
    SIZE=$(wc -c < "$OUTFILE")
    if [ "$SIZE" -gt "$MAX_SIZE" ]; then
        log "WARNING: $OUTFILE is ${SIZE}B (>${MAX_SIZE}B), truncating recent memories"
        python3 -c "
import sys
with open(sys.argv[1]) as f:
    c = f.read()
start = c.find('## Recent Memories')
if start != -1:
    end = c.find('## ', start + 1)
    if end != -1:
        c = c[:start] + c[end:]
with open(sys.argv[1], 'w') as f:
    f.write(c)
" "$OUTFILE"
        SIZE=$(wc -c < "$OUTFILE")
    fi

    log "Written $OUTFILE (${SIZE}B)"

done <<< "$AGENTS"

log "Done. Generated context for all agents."
