#!/usr/bin/env bash
# cleanup-worktrees.sh — Archive completed tasks, remove stale worktrees, prune git
# Designed to run daily via cron at 3AM
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SEABONE_DIR="$PROJECT_DIR/.seabone"
ACTIVE_FILE="$SEABONE_DIR/active-tasks.json"
COMPLETED_FILE="$SEABONE_DIR/completed-tasks.json"
CONFIG_FILE="$SEABONE_DIR/config.json"
export PATH="$HOME/.local/bin:$PATH"

# Load env
if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi

CLEANUP_DAYS=$(jq -r '.auto_cleanup_days // 7' "$CONFIG_FILE")

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running Seabone cleanup..."

cd "$PROJECT_DIR"

# 1. Move terminal-state tasks from active to completed
echo "[1/4] Archiving finished tasks..."
ARCHIVED=0
ACTIVE_COUNT=$(jq length "$ACTIVE_FILE" 2>/dev/null || echo 0)

# Process in reverse to avoid index shifting
for i in $(seq $((ACTIVE_COUNT - 1)) -1 0); do
    STATUS=$(jq -r ".[$i].status" "$ACTIVE_FILE")
    case "$STATUS" in
        pr_created|no_changes|completed|max_retries_exceeded|error|timeout)
            TASK_ID=$(jq -r ".[$i].id" "$ACTIVE_FILE")
            echo "  Archiving: $TASK_ID ($STATUS)"

            # Add completed_at and move to completed
            TASK_JSON=$(jq ".[$i] + {\"completed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" "$ACTIVE_FILE")
            jq ". + [$TASK_JSON]" "$COMPLETED_FILE" > "${COMPLETED_FILE}.tmp" && mv "${COMPLETED_FILE}.tmp" "$COMPLETED_FILE"
            jq "del(.[$i])" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"
            ARCHIVED=$((ARCHIVED + 1))
            ;;
    esac
done
echo "  Archived $ARCHIVED tasks"

# 2. Kill orphaned tmux sessions (sessions with no matching active task)
echo "[2/4] Cleaning orphaned tmux sessions..."
KILLED=0
for SESSION in $(tmux list-sessions -F "#{session_name}" 2>/dev/null | grep "^agent-" || true); do
    TASK_ID="${SESSION#agent-}"
    if ! jq -e ".[] | select(.id == \"$TASK_ID\")" "$ACTIVE_FILE" > /dev/null 2>&1; then
        echo "  Killing orphaned session: $SESSION"
        tmux kill-session -t "$SESSION" 2>/dev/null || true
        KILLED=$((KILLED + 1))
    fi
done
echo "  Killed $KILLED orphaned sessions"

# 3. Remove old worktrees
echo "[3/4] Removing stale worktrees..."
REMOVED=0
WORKTREE_BASE="$PROJECT_DIR/.worktrees"
if [[ -d "$WORKTREE_BASE" ]]; then
    for WT_DIR in "$WORKTREE_BASE"/*/; do
        [[ -d "$WT_DIR" ]] || continue
        TASK_ID=$(basename "$WT_DIR")

        # Check if task is still active
        if jq -e ".[] | select(.id == \"$TASK_ID\" and .status == \"running\")" "$ACTIVE_FILE" > /dev/null 2>&1; then
            continue
        fi

        # Check age
        if [[ -f "$WT_DIR/.agent-run.sh" ]]; then
            FILE_AGE_DAYS=$(( ($(date +%s) - $(stat -c %Y "$WT_DIR/.agent-run.sh" 2>/dev/null || echo 0)) / 86400 ))
            if [[ "$FILE_AGE_DAYS" -lt "$CLEANUP_DAYS" ]]; then
                continue
            fi
        fi

        echo "  Removing worktree: $TASK_ID"
        git worktree remove "$WT_DIR" --force 2>/dev/null || rm -rf "$WT_DIR"
        REMOVED=$((REMOVED + 1))
    done
fi
echo "  Removed $REMOVED worktrees"

# 4. Git prune
echo "[4/4] Pruning git..."
git worktree prune 2>/dev/null || true
git gc --auto --quiet 2>/dev/null || true

echo ""
echo "[DONE] Cleanup complete. Archived: $ARCHIVED, Killed sessions: $KILLED, Removed worktrees: $REMOVED"

"$SCRIPT_DIR/notify-telegram.sh" "🧹 *Seabone Cleanup*: Archived $ARCHIVED tasks, removed $REMOVED worktrees, killed $KILLED orphaned sessions." 2>/dev/null || true
