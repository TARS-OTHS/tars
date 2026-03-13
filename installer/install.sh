#!/usr/bin/env bash
# TARS Installer — from zero to setup wizard on a fresh Ubuntu/Debian server
# Usage: curl -fsSL https://raw.githubusercontent.com/TARS-OTHS/tars/main/installer/install.sh | bash
#
# What this does:
#   1. Installs system dependencies (docker, node, age, etc.)
#   2. Clones the TARS repo
#   3. Launches the setup wizard

set -euo pipefail

TARS_REPO="https://github.com/TARS-OTHS/tars.git"
TARS_DIR="/opt/tars"
NODE_MAJOR=20

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'
info()    { echo -e "${BLUE}[TARS]${RESET} $1"; }
success() { echo -e "${GREEN}[TARS]${RESET} $1"; }
warn()    { echo -e "${YELLOW}[TARS]${RESET} $1"; }
fail()    { echo -e "${RED}[TARS]${RESET} $1"; exit 1; }

# --- Root check ---
if [[ $EUID -ne 0 ]]; then
    fail "Run as root: curl -fsSL ... | sudo bash"
fi

# --- OS check ---
if [[ ! -f /etc/os-release ]]; then
    fail "Unsupported OS — requires Ubuntu 22.04+ or Debian 12+"
fi
source /etc/os-release
if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
    warn "Untested OS: $PRETTY_NAME — continuing anyway (Ubuntu/Debian recommended)"
fi

echo
echo -e "${BLUE}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BLUE}║  TARS Installer                          ║${RESET}"
echo -e "${BLUE}║  Trusted Agent Runtime Stack              ║${RESET}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${RESET}"
echo

# --- System dependencies ---
info "Updating package index..."
apt-get update -qq

install_if_missing() {
    local cmd="$1" pkg="${2:-$1}"
    if command -v "$cmd" &>/dev/null; then
        success "$cmd already installed"
    else
        info "Installing $pkg..."
        apt-get install -y -qq "$pkg" > /dev/null 2>&1
        if command -v "$cmd" &>/dev/null; then
            success "$cmd installed"
        else
            fail "Failed to install $pkg"
        fi
    fi
}

install_if_missing git git
install_if_missing curl curl
install_if_missing jq jq
install_if_missing age age

# --- Docker ---
if command -v docker &>/dev/null; then
    success "Docker already installed"
else
    info "Installing Docker..."
    # Docker official install script
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    success "Docker installed and started"
fi

# Docker Compose plugin
if docker compose version &>/dev/null 2>&1; then
    success "Docker Compose already installed"
else
    info "Installing Docker Compose plugin..."
    apt-get install -y -qq docker-compose-plugin > /dev/null 2>&1
    if docker compose version &>/dev/null 2>&1; then
        success "Docker Compose installed"
    else
        fail "Failed to install docker-compose-plugin"
    fi
fi

# Make sure Docker is running
if ! docker info &>/dev/null 2>&1; then
    info "Starting Docker daemon..."
    systemctl start docker
fi

# --- Node.js ---
if command -v node &>/dev/null; then
    node_ver=$(node --version | sed 's/v//' | cut -d. -f1)
    if [[ "$node_ver" -ge "$NODE_MAJOR" ]]; then
        success "Node.js v$(node --version | sed 's/v//') already installed"
    else
        warn "Node.js v$node_ver found — v${NODE_MAJOR}+ required, upgrading..."
        # Remove old and install new
        apt-get remove -y -qq nodejs > /dev/null 2>&1 || true
        curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -
        apt-get install -y -qq nodejs > /dev/null 2>&1
        success "Node.js v$(node --version | sed 's/v//') installed"
    fi
else
    info "Installing Node.js ${NODE_MAJOR}.x..."
    curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -
    apt-get install -y -qq nodejs > /dev/null 2>&1
    success "Node.js v$(node --version | sed 's/v//') installed"
fi

# --- gettext-base (for envsubst, used by cron service) ---
install_if_missing envsubst gettext-base

# --- System checks ---
echo
ram_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
disk_gb=$(df -BG /opt 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G')
if [[ "$ram_gb" -lt 2 ]]; then
    warn "RAM: ${ram_gb}GB — 2GB minimum, 4GB recommended"
else
    success "RAM: ${ram_gb}GB"
fi
if [[ "$disk_gb" -lt 20 ]]; then
    warn "Disk: ${disk_gb}GB free — 20GB recommended"
else
    success "Disk: ${disk_gb}GB free"
fi

# --- Clone TARS ---
echo
if [[ -d "$TARS_DIR/.git" ]]; then
    info "Existing TARS install found at $TARS_DIR"
    read -r -p "  Overwrite and start fresh? [y/N]: " ans
    if [[ "$ans" =~ ^[Yy] ]]; then
        rm -rf "$TARS_DIR"
    else
        info "Keeping existing install. Running setup wizard..."
        cd "$TARS_DIR"
        exec ./setup.sh
    fi
fi

info "Cloning TARS from GitHub..."
git clone "$TARS_REPO" "$TARS_DIR"
success "TARS cloned to $TARS_DIR"

# --- Launch setup wizard ---
echo
info "All dependencies installed. Launching setup wizard..."
echo
cd "$TARS_DIR"
exec ./setup.sh
