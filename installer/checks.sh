#!/usr/bin/env bash
# installer/checks.sh — Prerequisite checking helpers
# Sourced by setup.sh

check_docker_running() {
    if ! docker info &>/dev/null 2>&1; then
        print_error "Docker daemon is not running"
        echo "  Start it with: sudo systemctl start docker"
        exit 1
    fi
    print_success "Docker daemon running"
}

check_docker_compose() {
    if docker compose version &>/dev/null 2>&1; then
        print_success "docker compose (v2 plugin) found"
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        print_success "docker-compose (v1) found"
        COMPOSE_CMD="docker-compose"
    else
        print_error "docker compose not found — install docker-compose-plugin"
        exit 1
    fi
    export COMPOSE_CMD
}

check_ports_free() {
    local ports=(9100 8897 8896 8899 8765 8766)
    local blocked=()
    for port in "${ports[@]}"; do
        if ss -tlnp 2>/dev/null | grep -q ":$port " || \
           netstat -tlnp 2>/dev/null | grep -q ":$port "; then
            blocked+=("$port")
        fi
    done
    if [[ ${#blocked[@]} -gt 0 ]]; then
        print_warn "Ports already in use: ${blocked[*]}"
        print_info "These are the default TARS ports. Edit .env to use different ports if needed."
    else
        print_success "All default ports available"
    fi
}
