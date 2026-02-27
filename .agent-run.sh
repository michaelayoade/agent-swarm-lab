#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
WORKTREE_DIR=/home/dotmac/projects/agent-swarm-lab/.worktrees/improve-dotmac-erp
PROJECT_DIR=/home/dotmac/projects/agent-swarm-lab
SCRIPT_DIR=/home/dotmac/projects/agent-swarm-lab/scripts
ACTIVE_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/active-tasks.json
LOG_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/logs/improve-dotmac-erp.log
TASK_ID=improve-dotmac-erp
DESCRIPTION=$'First, check if the dotmac_erp repository exists at /home/dotmac/projects/dotmac_erp. If it doesn\'t exist, clone it from GitHub: Michaelayoade/dotmac_erp.\n\nOnce the repository is available, analyze the project structure and implement key improvements. Look for:\n\n1. **Project setup improvements**:\n   - Add/update README.md with clear documentation\n   - Set up proper .gitignore if missing\n   - Add license file if missing\n   - Set up basic project structure if needed\n\n2. **Code quality improvements**:\n   - Add linting configuration (ESLint, Prettier, Black, etc. based on tech stack)\n   - Set up basic testing framework if missing\n   - Add type definitions if applicable\n   - Improve error handling\n\n3. **Development workflow**:\n   - Add development scripts (dev, build, test, lint)\n   - Set up environment configuration (.env.example)\n   - Add Docker/docker-compose if appropriate\n   - Set up CI/CD basics if missing\n\n4. **Security improvements**:\n   - Check for hardcoded secrets\n   - Add security headers if web app\n   - Update dependencies if outdated\n\n5. **Performance improvements**:\n   - Optimize database queries if found\n   - Add caching if appropriate\n   - Optimize asset loading if web app\n\nStart by analyzing what exists, then implement the most valuable improvements based on the project\'s tech stack and current state. Create a PR with all changes.'
BRANCH=agent/improve-dotmac-erp
MODEL=deepseek-reasoner
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
