#\!/usr/bin/env bash
# notify-telegram.sh — Send a message to Telegram
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load env
if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    source "$PROJECT_DIR/.env.agent-swarm"
fi

MESSAGE="${1:-No message provided}"
PARSE_MODE="${2:-Markdown}"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "[WARN] Telegram not configured. Message: $MESSAGE"
    exit 0
fi

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TELEGRAM_CHAT_ID" \
    -d text="$MESSAGE" \
    -d parse_mode="$PARSE_MODE" \
    -d disable_web_page_preview=true > /dev/null 2>&1

echo "[OK] Telegram notification sent"
