#!/usr/bin/env bash
# TARS Update Script — pull latest code, rebuild, restart
# Usage: ./scripts/update.sh [--no-rebuild] [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARS_HOME="${TARS_HOME:-$(dirname "$SCRIPT_DIR")}"
cd "$TARS_HOME"

# Colors
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}✓ $1${RESET}"; }
warn() { echo -e "${YELLOW}⚠ $1${RESET}"; }
err()  { echo -e "${RED}✗ $1${RESET}"; }
info() { echo -e "${BLUE}  $1${RESET}"; }

NO_REBUILD=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --no-rebuild) NO_REBUILD=true ;;
        --dry-run)    DRY_RUN=true ;;
    esac
done

echo -e "\n${BLUE}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BLUE}║  TARS Update                             ║${RESET}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${RESET}\n"

# --- 1. Pull latest code ---
info "Pulling latest from GitHub..."
BEFORE=$(git rev-parse HEAD)
git pull --ff-only 2>&1 | tail -5
AFTER=$(git rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    ok "Already up to date"
    if [[ "$NO_REBUILD" == true ]]; then
        echo "  Nothing to do."
        exit 0
    fi
else
    COMMITS=$(git log --oneline "$BEFORE".."$AFTER" | wc -l)
    ok "Updated ($COMMITS new commit(s))"
    echo
    git log --oneline "$BEFORE".."$AFTER"
    echo
fi

if [[ "$DRY_RUN" == true ]]; then
    info "Dry run — stopping here"
    exit 0
fi

# --- 2. Install/update plugin dependencies ---
info "Updating plugin dependencies..."
for plugin_dir in "$TARS_HOME"/plugins/*/; do
    if [[ -f "$plugin_dir/package.json" ]]; then
        plugin_name=$(basename "$plugin_dir")
        (cd "$plugin_dir" && npm install --silent 2>&1) && ok "Plugin: $plugin_name" || warn "Plugin $plugin_name: npm install had issues"
    fi
done

# --- 3. Run migrations if present ---
MIGRATION_DIR="$TARS_HOME/migrations"
MIGRATION_STATE="$TARS_HOME/.migration-state"
if [[ -d "$MIGRATION_DIR" ]]; then
    LAST_MIGRATION=$(cat "$MIGRATION_STATE" 2>/dev/null || echo "0")
    for migration in "$MIGRATION_DIR"/*.sh; do
        [[ -f "$migration" ]] || continue
        migration_id=$(basename "$migration" .sh | cut -d- -f1)
        if [[ "$migration_id" -gt "$LAST_MIGRATION" ]]; then
            info "Running migration: $(basename "$migration")"
            bash "$migration" && echo "$migration_id" > "$MIGRATION_STATE" && ok "Migration $migration_id applied" || { err "Migration $migration_id failed"; exit 1; }
        fi
    done
else
    info "No migrations directory — skipping"
fi

# --- 4. Rebuild Docker images if needed ---
if [[ "$NO_REBUILD" == false ]]; then
    # Check if any Dockerfiles or service code changed
    CHANGED_FILES=$(git diff --name-only "$BEFORE".."$AFTER" 2>/dev/null || echo "")
    NEEDS_REBUILD=false

    if [[ "$BEFORE" == "$AFTER" ]]; then
        info "No code changes — skipping Docker rebuild"
    elif echo "$CHANGED_FILES" | grep -qE '(Dockerfile|docker-compose|services/)'; then
        NEEDS_REBUILD=true
    fi

    if [[ "$NEEDS_REBUILD" == true ]]; then
        info "Rebuilding Docker images..."
        docker compose build 2>&1 | tail -5
        ok "Docker images rebuilt"
    else
        info "No service changes — skipping Docker rebuild"
    fi

    # Rebuild sandbox image if Dockerfile.sandbox changed
    if echo "$CHANGED_FILES" | grep -q 'Dockerfile.sandbox'; then
        info "Rebuilding sandbox image..."
        docker build -t tars-sandbox:base -f "$TARS_HOME/templates/Dockerfile.sandbox" "$TARS_HOME" 2>&1 | tail -5
        ok "Sandbox image rebuilt"
    fi
fi

# --- 5. Restart services ---
info "Restarting Docker services..."
docker compose up -d 2>&1 | tail -5
ok "Docker services restarted"

info "Restarting OpenClaw gateway..."
openclaw gateway restart 2>&1 | tail -1
ok "Gateway restarted"

# --- 6. Health checks ---
echo
info "Running health checks..."

wait_healthy() {
    local url="$1" name="$2" timeout="${3:-60}" elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        if curl -sf "$url" > /dev/null 2>&1; then
            ok "$name healthy"
            return 0
        fi
        sleep 2; elapsed=$((elapsed+2))
    done
    err "$name not responding after ${timeout}s"
    return 1
}

DOCKER_HOST_IP=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || echo "172.17.0.1")

wait_healthy "http://${DOCKER_HOST_IP}:${AUTH_PROXY_PORT:-9100}/ops/health" "auth-proxy" 30
wait_healthy "http://${DOCKER_HOST_IP}:${MEMORY_API_PORT:-8897}/status" "memory-api" 30
wait_healthy "http://${DOCKER_HOST_IP}:${EMBEDDING_PORT:-8896}/health" "embedding-service" 60
wait_healthy "http://${DOCKER_HOST_IP}:${MCP_GATEWAY_PORT:-12008}/" "mcp-gateway" 60

# --- 7. Regenerate memory context ---
if [[ -x "$TARS_HOME/scripts/regen-memory-context.sh" ]]; then
    info "Regenerating memory context..."
    DOCKER_HOST_IP="${DOCKER_HOST_IP}" "$TARS_HOME/scripts/regen-memory-context.sh" 2>/dev/null && ok "Memory context regenerated" || warn "Memory context regen skipped"
fi

echo
ok "TARS update complete"
echo "  Version: $(git describe --tags 2>/dev/null || git rev-parse --short HEAD)"
echo
