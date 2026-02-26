#!/usr/bin/env bash
# review-pr.sh — Fetch PR diff, run DeepSeek review via aider, post as PR comment
# Usage: ./review-pr.sh <pr-number>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
export PATH="$HOME/.local/bin:$PATH"

# Load env
if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi

PR_NUMBER="${1:?Usage: review-pr.sh <pr-number>}"

echo "[1/4] Fetching PR #$PR_NUMBER diff..."
PR_DIFF=$(gh pr diff "$PR_NUMBER" 2>/dev/null) || {
    echo "[ERROR] Could not fetch PR diff. Is gh authenticated?"
    exit 1
}

PR_TITLE=$(gh pr view "$PR_NUMBER" --json title --jq '.title' 2>/dev/null || echo "Unknown")
PR_BRANCH=$(gh pr view "$PR_NUMBER" --json headRefName --jq '.headRefName' 2>/dev/null || echo "Unknown")

if [[ -z "$PR_DIFF" ]]; then
    echo "[WARN] PR #$PR_NUMBER has no diff."
    exit 0
fi

echo "[2/4] Sending diff to DeepSeek for review..."

REVIEW_PROMPT="You are a senior code reviewer. Review this pull request diff carefully.

PR Title: $PR_TITLE
PR Branch: $PR_BRANCH

Provide a concise review covering:
1. **Correctness**: Any bugs or logic errors?
2. **Security**: Any vulnerabilities (injection, XSS, secrets)?
3. **Performance**: Any obvious inefficiencies?
4. **Style**: Follows project conventions?
5. **Summary**: Overall assessment (approve/request changes/comment)

Be specific. Reference file names and line numbers. Keep it under 500 words.

--- DIFF START ---
$PR_DIFF
--- DIFF END ---"

REVIEW=$(curl -s "https://api.deepseek.com/chat/completions" \
    -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg prompt "$REVIEW_PROMPT" '{
        model: "deepseek-chat",
        messages: [{role: "user", content: $prompt}],
        temperature: 0.3,
        max_tokens: 1500
    }')" | jq -r '.choices[0].message.content // "Review generation failed"')

echo "[3/4] Review generated. Posting to PR..."

COMMENT_BODY="## 🤖 Seabone Automated Review

$REVIEW

---
*Automated review by Seabone Agent Swarm (DeepSeek)*"

gh pr comment "$PR_NUMBER" --body "$COMMENT_BODY" 2>/dev/null || {
    echo "[ERROR] Failed to post comment. Dumping review:"
    echo "$COMMENT_BODY"
    exit 1
}

echo "[4/4] Review posted to PR #$PR_NUMBER"

"$SCRIPT_DIR/notify-telegram.sh" "🔍 *Seabone Review* posted on PR #$PR_NUMBER: $PR_TITLE" 2>/dev/null || true
