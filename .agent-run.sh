#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

# ---- Injected at spawn time ----
WORKTREE_DIR=/home/dotmac/projects/agent-swarm-lab/.worktrees/fix-security-001
PROJECT_DIR=/home/dotmac/projects/agent-swarm-lab
SCRIPT_DIR=/home/dotmac/projects/agent-swarm-lab/scripts
ACTIVE_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/active-tasks.json
LOG_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/logs/fix-security-001.log
TASK_ID=fix-security-001
DESCRIPTION=Replace\ hardcoded\ API\ key
BRANCH=agent/fix-security-001
ENGINE=claude
MODEL=sonnet
EVENT_LOG=/home/dotmac/projects/agent-swarm-lab/.seabone/logs/events.log
CONFIG_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/config.json
SHARED_CONTEXT_FILE=/home/dotmac/projects/agent-swarm-lab/.seabone/shared-context.json
PROJECT_NAME=agent-swarm-lab
PROMPTS_DIR=/home/dotmac/projects/agent-swarm-lab/.seabone/prompts

if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi
source "$SCRIPT_DIR/json-lock.sh"

log_event() {
    local event="$1" status="$2" detail="$3"
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
    if terminal_status "$status"; then
        archive_terminal_task "$status"
    fi
    log_event "status" "$status" "updated"
}

terminal_status() {
    local status="$1"
    case "$status" in
        pr_created|no_changes|completed|quality_failed|max_retries_exceeded|timeout|error|failed|killed|dod_passed|dod_failed)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

archive_terminal_task() {
    local status="$1"
    local now task_json session_name

    task_json="$(json_read "$ACTIVE_FILE" ".[] | select(.id == \"$TASK_ID\")" 2>/dev/null | head -n1 || true)"
    [[ -n "$task_json" ]] || return 0

    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    task_json="$(printf '%s' "$task_json" | jq --arg status "$status" --arg now "$now" '.status=$status | .completed_at=$now')"
    session_name="$(printf '%s' "$task_json" | jq -r '.session // empty')"

    json_update "$COMPLETED_FILE" "map(select(.id != \"$TASK_ID\"))"
    json_append "$COMPLETED_FILE" "$task_json"
    json_update "$ACTIVE_FILE" "map(select(.id != \"$TASK_ID\"))"

    if [[ -x "$SCRIPT_DIR/fleet-manager.sh" ]]; then
        "$SCRIPT_DIR/fleet-manager.sh" release "$PROJECT_NAME" "$TASK_ID" "$session_name" >/dev/null 2>&1 || true
    fi
    log_event "task-archive" "$status" "moved-to-completed-inline"
}

handle_kill_signal() {
    set_status killed
    log_event "agent" "killed" "signal-received"
    exit 143
}
trap handle_kill_signal INT TERM HUP

config_bool() {
    local key="$1"
    local fallback="${2:-false}"
    local value
    value="$(jq -r "$key // $fallback" "$CONFIG_FILE" 2>/dev/null || echo "$fallback")"
    value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
    case "$value" in
        1|true|yes|on) printf '%s' "true" ;;
        *) printf '%s' "false" ;;
    esac
}

max_review_cycles() {
    local cycles
    cycles="$(jq -r '.max_review_cycles // 2' "$CONFIG_FILE" 2>/dev/null || echo 2)"
    if ! [[ "$cycles" =~ ^[0-9]+$ ]]; then
        cycles=2
    fi
    if (( cycles < 1 )); then
        cycles=1
    fi
    printf '%s' "$cycles"
}

extract_json_block() {
    local text="$1"
    local block
    block="$(printf '%s\n' "$text" | sed -n '/```json/,/```/p' | sed '1d;$d')"
    if [[ -z "$block" ]]; then
        block="$(printf '%s\n' "$text" | sed -n '/^{/,$p')"
    fi
    printf '%s' "$block"
}

resolve_openrouter_key() {
    if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
        printf '%s' "$OPENROUTER_API_KEY"
        return 0
    fi
    local key
    key="$(jq -r '.providers.openrouter.apiKey // empty' "$HOME/.openclaw/agents/main/agent/models.json" 2>/dev/null || true)"
    if [[ -n "$key" ]]; then
        printf '%s' "$key"
        return 0
    fi
    return 1
}

