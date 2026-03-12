#!/bin/bash
# session-state-cron.sh — Auto-generates session state from active OC session logs
# Safety net for when agents forget to self-report state.
# Runs every 15 min via cron. Fully mechanical — no agent cooperation needed.
# Only processes sessions active in the last 60 min.

API="${MEMORY_API_URL:-http://memory-api:8897}"
OC_AGENTS_DIR="${OPENCLAW_DIR:-/app/config}/agents"
AUTH_PROFILES="${OPENCLAW_DIR:-/app/config}/agents/main/agent/auth-profiles.json"
MAX_CHARS=3000          # Cap input to Claude CLI to limit cost
ACTIVE_WINDOW_MIN=60    # Only process sessions modified within this many minutes
MAX_MESSAGES=12         # Read last N messages from session tail
TMPDIR="/tmp/session-state-work"

log() { echo "[$(date -Iseconds)] session-state: $*"; }

mkdir -p "$TMPDIR"

# Find all agent session directories
for AGENT_DIR in "$OC_AGENTS_DIR"/*/; do
    AGENT_NAME=$(basename "$AGENT_DIR")
    SESSIONS_DIR="$AGENT_DIR/sessions"

    if [ ! -d "$SESSIONS_DIR" ]; then
        continue
    fi

    # Find the most recently modified .jsonl file (active session)
    LATEST_SESSION=$(find "$SESSIONS_DIR" -name '*.jsonl' -not -name '*.bak*' \
        -mmin -"$ACTIVE_WINDOW_MIN" -printf '%T@ %p
' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

    if [ -z "$LATEST_SESSION" ]; then
        continue  # No active session for this agent
    fi

    SESSION_ID=$(basename "$LATEST_SESSION" .jsonl)

    # Extract channel from session key in sessions.json
    SESSION_CHANNEL=""
    if [ -f "$SESSIONS_DIR/sessions.json" ]; then
        SESSION_CHANNEL=$(python3 -c "
import json, sys
try:
    d = json.load(open('$SESSIONS_DIR/sessions.json'))
    for key, val in d.items():
        sid = val.get('sessionId') or val.get('id', '')
        if sid == '$SESSION_ID':
            # Extract channel: agent:X:discord:channel:123 -> discord:channel:123
            # agent:X:main -> main
            parts = key.split(':', 2)
            if len(parts) >= 3:
                print(parts[2])
            else:
                print('main')
            break
except: pass
" 2>/dev/null)
    fi
    SESSION_CHANNEL="${SESSION_CHANNEL:-main}"
    log "Active session for $AGENT_NAME: $SESSION_ID (channel: $SESSION_CHANNEL)"

    # Extract last N user/assistant messages into a temp file
    python3 - "$LATEST_SESSION" "$MAX_MESSAGES" "$MAX_CHARS" "$TMPDIR/conv_${AGENT_NAME}.txt" << 'PYEOF'
import json, sys

session_file = sys.argv[1]
max_messages = int(sys.argv[2])
max_chars = int(sys.argv[3])
outfile = sys.argv[4]

messages = []

with open(session_file) as f:
    lines = f.readlines()

# Read backwards to get the most recent messages
for line in reversed(lines):
    if len(messages) >= max_messages:
        break
    try:
        d = json.loads(line)
    except:
        continue

    if d.get("type") == "compaction":
        continue
    if d.get("type") != "message":
        continue

    msg = d.get("message", {})
    role = msg.get("role", "")
    if role not in ("user", "assistant"):
        continue

    content = msg.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        content = "
".join(text_parts)

    if not content or len(content) < 20:
        continue

    # Skip system/startup messages
    if "Execute your Session Startup" in content:
        continue
    if content.startswith("A new session was started"):
        continue

    # Truncate long messages
    if len(content) > 800:
        content = content[:800] + "..."

    messages.append(f"{role}: {content}")

# Reverse back to chronological order
messages.reverse()

if messages:
    output = "
---
".join(messages)
    # Final cap
    if len(output) > max_chars:
        output = output[-max_chars:]
    with open(outfile, 'w') as f:
        f.write(output)
else:
    # Write empty file
    with open(outfile, 'w') as f:
        pass
PYEOF

    CONV_FILE="$TMPDIR/conv_${AGENT_NAME}.txt"
    CONV_SIZE=$(wc -c < "$CONV_FILE" 2>/dev/null || echo 0)

    if [ "$CONV_SIZE" -lt 30 ]; then
        log "Skipping $AGENT_NAME — not enough conversation content"
        continue
    fi

    log "Summarizing session for $AGENT_NAME (${CONV_SIZE} bytes)"

    # Build the prompt file
    PROMPT_FILE="$TMPDIR/prompt_${AGENT_NAME}.txt"
    cat > "$PROMPT_FILE" << 'PROMPTEOF'
You are generating a session state snapshot for an AI agent. This will be used to help the agent resume work if it gets reset.

Read this recent conversation and produce a JSON object with exactly these fields:
- task_summary: What the agent is currently working on (1-2 sentences, specific and actionable)
- status: One of: active, completed, blocked, idle
- context: Key details needed for resumption — specific file names, error messages, decisions made, next steps (2-3 sentences max)

Return ONLY the JSON object, no markdown, no explanation.

Recent conversation:
PROMPTEOF
    cat "$CONV_FILE" >> "$PROMPT_FILE"

    # Use Anthropic API directly (reads token from OpenClaw auth-profiles.json)
    SUMMARY_FILE="$TMPDIR/summary_${AGENT_NAME}.json"
    API_TOKEN=$(python3 -c "import json; d=json.load(open('$AUTH_PROFILES')); p=d.get('profiles',{}).get('anthropic:manual') or d.get('profiles',{}).get('anthropic:default'); print(p.get('token') or p.get('key') or p.get('access'))" 2>/dev/null)
    if [ -z "$API_TOKEN" ]; then
        log "WARNING: Could not read Anthropic token from $AUTH_PROFILES"
        continue
    fi

    PROMPT_CONTENT=$(cat "$PROMPT_FILE")
    API_BODY=$(python3 -c "
import json, sys
prompt = sys.stdin.read()
print(json.dumps({
    'model': 'claude-sonnet-4-20250514',
    'max_tokens': 4096,
    'messages': [{'role': 'user', 'content': prompt}]
}))
" <<< "$PROMPT_CONTENT")

    # Detect OAuth token (sk-ant-oat) vs API key and use correct auth
    if [[ "$API_TOKEN" == *"sk-ant-oat"* ]]; then
        API_RESPONSE=$(curl -s -X POST "https://api.anthropic.com/v1/messages" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $API_TOKEN" \
            -H "anthropic-version: 2023-06-01" \
            -H "anthropic-beta: claude-code-20250219,oauth-2025-04-20" \
            -H "anthropic-dangerous-direct-browser-access: true" \
            -H "user-agent: session-state-cron/1.0 (external, cli)" \
            -H "x-app: cli" \
            -d "$API_BODY" 2>/dev/null)
    else
        API_RESPONSE=$(curl -s -X POST "https://api.anthropic.com/v1/messages" \
            -H "Content-Type: application/json" \
            -H "x-api-key: $API_TOKEN" \
            -H "anthropic-version: 2023-06-01" \
            -d "$API_BODY" 2>/dev/null)
    fi

    # Extract text from API response
    python3 -c "
import json, sys
resp = json.loads(sys.stdin.read())
if 'error' in resp:
    print(f'API error: {resp["error"]}', file=sys.stderr)
    sys.exit(1)
text = ''.join(b.get('text','') for b in resp.get('content',[]) if b.get('type')=='text')
print(text)
" <<< "$API_RESPONSE" > "$SUMMARY_FILE" 2>/dev/null

    if [ $? -ne 0 ] || [ ! -s "$SUMMARY_FILE" ]; then
        log "WARNING: Anthropic API failed for $AGENT_NAME"
        continue
    fi

    # (empty check handled above)

    # Parse the summary and POST to session-state API
    SESSION_CHANNEL="$SESSION_CHANNEL" python3 - "$SUMMARY_FILE" "$AGENT_NAME" "$API" << 'PYEOF'
import json, sys, re, os, urllib.request

summary_file = sys.argv[1]
agent = sys.argv[2]
api_base = sys.argv[3]

with open(summary_file) as f:
    raw = f.read()

# Extract JSON object
match = re.search(r'\{[\s\S]*\}', raw)
if not match:
    print(f"ERROR: No JSON found in summary", file=sys.stderr)
    sys.exit(1)

try:
    d = json.loads(match.group(0))
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

task = d.get("task_summary", "")
status = d.get("status", "active")
context = d.get("context", "")

if not task:
    print("ERROR: No task_summary in parsed JSON", file=sys.stderr)
    sys.exit(1)

# POST to session-state API
channel = os.environ.get("SESSION_CHANNEL", "")
payload_dict = {
    "agent": agent,
    "task_summary": task,
    "status": status,
    "context": context
}
if channel and channel != "main":
    payload_dict["channel"] = channel
payload = json.dumps(payload_dict).encode()

req = urllib.request.Request(
    f"{api_base}/memory/session-state",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        action = result.get("action", "unknown")
        print(f"{action}: {task}")
except Exception as e:
    print(f"ERROR: API POST failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

    if [ $? -eq 0 ]; then
        log "Session state saved for $AGENT_NAME"
    else
        log "WARNING: Failed to save session state for $AGENT_NAME"
    fi

done

# Cleanup temp files
rm -f "$TMPDIR"/conv_*.txt "$TMPDIR"/prompt_*.txt "$TMPDIR"/summary_*.json

log "Done."
