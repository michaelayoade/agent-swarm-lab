#!/usr/bin/env bash
# Thin wrapper — delegates to shared canonical list-tasks.sh
export SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
exec /home/dotmac/projects/shared-scripts/list-tasks.sh "$@"
