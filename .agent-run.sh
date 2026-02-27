#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
WORKTREE_DIR=/home/dotmac/projects/agent-swarm-lab/.worktrees/confusion-detection-system
PROJECT_DIR=/home/dotmac/projects/agent-swarm-lab
SCRIPT_DIR=/home/dotmac/projects/agent-swarm-lab/scripts
ACTIVE_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/active-tasks.json
LOG_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/logs/confusion-detection-system.log
TASK_ID=confusion-detection-system
DESCRIPTION=$'Implement confusion detection logic for agents. Create a system that monitors agent activity and detects when an agent is confused (spinning in loops, making no progress, hitting token limits). The system should:\n\n1. Add confusion detection to scripts/spawn-agent.sh:\n   - Monitor aider output for patterns indicating confusion\n   - Track token usage and detect when limits are approached\n   - Set a maximum confusion attempts threshold (configurable)\n   - Log confusion events with timestamps and reasons\n\n2. Create confusion detection functions:\n   - detect_confusion(): analyzes aider logs for confusion patterns\n   - should_escalate(): determines if escalation is needed\n   - log_confusion_event(): logs confusion to a structured file\n\n3. Update configuration in .seabone/config.json:\n   - Add confusion detection settings\n   - Add thresholds and timeouts\n\n4. Create a simple confusion log file format in .seabone/confusion-log.json\n\nFocus ONLY on detection logic first. Do NOT implement escalation yet. Keep changes minimal and focused on detection only.'
BRANCH=agent/confusion-detection-system
MODEL=deepseek-chat
EVENT_LOG=/home/dotmac/projects/agent-swarm-lab/.seabone/logs/events.log
CONFIG_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/config.json
PROJECT_NAME=agent-swarm-lab

if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi

# Default aider's OpenAI-compatible endpoint to DeepSeek when no override is set.
if [[ -n "${DEEPSEEK_API_KEY:-}" ]]; then
    export OPENAI_API_KEY="${OPENAI_API_KEY:-$DEEPSEEK_API_KEY}"
    export OPENAI_API_BASE="${OPENAI_API_BASE:-https://api.deepseek.com}"
fi

source "$SCRIPT_DIR/json-lock.sh"

log_event() {
    local event="$1"
    local status="$2"
    local detail="$3"
    local ts project_slug
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    project_slug="${PROJECT_NAME:-}"
    if [[ -z "$project_slug" || "$project_slug" == */* ]]; then
        project_slug="$(basename "$PROJECT_DIR")"
    fi
    printf '%s\n' "$(jq -n --arg ts "$ts" --arg project "$project_slug" --arg task_id "$TASK_ID" --arg event "$event" --arg status "$status" --arg detail "$detail" '{ts:$ts,project:$project,task_id:$task_id,event:$event,status:$status,detail:$detail}')" >> "$EVENT_LOG"
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

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "[ERROR] OPENAI_API_KEY is not set. Configure OPENAI_API_KEY or DEEPSEEK_API_KEY in .env.agent-swarm." | tee -a "$LOG_FILE"
    set_status failed
    log_event "aider" "failed" "missing-openai-api-key"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone Agent*: \`$TASK_ID\` missing API credentials." 2>/dev/null || true
    exit 1
fi

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
