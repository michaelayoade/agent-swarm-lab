#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

WORKTREE_DIR="/home/dotmac/projects/agent-swarm-lab/.worktrees/test-msg-123"
PROJECT_DIR="/home/dotmac/projects/agent-swarm-lab"
SCRIPT_DIR="/home/dotmac/projects/agent-swarm-lab/scripts"
ACTIVE_FILE="/home/dotmac/projects/agent-swarm-lab/.seabone/active-tasks.json"
LOG_FILE="/home/dotmac/projects/agent-swarm-lab/.seabone/logs/test-msg-123.log"
TASK_ID="test-msg-123"
DESCRIPTION="Tell me about the dotmac platform"
BRANCH="agent/test-msg-123"
MODEL="deepseek-chat"
EVENT_LOG="/home/dotmac/projects/agent-swarm-lab/.seabone/logs/events.log"
CONFIG_FILE="/home/dotmac/projects/agent-swarm-lab/.seabone/config.json"
PROJECT_NAME="/home/dotmac/projects/agent-swarm-lab_NAME"

if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi

source "$SCRIPT_DIR/json-lock.sh"

log_event() {
    local event="$1"
    local status="$2"
    local detail="$3"
    local ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' "$(jq -n --arg ts "$ts" --arg project "$PROJECT_NAME" --arg task_id "$TASK_ID" --arg event "$event" --arg status "$status" --arg detail "$detail" '{ts:$ts,project:$project,task_id:$task_id,event:$event,status:$status,detail:$detail}')" >> "$EVENT_LOG"
}

set_status() {
    local status="$1"
    json_update "$ACTIVE_FILE" "(.[] | select(.id == \"$TASK_ID\") | .status) = \"$status\""
    json_update "$ACTIVE_FILE" "(.[] | select(.id == \"$TASK_ID\") | .last_heartbeat) = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    log_event "status" "$status" "updated"
}

run_quality_gates() {
    local gates
    local changed_files

    changed_files="$(git diff --name-only)"
    if [[ -z "$(printf '%s' "$changed_files" | tr -d '[:space:]')" ]]; then
        return 0
    fi

    gates="$(jq -c '.quality_gates // []' "$CONFIG_FILE")"
    if [[ "$gates" == "null" || "$gates" == "[]" ]]; then
        return 0
    fi

    local attempt=1
    local max_retries
    local gate_cmd
    local passed=0

    max_retries="$(jq -r '.quality_gate_retries // 1' "$CONFIG_FILE")"
    [[ "$max_retries" =~ ^[0-9]+$ ]] || max_retries=1
    (( max_retries < 1 )) && max_retries=1

    while (( attempt <= max_retries )); do
        passed=1
        while IFS= read -r gate_cmd; do
            if ! SEABONE_CHANGED_FILES="$changed_files" bash -lc "$gate_cmd" >> "$LOG_FILE" 2>&1; then
                passed=0
                break
            fi
        done < <(echo "$gates" | jq -r '.[]')

        if (( passed == 1 )); then
            return 0
        fi

        attempt=$(( attempt + 1 ))
    done

    return 1
}

cd "$WORKTREE_DIR"

echo "=== Seabone Agent: $TASK_ID ==="
echo "Task: $DESCRIPTION"
echo "Branch: $BRANCH"
echo "Model: $MODEL"

aider --model "openai/$MODEL" \
    --no-auto-commits \
    --yes-always \
    --no-show-model-warnings \
    --no-detect-urls \
    --subtree-only \
    --map-tokens 1024 \
    --message "$DESCRIPTION" \
    2>&1 | tee -a "$LOG_FILE"
AIDER_EXIT=${PIPESTATUS[0]}

if [[ $AIDER_EXIT -ne 0 ]]; then
    set_status failed
    log_event "aider" "failed" "exit-$AIDER_EXIT"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone Agent*: \`$TASK_ID\` failed (exit $AIDER_EXIT)." 2>/dev/null || true
    exit 1
fi

if git diff --quiet && git diff --cached --quiet; then
    set_status no_changes
    log_event "completion" "no_changes" "No diff produced"
    "$SCRIPT_DIR/notify-telegram.sh" "⚠️ *Seabone Agent*: \`$TASK_ID\` finished with no changes." 2>/dev/null || true
    exit 0
fi

if ! run_quality_gates; then
    set_status quality_failed
    log_event "completion" "quality_failed" "Quality gates failed"
    "$SCRIPT_DIR/notify-telegram.sh" "🔴 *Seabone Agent*: \`$TASK_ID\` quality gates failed." 2>/dev/null || true
    exit 2
fi

git add -A

git commit -m "feat($TASK_ID): $DESCRIPTION" -m "Automated by Seabone agent swarm (aider + $MODEL)"

git push -u origin "$BRANCH"

PR_URL=$(gh pr create \
    --title "[$TASK_ID] $DESCRIPTION" \
    --body "## Summary\nAutomated PR created by Seabone agent swarm.\n\n**Task:** $DESCRIPTION\n**Model:** $MODEL\n**Branch:** \`$BRANCH\`" \
    --head "$BRANCH" 2>&1) || PR_URL="PR creation failed"

if [[ "$PR_URL" == "PR creation failed" ]]; then
    set_status failed
    log_event "completion" "failed" "pr creation failed"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone Agent*: \`$TASK_ID\` PR creation failed." 2>/dev/null || true
    exit 1
fi

set_status pr_created
log_event "completion" "pr_created" "$PR_URL"
"$SCRIPT_DIR/notify-telegram.sh" "✅ *Seabone Agent*: \`$TASK_ID\` completed\nPR: $PR_URL" 2>/dev/null || true
exit 0
