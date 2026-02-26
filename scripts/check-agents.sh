#!/usr/bin/env bash
# check-agents.sh — Monitor agent health, auto-respawn failures, notify on issues
# Designed to run via cron every 10 minutes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SEABONE_DIR="$PROJECT_DIR/.seabone"
ACTIVE_FILE="$SEABONE_DIR/active-tasks.json"
CONFIG_FILE="$SEABONE_DIR/config.json"
LOG_DIR="$SEABONE_DIR/logs"
export PATH="$HOME/.local/bin:$PATH"

# Load env
if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi

MAX_RETRIES=$(jq -r '.max_retries // 3' "$CONFIG_FILE")
TIMEOUT_MIN=$(jq -r '.agent_timeout_minutes // 30' "$CONFIG_FILE")

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running Seabone agent health check..."

ACTIVE_COUNT=$(jq length "$ACTIVE_FILE" 2>/dev/null || echo 0)
if [[ "$ACTIVE_COUNT" -eq 0 ]]; then
    echo "  No active tasks. All clear."
    exit 0
fi

ISSUES=0
RESPAWNED=0

# Iterate over active tasks
for i in $(seq 0 $((ACTIVE_COUNT - 1))); do
    TASK_ID=$(jq -r ".[$i].id" "$ACTIVE_FILE")
    SESSION=$(jq -r ".[$i].session" "$ACTIVE_FILE")
    STATUS=$(jq -r ".[$i].status" "$ACTIVE_FILE")
    RETRIES=$(jq -r ".[$i].retries" "$ACTIVE_FILE")
    BRANCH=$(jq -r ".[$i].branch" "$ACTIVE_FILE")
    DESC=$(jq -r ".[$i].description" "$ACTIVE_FILE")
    WORKTREE=$(jq -r ".[$i].worktree" "$ACTIVE_FILE")
    MODEL=$(jq -r ".[$i].model" "$ACTIVE_FILE")
    STARTED=$(jq -r ".[$i].started_at" "$ACTIVE_FILE")

    echo ""
    echo "  Checking task: $TASK_ID ($STATUS)"

    # Skip tasks that already completed/PR'd
    if [[ "$STATUS" == "pr_created" || "$STATUS" == "no_changes" || "$STATUS" == "completed" ]]; then
        echo "    Status is $STATUS — moving to completed"
        TASK_JSON=$(jq ".[$i] + {\"completed_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" "$ACTIVE_FILE")

        # Add to completed
        COMPLETED_FILE="$SEABONE_DIR/completed-tasks.json"
        jq ". + [$TASK_JSON]" "$COMPLETED_FILE" > "${COMPLETED_FILE}.tmp" && mv "${COMPLETED_FILE}.tmp" "$COMPLETED_FILE"

        # Remove from active
        jq "del(.[$i])" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"
        continue
    fi

    # Check if tmux session is alive
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "    tmux session alive"

        # Update heartbeat
        jq "(.[$i].last_heartbeat) = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"

        # Check for timeout
        STARTED_EPOCH=$(date -d "$STARTED" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$STARTED" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        ELAPSED_MIN=$(( (NOW_EPOCH - STARTED_EPOCH) / 60 ))

        if [[ "$ELAPSED_MIN" -gt "$TIMEOUT_MIN" ]]; then
            echo "    [TIMEOUT] Agent running for ${ELAPSED_MIN}m (limit: ${TIMEOUT_MIN}m)"
            tmux kill-session -t "$SESSION" 2>/dev/null || true
            jq "(.[$i].status) = \"timeout\"" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"
            ISSUES=$((ISSUES + 1))

            "$SCRIPT_DIR/notify-telegram.sh" "⏰ *Seabone Agent Timeout*: \`$TASK_ID\`
Running for ${ELAPSED_MIN}m (limit: ${TIMEOUT_MIN}m)
Task: $DESC" 2>/dev/null || true
        fi
    else
        echo "    [DEAD] tmux session not found"

        # Check if it completed successfully (status might have been updated)
        CURRENT_STATUS=$(jq -r ".[$i].status" "$ACTIVE_FILE")
        if [[ "$CURRENT_STATUS" == "pr_created" || "$CURRENT_STATUS" == "completed" ]]; then
            echo "    Task completed while we were checking. Skipping."
            continue
        fi

        # Agent died — check retry count
        if [[ "$RETRIES" -lt "$MAX_RETRIES" ]]; then
            NEW_RETRIES=$((RETRIES + 1))
            echo "    [RESPAWN] Retry $NEW_RETRIES/$MAX_RETRIES"

            # Update retry count and status
            jq "(.[$i].retries) = $NEW_RETRIES | (.[$i].status) = \"running\" | (.[$i].started_at) = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"

            # Respawn in tmux
            if [[ -f "$WORKTREE/.agent-run.sh" ]]; then
                tmux new-session -d -s "$SESSION" "bash $WORKTREE/.agent-run.sh"
                RESPAWNED=$((RESPAWNED + 1))
                echo "    Respawned successfully"

                "$SCRIPT_DIR/notify-telegram.sh" "🔄 *Seabone Agent Respawned*: \`$TASK_ID\` (retry $NEW_RETRIES/$MAX_RETRIES)
Task: $DESC" 2>/dev/null || true
            else
                echo "    [ERROR] Agent script not found at $WORKTREE/.agent-run.sh"
                jq "(.[$i].status) = \"error\"" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"
                ISSUES=$((ISSUES + 1))
            fi
        else
            echo "    [FAILED] Max retries ($MAX_RETRIES) exceeded"
            jq "(.[$i].status) = \"max_retries_exceeded\"" "$ACTIVE_FILE" > "${ACTIVE_FILE}.tmp" && mv "${ACTIVE_FILE}.tmp" "$ACTIVE_FILE"
            ISSUES=$((ISSUES + 1))

            "$SCRIPT_DIR/notify-telegram.sh" "💀 *Seabone Agent Failed*: \`$TASK_ID\` after $MAX_RETRIES retries.
Task: $DESC
Check logs: $LOG_DIR/${TASK_ID}.log" 2>/dev/null || true
        fi
    fi
done

echo ""
echo "[DONE] Checked $ACTIVE_COUNT tasks. Issues: $ISSUES, Respawned: $RESPAWNED"

# Check CI status for any open agent PRs
echo ""
echo "Checking CI status for agent PRs..."
gh pr list --search "head:agent/" --json number,headRefName,statusCheckRollup --jq '.[] | "\(.number) \(.headRefName) \(.statusCheckRollup | map(.state) | join(","))"' 2>/dev/null | while read -r PR_NUM BRANCH_NAME CI_STATUS; do
    if echo "$CI_STATUS" | grep -q "FAILURE"; then
        echo "  PR #$PR_NUM ($BRANCH_NAME): CI FAILED"
        "$SCRIPT_DIR/notify-telegram.sh" "🔴 *CI Failed* on PR #$PR_NUM (\`$BRANCH_NAME\`)" 2>/dev/null || true
    fi
done || echo "  (could not check CI — gh not authenticated?)"
