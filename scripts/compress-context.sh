#!/usr/bin/env bash
# Batch compress agent context files to reduce input tokens.
# Compresses CLAUDE.md, codex docs, and skill prompts.
# Idempotent — skips files unchanged since last compression.
#
# Usage: scripts/compress-context.sh [--dry-run] [--level lite|standard]
#
# Reads compression config from config.yaml. Respects per-agent overrides.
# Originals preserved as .original.md alongside compressed versions.

set -uo pipefail

TARS_DIR="${TARS_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
OVERLAY="${TARS_OVERLAY:-}"
LEVEL="standard"
DRY_RUN=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN="--dry-run"; shift ;;
        --level) LEVEL="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Check if compression is enabled in config
CONFIG="${OVERLAY:+$OVERLAY/config/config.yaml}"
CONFIG="${CONFIG:-$TARS_DIR/config/config.yaml}"

if [ -f "$CONFIG" ]; then
    # Simple YAML check — look for compression.enabled: false
    if grep -qE '^\s*enabled:\s*false' <(grep -A2 '^compression:' "$CONFIG" 2>/dev/null); then
        echo "[compress] Compression disabled in config. Exiting."
        exit 0
    fi
    # Read level from config if not overridden by CLI arg
    config_level=$(grep -A2 '^compression:' "$CONFIG" 2>/dev/null | grep -oP 'level:\s*\K\w+' || true)
    if [ -n "$config_level" ] && [ "$LEVEL" = "standard" ]; then
        LEVEL="$config_level"
    fi
fi

COMPRESSOR="$TARS_DIR/src/lib/compressor.py"

if [ ! -f "$COMPRESSOR" ]; then
    echo "[compress] ERROR: Compressor not found at $COMPRESSOR"
    exit 1
fi

compress_file() {
    local file="$1"
    local level="$2"

    if [ ! -f "$file" ]; then
        return
    fi

    # Skip if file has compression tag and original hasn't changed
    if grep -q "<!-- compressed:" "$file" 2>/dev/null; then
        local original="${file%.*}.original.${file##*.}"
        if [ -f "$original" ]; then
            # Check if original is newer than compressed
            if [ ! "$original" -nt "$file" ]; then
                echo "[compress] Skip: $(basename "$file") (unchanged)"
                return
            fi
        fi
    fi

    local result
    result=$(cd "$TARS_DIR" && python3 -c "
from src.lib.compressor import compress_file
r = compress_file('$file', level='$level'${DRY_RUN:+, dry_run=True})
print(f\"{r['original_tokens']} -> {r['compressed_tokens']} tokens ({r['saved_pct']}% saved)\")
" 2>&1)

    if [ $? -eq 0 ]; then
        echo "[compress] ${DRY_RUN:+[DRY RUN] }$(basename "$file"): $result"
    else
        echo "[compress] ERROR: $(basename "$file"): $result"
    fi
}

total=0

# --- Overlay agent CLAUDE.md files ---
if [ -n "$OVERLAY" ] && [ -d "$OVERLAY/agents" ]; then
    for agent_dir in "$OVERLAY"/agents/*/; do
        claude_md="$agent_dir/CLAUDE.md"
        if [ -f "$claude_md" ]; then
            compress_file "$claude_md" "$LEVEL"
            total=$((total + 1))
        fi
    done
fi

# --- Core agent CLAUDE.md files (if agents dir exists) ---
if [ -d "$TARS_DIR/agents" ]; then
    for agent_dir in "$TARS_DIR"/agents/*/; do
        claude_md="$agent_dir/CLAUDE.md"
        if [ -f "$claude_md" ]; then
            compress_file "$claude_md" "$LEVEL"
            total=$((total + 1))
        fi
    done
fi

# --- Codex docs ---
if [ -n "$OVERLAY" ] && [ -d "$OVERLAY/codex" ]; then
    while IFS= read -r -d '' f; do
        compress_file "$f" "$LEVEL"
        total=$((total + 1))
    done < <(find "$OVERLAY/codex" -name "*.md" -not -name "*.original.md" -print0)
fi

if [ -d "$TARS_DIR/codex" ]; then
    while IFS= read -r -d '' f; do
        compress_file "$f" "$LEVEL"
        total=$((total + 1))
    done < <(find "$TARS_DIR/codex" -name "*.md" -not -name "*.original.md" -print0)
fi

echo "[compress] Done. Processed $total files."
