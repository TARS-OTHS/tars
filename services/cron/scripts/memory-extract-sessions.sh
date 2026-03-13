#!/bin/bash
# memory-extract-sessions.sh — Extracts facts from OC session logs
# Runs every 30 min via cron. Fully mechanical — no agent cooperation needed.
# Uses watermarks to only process new messages. Skips compaction summaries.

API="${MEMORY_API_URL:-http://memory-api:8897}"
OC_AGENTS_DIR="${OPENCLAW_DIR:-/oc-config}/agents"
WATERMARK_DIR="${AGENT_SERVICES_DIR:-/app}/watermarks"
MIN_CONTENT_LENGTH=50  # Skip very short messages
MAX_CHARS_PER_RUN=8000  # Cap input to extraction per run to limit Claude CLI cost

log() { echo "[$(date -Iseconds)] extract-sessions: $*"; }

mkdir -p "$WATERMARK_DIR"

# Find all agent session directories
for AGENT_DIR in "$OC_AGENTS_DIR"/*/; do
    AGENT_NAME=$(basename "$AGENT_DIR")
    SESSIONS_DIR="$AGENT_DIR/sessions"
    
    if [ ! -d "$SESSIONS_DIR" ]; then
        continue
    fi
    
    log "Processing agent: $AGENT_NAME"
    
    # Find active session files (not .bak)
    for SESSION_FILE in "$SESSIONS_DIR"/*.jsonl; do
        [ -f "$SESSION_FILE" ] || continue
        # Skip bak files
        case "$SESSION_FILE" in *.bak*) continue ;; esac
        
        SESSION_ID=$(basename "$SESSION_FILE" .jsonl)
        WATERMARK_FILE="$WATERMARK_DIR/${AGENT_NAME}_${SESSION_ID}.watermark"
        
        # Read watermark (last processed line number)
        WATERMARK=0
        if [ -f "$WATERMARK_FILE" ]; then
            WATERMARK=$(cat "$WATERMARK_FILE")
        fi
        
        # Count total lines
        TOTAL_LINES=$(wc -l < "$SESSION_FILE")
        
        if [ "$TOTAL_LINES" -le "$WATERMARK" ]; then
            continue  # Nothing new
        fi
        
        log "Session $SESSION_ID: $TOTAL_LINES lines, watermark at $WATERMARK"
        
        # Extract new user/assistant messages, skip compaction entries
        CONVERSATION=$(python3 << PYEOF
import json, sys

session_file = "$SESSION_FILE"
watermark = $WATERMARK
max_chars = $MAX_CHARS_PER_RUN
min_len = $MIN_CONTENT_LENGTH

messages = []
total_chars = 0

with open(session_file) as f:
    for i, line in enumerate(f):
        if i < watermark:
            continue
        try:
            d = json.loads(line)
        except:
            continue
        
        # Skip compaction entries entirely
        if d.get("type") == "compaction":
            continue
        
        # Only process user and assistant messages
        if d.get("type") != "message":
            continue
        
        msg = d.get("message", {})
        role = msg.get("role", "")
        
        # Only user and assistant messages contain extractable content
        if role not in ("user", "assistant"):
            continue
        
        content = msg.get("content", "")
        if isinstance(content, list):
            # Extract text from content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = "
".join(text_parts)
        
        if not content or len(content) < min_len:
            continue
        
        # Skip system/internal messages
        if content.startswith("A new session was started"):
            continue
        if "Execute your Session Startup sequence" in content:
            continue
        
        # Truncate very long messages
        if len(content) > 2000:
            content = content[:2000] + "..."
        
        if total_chars + len(content) > max_chars:
            break  # Hit per-run cap
        
        messages.append(f"{role}: {content}")
        total_chars += len(content)

if messages:
    print("
---
".join(messages))
PYEOF
)
        
        if [ -z "$CONVERSATION" ] || [ ${#CONVERSATION} -lt "$MIN_CONTENT_LENGTH" ]; then
            # Update watermark even if nothing to extract (avoid reprocessing)
            echo "$TOTAL_LINES" > "$WATERMARK_FILE"
            continue
        fi
        
        log "Extracting from $SESSION_ID (${#CONVERSATION} chars of new conversation)"
        
        # Call extraction API
        RESULT=$(curl -sf -X POST "$API/memory/extract" \
            -H "Content-Type: application/json" \
            -d "$(python3 -c "
import json
conv = open('/dev/stdin').read()
print(json.dumps({'conversation': conv, 'agent': '$AGENT_NAME', 'session_id': '$SESSION_ID'}))
" <<< "$CONVERSATION")" 2>/dev/null)
        
        if [ $? -eq 0 ]; then
            EXTRACTED=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('extracted',0))" 2>/dev/null)
            log "Extracted $EXTRACTED facts from session $SESSION_ID"
        else
            log "WARNING: Extraction failed for session $SESSION_ID"
        fi
        
        # Update watermark
        echo "$TOTAL_LINES" > "$WATERMARK_FILE"
        
    done
done

# Update session index as side effect
IDX_DIR="${AGENT_SERVICES_DIR:-/app/data}"
python3 << IDXEOF
import json, glob, os

agents_dir = "${OC_AGENTS_DIR}"
idx_path = os.path.join("${IDX_DIR}", "session-index.json")

os.makedirs(os.path.dirname(idx_path), exist_ok=True)

# Load existing index
try:
    with open(idx_path) as f:
        index = json.load(f)
except:
    index = {}

for agent_dir in sorted(glob.glob(agents_dir + "/*")):
    agent = os.path.basename(agent_dir)
    sessions_dir = os.path.join(agent_dir, "sessions")
    if not os.path.isdir(sessions_dir):
        continue
    for sf in glob.glob(sessions_dir + "/*.jsonl"):
        if "bak" in sf:
            continue
        sid = os.path.basename(sf).replace(".jsonl", "")
        mtime = os.path.getmtime(sf)
        if sid in index and index[sid].get("mtime") == mtime:
            continue
        channel = None
        start_time = None
        end_time = None
        msg_count = 0
        with open(sf) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except:
                    continue
                if d.get("type") == "session" and not start_time:
                    start_time = d.get("timestamp")
                if d.get("type") == "message" and not channel:
                    msg = d.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list) and content:
                        txt = content[0].get("text", "") if isinstance(content[0], dict) else ""
                    else:
                        txt = str(content)
                    if "Discord Guild #" in txt:
                        try:
                            channel = txt.split("Discord Guild #")[1].split(" ")[0]
                        except:
                            pass
                    elif "Signal" in txt or "signal" in txt:
                        channel = "signal"
                if d.get("type") == "message":
                    role = d.get("message", {}).get("role", "")
                    if role in ("user", "assistant"):
                        msg_count += 1
                        ts = d.get("timestamp")
                        if ts:
                            end_time = ts
        index[sid] = {"agent": agent, "channel": channel, "start_time": start_time, "end_time": end_time, "message_count": msg_count, "file": sf, "mtime": mtime}

with open(idx_path, "w") as f:
    json.dump(index, f, indent=2)
IDXEOF
log "Session index updated."

log "Done."
