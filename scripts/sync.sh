#!/usr/bin/env bash
# sync.sh — Install all dependencies across layers.
#
# Replaces bare `uv sync` in the deploy ritual. Ensures Layer 2 packages
# (declared in requirements.txt files alongside TARS_OTHS modules) survive
# Core dependency reconciliation.
#
# Usage:
#   scripts/sync.sh          # from /opt/tars, or
#   TARS_OTHS=... scripts/sync.sh   # with explicit layer 2 paths

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- Resolve layer paths from systemd if not in environment ---
# The service unit is the single source of truth for TARS_OTHS and TARS_OVERLAY.
# This fallback means manual deploys (sudo -u tars scripts/sync.sh) work without
# maintaining a separate shell profile export.
if [ -z "${TARS_OTHS:-}" ]; then
    TARS_OTHS=$(systemctl show tars.service -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | grep -oP '^TARS_OTHS=\K.*' || true)
    [ -n "$TARS_OTHS" ] && echo "[sync] TARS_OTHS resolved from systemd unit"
fi
if [ -z "${TARS_OVERLAY:-}" ]; then
    TARS_OVERLAY=$(systemctl show tars.service -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | grep -oP '^TARS_OVERLAY=\K.*' || true)
    [ -n "$TARS_OVERLAY" ] && echo "[sync] TARS_OVERLAY resolved from systemd unit"
fi

# --- Layer 1: Core ---
echo "[sync] Layer 1: uv sync (Core)"
uv sync

# --- Layer 2: TARS_OTHS modules ---
if [ -n "${TARS_OTHS:-}" ]; then
    IFS=':' read -ra oths_dirs <<< "$TARS_OTHS"
    for dir in "${oths_dirs[@]}"; do
        # TARS_OTHS entries point to module dirs (e.g. /opt/tars-oths/amazon).
        # requirements.txt lives in the module root.
        req="$dir/requirements.txt"
        if [ -f "$req" ]; then
            echo "[sync] Layer 2: installing $req"
            uv pip install -r "$req"
        fi
    done
else
    echo "[sync] Layer 2: TARS_OTHS not set, skipping"
fi

# --- Layer 3: Overlay ---
if [ -n "${TARS_OVERLAY:-}" ] && [ -f "$TARS_OVERLAY/requirements.txt" ]; then
    echo "[sync] Layer 3: installing $TARS_OVERLAY/requirements.txt"
    uv pip install -r "$TARS_OVERLAY/requirements.txt"
fi

echo "[sync] Done"
