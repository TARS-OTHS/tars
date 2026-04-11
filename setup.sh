#!/usr/bin/env bash
# setup.sh is deprecated — use setup.py instead.
# This stub redirects to the Python wizard.
cd "$(dirname "$0")" && exec uv run python setup.py "$@"