OPENROUTER_KEY="$(resolve_openrouter_key || true)"
REVIEW_MODEL="${REVIEW_MODEL:-google/gemini-2.5-flash}"

openrouter_pr_review_json() {
    local pr_number="$1"
    local pr_title pr_diff prompt raw content json_block issue_lines

    [[ -n "$OPENROUTER_KEY" ]] || return 1

    pr_title="$(gh pr view "$pr_number" --json title --jq '.title' 2>/dev/null || echo "Unknown")"
    pr_diff="$(gh pr diff "$pr_number" 2>/dev/null || true)"
    [[ -n "$pr_diff" ]] || return 1

    prompt="You are a strict senior reviewer.
Review this PR diff and return ONLY JSON with this exact schema:
{
  \"verdict\": \"approve|changes_requested\",
  \"summary\": \"short summary\",
  \"actionable_issues\": [\"concrete fix 1\", \"concrete fix 2\"]
}
Rules:
- actionable_issues must contain only concrete, code-change-required items.
- If no issues, return actionable_issues as [] and verdict=approve.
- No markdown, no extra keys.

PR: ${pr_title}

DIFF:
$(printf '%s' "$pr_diff" | head -3000)"

    raw="$(curl -s --max-time 60 "https://openrouter.ai/api/v1/chat/completions" \
        -H "Authorization: Bearer ${OPENROUTER_KEY}" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg p "$prompt" --arg model "$REVIEW_MODEL" '{model:$model, messages:[{role:"user",content:$p}], temperature:0.2, max_tokens:1500}')" \
        || true)"

    content="$(printf '%s' "$raw" | jq -r '.choices[0].message.content // empty' 2>/dev/null || true)"
    [[ -n "$content" ]] || return 1
    json_block="$(extract_json_block "$content")"
    [[ -n "$json_block" ]] || json_block="$content"

    if printf '%s' "$json_block" | jq -e . >/dev/null 2>&1; then
        printf '%s' "$json_block" | jq -c '
            {
              verdict: ((.verdict // "changes_requested") | tostring | ascii_downcase),
              summary: ((.summary // "") | tostring),
              actionable_issues: ((.actionable_issues // []) | map(tostring) | map(gsub("^\\s+|\\s+$";"")) | map(select(length > 0)) | unique)
            }
        '
        return 0
    fi

    issue_lines="$(printf '%s\n' "$content" | sed -n 's/^[[:space:]]*[-*][[:space:]]\+//p' | head -8)"
    jq -nc \
        --arg summary "$(printf '%s' "$content" | head -c 300)" \
        --argjson issues "$(printf '%s\n' "$issue_lines" | jq -Rsc 'split("\n") | map(gsub("^\\s+|\\s+$";"")) | map(select(length > 0)) | unique')" \
        '{verdict:(if ($issues | length) > 0 then "changes_requested" else "approve" end), summary:$summary, actionable_issues:$issues}'
}

run_post_pr_review_cycles() {
    local pr_number="$1"
    local review_json verdict summary issues_count issues_text comment_body
    local review_enable

    review_enable="$(config_bool '.auto_review' 'true')"
    [[ "$review_enable" == "true" ]] || return 0
    [[ -n "$OPENROUTER_KEY" ]] || return 0

    echo "[REVIEW] Running PR review..."
    review_json="$(openrouter_pr_review_json "$pr_number" || true)"
    if [[ -z "$review_json" ]]; then
        log_event "review-cycle" "skipped" "no-review-json"
        return 0
    fi

    verdict="$(printf '%s' "$review_json" | jq -r '.verdict // "changes_requested"' 2>/dev/null || echo changes_requested)"
    summary="$(printf '%s' "$review_json" | jq -r '.summary // ""' 2>/dev/null || echo "")"
    issues_count="$(printf '%s' "$review_json" | jq -r '(.actionable_issues // []) | length' 2>/dev/null || echo 0)"
    issues_text="$(printf '%s' "$review_json" | jq -r '(.actionable_issues // []) | to_entries | map("- [ ] \(.key + 1). \(.value)") | join("\n")' 2>/dev/null || echo "")"

    comment_body="## 🤖 Seabone Review
Verdict: **${verdict}**

${summary:-No summary provided.}"
    if (( issues_count > 0 )); then
        comment_body="${comment_body}

### Actionable Issues
${issues_text}"
    fi
    gh pr comment "$pr_number" --body "$comment_body" >/dev/null 2>&1 || true

    if (( issues_count == 0 )) || [[ "$verdict" == "approve" ]]; then
        log_event "review-cycle" "approved" "verdict=${verdict}"
        echo "[REVIEW] No actionable issues."
        return 0
    fi

    log_event "review-cycle" "issues-found" "issues=${issues_count}"
    return 0
}

close_pr_and_delete_branch() {
    local pr_number="$1"
    local reason="$2"
    gh pr comment "$pr_number" --body "## 🚫 Seabone Auto-Close

This PR is being auto-closed.

Reason: ${reason}" >/dev/null 2>&1 || true
    gh pr close "$pr_number" --delete-branch >/dev/null 2>&1 || {
        gh pr close "$pr_number" >/dev/null 2>&1 || true
        git push origin --delete "$BRANCH" >/dev/null 2>&1 || true
    }
    log_event "rollback" "pr_closed" "pr=${pr_number} reason=${reason}"
}

shared_context_summary() {
    if [[ -x "$SCRIPT_DIR/shared-context.sh" ]]; then
        "$SCRIPT_DIR/shared-context.sh" summary 8 2>/dev/null || true
        return
    fi
    echo "(shared context unavailable)"
}

build_task_prompt_with_context() {
    local task="$1"
    local summary="$2"
    cat <<EOF
Shared Context (cross-agent findings):
${summary}

If you discover reusable patterns/constraints while working, add them:
\`$SCRIPT_DIR/shared-context.sh add --source "$TASK_ID" --kind pattern --scope "<module-or-area>" --note "<finding>" --confidence 0.80\`

Primary Task:
${task}
EOF
}

cd "$WORKTREE_DIR"
SHARED_CONTEXT_SUMMARY="$(shared_context_summary)"
TASK_WITH_CONTEXT="$(build_task_prompt_with_context "$DESCRIPTION" "$SHARED_CONTEXT_SUMMARY")"

echo "=== Seabone Agent: $TASK_ID ==="
echo "Engine: $ENGINE"
echo "Model: $MODEL"
echo "Task: $DESCRIPTION"
echo "Branch: $BRANCH"
echo "Started: $(date)"
echo "================================"

# =========================================
#  ENGINE: Claude Code
# =========================================
if [[ "$ENGINE" == "claude" ]]; then
    echo "[RUN] Claude Code (headless)..."

    CLAUDE_ARGS=(
        -p "$TASK_WITH_CONTEXT"
        --dangerously-skip-permissions
        --output-format stream-json
        --model "$MODEL"
        --verbose
    )

    if [[ -f "$PROJECT_DIR/CLAUDE.md" ]]; then
        CLAUDE_ARGS+=(--append-system-prompt "$(cat "$PROJECT_DIR/CLAUDE.md")")
    fi

    claude "${CLAUDE_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Claude Frontend Design Specialist
# =========================================
elif [[ "$ENGINE" == "claude-frontend" ]]; then
    echo "[RUN] Claude Frontend Design Specialist..."

    # Load the frontend design system prompt
    FRONTEND_PROMPT=""
    if [[ -f "$PROMPTS_DIR/frontend-design.md" ]]; then
        FRONTEND_PROMPT=$(cat "$PROMPTS_DIR/frontend-design.md")
    fi

    # Build the full prompt: system context + task
    FULL_TASK="$FRONTEND_PROMPT

---

## Your Task

$TASK_WITH_CONTEXT

## Project Context
- Stack: Python 3.12, FastAPI, Jinja2 templates, Tailwind CSS, Alpine.js, HTMX
- Templates: app/templates/ (Jinja2 .html files)
- Static: app/static/css/, app/static/js/, app/static/img/
- Base template: app/templates/base.html (extend this)
- Use CDN for Tailwind, Alpine.js, HTMX unless local files exist already

## Requirements
- Create working, production-grade frontend code
- Every file must be complete and functional — no placeholders
- Follow existing project patterns for template structure
- Responsive design (mobile-first)
- Dark mode support
- Accessible (ARIA labels, semantic HTML)
- Distinctive design — no generic Bootstrap/AI-slop aesthetics"

    CLAUDE_ARGS=(
        -p "$FULL_TASK"
        --dangerously-skip-permissions
        --output-format stream-json
        --model "$MODEL"
        --max-turns 50
        --verbose
    )

    if [[ -f "$PROJECT_DIR/CLAUDE.md" ]]; then
        CLAUDE_ARGS+=(--append-system-prompt "$(cat "$PROJECT_DIR/CLAUDE.md")")
    fi

    claude "${CLAUDE_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Codex
# =========================================
elif [[ "$ENGINE" == "codex" ]]; then
    echo "[RUN] Codex CLI (full-auto)..."

    codex exec \
        --full-auto \
        --model "$MODEL" \
        "$TASK_WITH_CONTEXT" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Codex Testing Specialist
# =========================================
elif [[ "$ENGINE" == "codex-test" ]]; then
    echo "[RUN] Codex Testing Specialist..."

    # Load the testing system prompt
    TEST_PROMPT=""
    if [[ -f "$PROMPTS_DIR/testing-agent.md" ]]; then
        TEST_PROMPT=$(cat "$PROMPTS_DIR/testing-agent.md")
    fi

    FULL_TASK="${TEST_PROMPT}

---

## Your Task

${TASK_WITH_CONTEXT}

## Project Context
- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, PostgreSQL
- Test runner: pytest
- Test dir: tests/ (mirrors app/ structure)
- Fixtures: tests/conftest.py
- Run tests with: python -m pytest tests/ -v
- Do NOT import structlog — use stdlib logging only

## Requirements
- Write complete, runnable test files
- Run the tests after writing to verify they pass
- Fix any test failures before finishing
- Use pytest fixtures, not setUp/tearDown
- Use httpx.AsyncClient for API tests
- Mock external services, never call real APIs in tests"

    codex exec \
        --full-auto \
        --model "$MODEL" \
        "$FULL_TASK" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Codex Senior Dev (Escalation)
# =========================================
elif [[ "$ENGINE" == "codex-senior" ]]; then
    echo "[RUN] Codex Senior Dev (Escalation)..."

    # Load the senior dev system prompt
    SENIOR_PROMPT=""
    if [[ -f "$PROMPTS_DIR/senior-dev.md" ]]; then
        SENIOR_PROMPT=$(cat "$PROMPTS_DIR/senior-dev.md")
    fi

    # Check for previous agent logs to provide context
    PREV_LOG_CONTEXT=""
    # Extract base task ID (strip -v2, -v3 suffixes for escalation lookups)
    BASE_TASK_ID=$(echo "$TASK_ID" | sed -E 's/-v[0-9]+$//')
    for prev_log in "$LOG_DIR/${BASE_TASK_ID}"*.log; do
        if [[ -f "$prev_log" && "$prev_log" != "$LOG_FILE" ]]; then
            # Get last 80 lines of previous attempts
            PREV_LOG_CONTEXT="${PREV_LOG_CONTEXT}

--- Previous attempt log: $(basename "$prev_log") ---
$(tail -80 "$prev_log" 2>/dev/null || echo "(empty)")"
        fi
    done

    FULL_TASK="${SENIOR_PROMPT}

---

## Your Task

${TASK_WITH_CONTEXT}

## Project Context
- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, PostgreSQL, Redis
- Do NOT import structlog — use stdlib logging only
- Health/status endpoints are intentionally unauthenticated
- Services use serialize() methods that return dicts
- Schemas use Pydantic BaseModel with org_id as UUID

## Previous Attempts
${PREV_LOG_CONTEXT:-No previous attempts — this is a first escalation.}

## Requirements
- Read the previous agent's log above to understand what went wrong
- Read at least 5-10 relevant source files before making changes
- Fix the root cause, not just the symptom
- Run tests after fixing to verify
- If the task is fundamentally impossible, document why and exit cleanly"

    codex exec \
        --full-auto \
        --model "$MODEL" \
        "$FULL_TASK" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

fi

# =========================================
#  Common: check result, commit, push, PR
# =========================================
if [[ ${AGENT_EXIT:-1} -ne 0 ]]; then
    set_status failed
    log_event "agent" "failed" "exit-${AGENT_EXIT}"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone*: \`$TASK_ID\` failed (${ENGINE}, exit ${AGENT_EXIT})." 2>/dev/null || true
    exit 1
fi

cd "$WORKTREE_DIR"
if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
    set_status no_changes
    log_event "completion" "no_changes" "No diff produced"
    "$SCRIPT_DIR/notify-telegram.sh" "⚠️ *Seabone*: \`$TASK_ID\` no changes (${ENGINE})." 2>/dev/null || true
    exit 0
fi

echo ""
echo "[COMMIT] Staging and committing..."
# Exclude .agent-run.sh from commits (it is a local-only bootstrap file)
git add -A
git reset HEAD .agent-run.sh 2>/dev/null || true
git commit -m "feat($TASK_ID): $DESCRIPTION

Automated by Seabone ($ENGINE + $MODEL)"

# ---- Run quality gates before pushing ----
echo ""
echo "[QUALITY] Running pre-push quality gates..."
QUALITY_PASSED=true
QUALITY_NOTE=""
QUALITY_GATE_RETRIES="$(jq -r '.quality_gate_retries // 1' "$CONFIG_FILE" 2>/dev/null || echo 1)"
if ! [[ "$QUALITY_GATE_RETRIES" =~ ^[0-9]+$ ]]; then
    QUALITY_GATE_RETRIES=1
fi
if (( QUALITY_GATE_RETRIES < 1 )); then
    QUALITY_GATE_RETRIES=1
fi

gate_enabled() {
    local gate_id="$1"
    jq -e --arg gate "$gate_id" '
        if (.quality_gates | type) != "array" or ((.quality_gates | length) == 0) then
            true
        else
            any(.quality_gates[]?;
                if type == "string" then
                    . == $gate
                elif type == "object" then
                    (((.id // .name // "") == $gate) and ((.enabled // true) == true))
                else
                    false
                end
            )
        end
    ' "$CONFIG_FILE" >/dev/null 2>&1
}

run_gate() {
    local gate_id="$1"
    shift
    local attempt output
    for ((attempt=1; attempt<=QUALITY_GATE_RETRIES; attempt++)); do
        echo "  [GATE:$gate_id] attempt ${attempt}/${QUALITY_GATE_RETRIES}: $*"
        if output="$("$@" 2>&1)"; then
            if [[ -n "$output" ]]; then
                echo "$output" | tail -20 | tee -a "$LOG_FILE"
            fi
            return 0
        fi
        echo "$output" | tail -40 | tee -a "$LOG_FILE"
    done
    return 1
}

mapfile -t CHANGED_FILES < <(git show --pretty="" --name-only HEAD 2>/dev/null | sed '/^$/d' || true)

declare -a BASH_FILES
declare -a PY_FILES
declare -a JS_TS_FILES
declare -a GO_FILES
declare -a GATES_RAN
declare -a GATES_FAILED

for file in "${CHANGED_FILES[@]}"; do
    [[ -n "$file" ]] || continue
    case "$file" in
        *.sh|*.bash|*.zsh) BASH_FILES+=("$file") ;;
    esac
    case "$file" in
        *.py) PY_FILES+=("$file") ;;
    esac
    case "$file" in
        *.js|*.jsx|*.mjs|*.cjs|*.ts|*.tsx) JS_TS_FILES+=("$file") ;;
    esac
    case "$file" in
        *.go) GO_FILES+=("$file") ;;
    esac
done

if (( ${#BASH_FILES[@]} > 0 )) && gate_enabled "shellcheck"; then
    GATES_RAN+=("shellcheck")
    if command -v shellcheck >/dev/null 2>&1; then
        if ! run_gate "shellcheck" shellcheck -x "${BASH_FILES[@]}"; then
            QUALITY_PASSED=false
            GATES_FAILED+=("shellcheck")
        fi
    else
        echo "  [GATE:shellcheck] missing tool: shellcheck" | tee -a "$LOG_FILE"
        QUALITY_PASSED=false
        GATES_FAILED+=("shellcheck")
    fi
fi

if (( ${#PY_FILES[@]} > 0 )) && gate_enabled "pytest"; then
    GATES_RAN+=("pytest")
    if command -v python >/dev/null 2>&1; then
        if [[ -d tests ]]; then
            if ! run_gate "pytest" python -m pytest tests/ -x -q --tb=short; then
                QUALITY_PASSED=false
                GATES_FAILED+=("pytest")
            fi
        else
            if ! run_gate "pytest" python -m pytest -x -q --tb=short; then
                QUALITY_PASSED=false
                GATES_FAILED+=("pytest")
            fi
        fi
    else
        echo "  [GATE:pytest] missing tool: python" | tee -a "$LOG_FILE"
        QUALITY_PASSED=false
        GATES_FAILED+=("pytest")
    fi
fi

if (( ${#JS_TS_FILES[@]} > 0 )) && gate_enabled "eslint"; then
    GATES_RAN+=("eslint")
    if command -v npm >/dev/null 2>&1 || command -v npx >/dev/null 2>&1; then
        if [[ -f package.json ]] && jq -e '.scripts.lint' package.json >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
            if ! run_gate "eslint" npm run -s lint -- --max-warnings=0; then
                QUALITY_PASSED=false
                GATES_FAILED+=("eslint")
            fi
        else
            JS_TS_EXISTING=()
            for file in "${JS_TS_FILES[@]}"; do
                [[ -f "$file" ]] && JS_TS_EXISTING+=("$file")
            done
            if (( ${#JS_TS_EXISTING[@]} > 0 )); then
                if command -v npx >/dev/null 2>&1; then
                    if ! run_gate "eslint" npx --yes eslint "${JS_TS_EXISTING[@]}"; then
                        QUALITY_PASSED=false
                        GATES_FAILED+=("eslint")
                    fi
                else
                    echo "  [GATE:eslint] missing tool: npx" | tee -a "$LOG_FILE"
                    QUALITY_PASSED=false
                    GATES_FAILED+=("eslint")
                fi
            else
                echo "  [GATE:eslint] no existing JS/TS files to lint after commit." | tee -a "$LOG_FILE"
            fi
        fi
    else
        echo "  [GATE:eslint] missing tool: npm/npx" | tee -a "$LOG_FILE"
        QUALITY_PASSED=false
        GATES_FAILED+=("eslint")
    fi
fi

if (( ${#GO_FILES[@]} > 0 )) && gate_enabled "go-vet"; then
    GATES_RAN+=("go-vet")
    if command -v go >/dev/null 2>&1; then
        if ! run_gate "go-vet" go vet ./...; then
            QUALITY_PASSED=false
            GATES_FAILED+=("go-vet")
        fi
    else
        echo "  [GATE:go-vet] missing tool: go" | tee -a "$LOG_FILE"
        QUALITY_PASSED=false
        GATES_FAILED+=("go-vet")
    fi
fi

if [[ "$QUALITY_PASSED" == "false" ]]; then
    FAILED_GATES_CSV="$(printf '%s\n' "${GATES_FAILED[@]}" | sed '/^$/d' | paste -sd ', ' -)"
    set_status quality_failed
    log_event "quality-gates" "quality_failed" "failed=${FAILED_GATES_CSV:-unknown}"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone*: \`$TASK_ID\` quality gates failed (${FAILED_GATES_CSV:-unknown})." 2>/dev/null || true
    exit 1
fi

if (( ${#GATES_RAN[@]} > 0 )); then
    GATES_RAN_CSV="$(printf '%s\n' "${GATES_RAN[@]}" | sed '/^$/d' | paste -sd ', ' -)"
    QUALITY_RESULT_TEXT="✅ Passed (${GATES_RAN_CSV})"
else
    QUALITY_RESULT_TEXT="⚪ No language gates triggered"
fi

echo "[QUALITY] ${QUALITY_RESULT_TEXT}"

echo "[PUSH] Pushing to origin..."
git push -u origin "$BRANCH"

echo "[PR] Creating pull request..."
PR_URL=$(gh pr create \
    --title "[$TASK_ID] $DESCRIPTION" \
    --body "## Summary
Automated PR by Seabone agent swarm.

**Task:** $DESCRIPTION
**Engine:** \`$ENGINE\`
**Model:** \`$MODEL\`
**Branch:** \`$BRANCH\`
**Quality Gates:** ${QUALITY_RESULT_TEXT}${QUALITY_NOTE}

---
🤖 Seabone Agent Swarm" \
    --head "$BRANCH" 2>&1) || PR_URL="PR creation failed"

if [[ "$PR_URL" == "PR creation failed" ]]; then
    set_status failed
    log_event "completion" "failed" "PR creation failed"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone*: \`$TASK_ID\` PR creation failed." 2>/dev/null || true
    exit 1
fi

PR_NUMBER="$(gh pr view --head "$BRANCH" --json number --jq '.number' 2>/dev/null || true)"
if [[ -n "$PR_NUMBER" ]]; then
    echo "[REVIEW] Running PR review..."
    run_post_pr_review_cycles "$PR_NUMBER" || true
fi

echo "[OK] PR created: $PR_URL"

# ---- Multi-model review (opt-in) ----
MULTI_REVIEW_ENABLED="$(jq -r '.review_models.multi_review_enabled // false' "$CONFIG_FILE" 2>/dev/null || echo false)"
if [[ "$MULTI_REVIEW_ENABLED" == "true" && -n "$PR_NUMBER" ]]; then
    echo "[REVIEW] Running multi-model review pipeline..."
    set_status pr_reviewing
    log_event "multi-review" "started" "pr=$PR_NUMBER"
    MULTI_REVIEW_RC=0
    "$SCRIPT_DIR/multi-review.sh" "$PR_NUMBER" --config "$CONFIG_FILE" || MULTI_REVIEW_RC=$?
    if (( MULTI_REVIEW_RC == 0 )); then
        set_status review_passed
        log_event "multi-review" "passed" "pr=$PR_NUMBER"
    else
        set_status review_failed
        log_event "multi-review" "failed" "pr=$PR_NUMBER"
    fi
fi

# ---- Definition of Done gating (opt-in) ----
DOD_ENABLED="$(jq -r '.dod_enabled // false' "$CONFIG_FILE" 2>/dev/null || echo false)"
if [[ "$DOD_ENABLED" == "true" && -n "$PR_NUMBER" ]]; then
    echo "[DOD] Running Definition of Done checks..."
    DOD_FAILURES=""

    # Check 1: Branch synced / mergeable
    DOD_CHECK_SYNCED="$(jq -r '.dod_checks.branch_synced // true' "$CONFIG_FILE" 2>/dev/null || echo true)"
    if [[ "$DOD_CHECK_SYNCED" == "true" ]]; then
        MERGEABLE="$(gh pr view "$PR_NUMBER" --json mergeable --jq '.mergeable' 2>/dev/null || echo "UNKNOWN")"
        if [[ "$MERGEABLE" != "MERGEABLE" ]]; then
            DOD_FAILURES="${DOD_FAILURES}branch_synced($MERGEABLE) "
            echo "[DOD] Branch synced: FAILED ($MERGEABLE)"
        else
            echo "[DOD] Branch synced: OK"
        fi
    fi

    # Check 2: CI passing
    DOD_CHECK_CI="$(jq -r '.dod_checks.ci_passing // true' "$CONFIG_FILE" 2>/dev/null || echo true)"
    DOD_CI_TIMEOUT="$(jq -r '.dod_ci_timeout_seconds // 300' "$CONFIG_FILE" 2>/dev/null || echo 300)"
    if [[ "$DOD_CHECK_CI" == "true" ]]; then
        CI_PASSED=false
        CI_ELAPSED=0
        CI_INTERVAL=15
        while (( CI_ELAPSED < DOD_CI_TIMEOUT )); do
            CI_STATUS="$(gh pr checks "$PR_NUMBER" 2>/dev/null || true)"
            if printf '%s' "$CI_STATUS" | grep -q "fail\|FAILURE"; then
                echo "[DOD] CI: FAILED"
                break
            elif printf '%s' "$CI_STATUS" | grep -q "pending\|PENDING\|queued"; then
                sleep "$CI_INTERVAL"
                CI_ELAPSED=$((CI_ELAPSED + CI_INTERVAL))
                echo "[DOD] CI: pending... (${CI_ELAPSED}/${DOD_CI_TIMEOUT}s)"
            else
                CI_PASSED=true
                echo "[DOD] CI: OK"
                break
            fi
        done
        if [[ "$CI_PASSED" != "true" ]]; then
            DOD_FAILURES="${DOD_FAILURES}ci_passing "
        fi
    fi

    # Check 3: AI reviews passed
    DOD_CHECK_REVIEWS="$(jq -r '.dod_checks.ai_reviews_passed // true' "$CONFIG_FILE" 2>/dev/null || echo true)"
    if [[ "$DOD_CHECK_REVIEWS" == "true" && "$MULTI_REVIEW_ENABLED" == "true" ]]; then
        if (( ${MULTI_REVIEW_RC:-0} != 0 )); then
            DOD_FAILURES="${DOD_FAILURES}ai_reviews "
            echo "[DOD] AI reviews: FAILED"
        else
            echo "[DOD] AI reviews: OK"
        fi
    fi

    # Check 4: Screenshots (optional)
    DOD_CHECK_SCREENSHOTS="$(jq -r '.dod_checks.screenshots_required // false' "$CONFIG_FILE" 2>/dev/null || echo false)"
    if [[ "$DOD_CHECK_SCREENSHOTS" == "true" ]]; then
        PR_BODY="$(gh pr view "$PR_NUMBER" --json body --jq '.body' 2>/dev/null || true)"
        if printf '%s' "$PR_BODY" | grep -qE '!\[|\.png|\.jpg|\.gif|\.webp|imgur\.com|screenshot'; then
            echo "[DOD] Screenshots: OK"
        else
            DOD_FAILURES="${DOD_FAILURES}screenshots "
            echo "[DOD] Screenshots: MISSING"
        fi
    fi

    # DoD verdict
    if [[ -z "$DOD_FAILURES" ]]; then
        set_status dod_passed
        log_event "dod" "passed" "pr=$PR_NUMBER"
        "$SCRIPT_DIR/notify-telegram.sh" "✅ *Seabone*: \`$TASK_ID\` done ($ENGINE) — DoD: All checks passed
PR: $PR_URL" 2>/dev/null || true
    else
        set_status dod_failed
        log_event "dod" "failed" "pr=$PR_NUMBER failures=${DOD_FAILURES}"
        "$SCRIPT_DIR/notify-telegram.sh" "⚠️ *Seabone*: \`$TASK_ID\` PR created but DoD failed — Failed: ${DOD_FAILURES}
PR: $PR_URL" 2>/dev/null || true
    fi
else
    # No DoD — notify immediately as before
    if [[ "$MULTI_REVIEW_ENABLED" == "true" && "${MULTI_REVIEW_RC:-0}" -ne 0 ]]; then
        set_status pr_created
        log_event "completion" "pr_created" "$PR_URL review=changes_requested"
        "$SCRIPT_DIR/notify-telegram.sh" "⚠️ *Seabone*: \`$TASK_ID\` done ($ENGINE) — review requested changes
PR: $PR_URL" 2>/dev/null || true
    else
        set_status pr_created
        log_event "completion" "pr_created" "$PR_URL"
        "$SCRIPT_DIR/notify-telegram.sh" "✅ *Seabone*: \`$TASK_ID\` done ($ENGINE)
PR: $PR_URL" 2>/dev/null || true
    fi
fi
exit 0
