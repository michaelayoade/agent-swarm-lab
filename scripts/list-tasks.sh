#!/usr/bin/env bash
# list-tasks.sh — Quick task status viewer for Seabone agent swarm
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SEABONE_DIR="$PROJECT_DIR/.seabone"
ACTIVE_FILE="$SEABONE_DIR/active-tasks.json"
COMPLETED_FILE="$SEABONE_DIR/completed-tasks.json"

echo "=== SEABONE AGENT SWARM STATUS ==="
echo ""

# Active tasks
ACTIVE_COUNT=$(jq length "$ACTIVE_FILE" 2>/dev/null || echo 0)
echo "Active Tasks ($ACTIVE_COUNT):"
echo "---"
if [[ "$ACTIVE_COUNT" -gt 0 ]]; then
    jq -r '.[] | "  [\(.status)] \(.id) — \(.description) (branch: \(.branch), retries: \(.retries))"' "$ACTIVE_FILE"
else
    echo "  (none)"
fi
echo ""

# Check tmux sessions
echo "Tmux Agent Sessions:"
echo "---"
SESSIONS=$(tmux list-sessions 2>/dev/null | grep "^agent-" || echo "(none)")
echo "  $SESSIONS"
echo ""

# Completed tasks
COMPLETED_COUNT=$(jq length "$COMPLETED_FILE" 2>/dev/null || echo 0)
echo "Completed Tasks ($COMPLETED_COUNT):"
echo "---"
if [[ "$COMPLETED_COUNT" -gt 0 ]]; then
    jq -r '.[-5:] | .[] | "  [\(.status)] \(.id) — \(.description) (completed: \(.completed_at // "N/A"))"' "$COMPLETED_FILE"
    if [[ "$COMPLETED_COUNT" -gt 5 ]]; then
        echo "  ... and $((COMPLETED_COUNT - 5)) more"
    fi
else
    echo "  (none)"
fi
echo ""

# Git worktrees
echo "Git Worktrees:"
echo "---"
cd "$PROJECT_DIR"
git worktree list 2>/dev/null | grep -v "^$PROJECT_DIR " || echo "  (main only)"
echo ""

# Open PRs
echo "Open PRs (agent branches):"
echo "---"
gh pr list --search "head:agent/" --json number,title,headRefName,state 2>/dev/null | jq -r '.[] | "  #\(.number) [\(.state)] \(.title) (\(.headRefName))"' || echo "  (could not fetch — gh not authenticated?)"
