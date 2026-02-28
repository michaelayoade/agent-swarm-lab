# Seabone Agent Swarm Lab

An autonomous coding agent swarm powered by **Aider + DeepSeek**, orchestrated by **Seabone**.

## Quick Start

```bash
# 1. Configure your API keys
cp .env.agent-swarm.example .env.agent-swarm
vim .env.agent-swarm  # Add DEEPSEEK_API_KEY, TELEGRAM tokens

# 2. Authenticate GitHub CLI
gh auth login

# 3. Spawn an agent
./scripts/spawn-agent.sh "fix-bug-123" "Fix the login validation bug in auth.py"

# 4. Check status
./scripts/list-tasks.sh

# 5. Watch agent live
tmux attach -t agent-agent-swarm-lab-fix-bug-123
```

## How It Works

Each task spawns an isolated agent:
- **Git worktree** on branch `agent/<task-id>` — isolated file changes
- **Tmux session** — persistent background execution
- **Aider + DeepSeek** — AI-powered code generation
- **Auto PR** — changes are committed, pushed, and a PR is created
- **Telegram alerts** — notifications on completion/failure
- **Auto review** — DeepSeek reviews the PR diff

## Scripts

| Script | Purpose |
|--------|---------|
| `spawn-agent.sh` | Launch a new coding agent |
| `check-agents.sh` | Monitor health, auto-respawn (cron) |
| `cleanup-worktrees.sh` | Archive old tasks, prune (cron) |
| `notify-telegram.sh` | Send Telegram notifications |
| `review-pr.sh` | AI code review on a PR |
| `list-tasks.sh` | View task status |
| `telegram-bot.py` | Bidirectional Telegram bot for remote control |

## Telegram Bot (Remote Control)

Control the swarm from your phone via a persistent AI agent powered by DeepSeek.

### Architecture (v2)

The bot is a fully persistent autonomous agent with:
- **JSONL sessions** — conversation history survives restarts, with automatic compaction
- **Workspace identity** — personality (SOUL.md), operator profile (USER.md), tool guidance (TOOLS.md)
- **Long-term memory** — the agent writes to MEMORY.md and daily logs across sessions
- **Inline keyboards** — action buttons on spawn results, help, and completions
- **Cron scheduler** — built-in scheduled jobs (morning status, stale checks, maintenance)
- **GitHub webhooks** — auto-respond to issues with `agent` label, new PRs, CI failures
- **13 tools** — 8 original + write_memory, read_memory, list_prs, view_pr, merge_pr

### Setup

1. Ensure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env.agent-swarm`
2. Run directly: `python3 scripts/telegram-bot.py`
3. Or install as a systemd service:

```bash
sudo cp seabone-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seabone-telegram-bot
```

### Commands

| Command | Description |
|---------|-------------|
| `/spawn <desc>` | Spawn a new coding agent |
| `/status` | Show swarm status (active, queued, sessions) |
| `/logs <id> [N]` | Tail last N lines of agent log (default 50) |
| `/kill <id>` | Kill an agent's tmux session |
| `/queue` | Show the current task queue |
| `/prs [state]` | List GitHub pull requests |
| `/memory [query]` | Search long-term memory |
| `/clear` | Clear conversation (reset session) |
| `/help` | List available commands |

Or just talk naturally — the agent decides which tools to use.

### Workspace Files

| File | Purpose |
|------|---------|
| `.seabone/workspace/SOUL.md` | Agent personality and behaviour rules |
| `.seabone/workspace/USER.md` | Operator profile (edit to add your preferences) |
| `.seabone/workspace/TOOLS.md` | Tool usage guidance for the agent |
| `.seabone/workspace/MEMORY.md` | Long-term memory (written by the agent) |
| `.seabone/workspace/memory/` | Daily logs (YYYY-MM-DD.md) |
| `.seabone/sessions/` | JSONL conversation transcripts |

### Webhooks

To receive GitHub events, enable webhooks in `.seabone/config.json`:

```json
{
  "webhook": {
    "enabled": true,
    "port": 18790,
    "secret": "your-webhook-secret"
  }
}
```

Then configure your GitHub repo webhook to point to `http://your-server:18790/webhook/github` with content type `application/json` and the same secret.

Events handled: issues with `agent` label, new PRs, CI failures.

### Cron Jobs (Built-in)

The bot has a built-in cron scheduler. Default jobs in config:

| Job | Schedule | Action |
|-----|----------|--------|
| `morning-status` | `0 8 * * *` | Morning swarm status report |
| `stale-check` | `*/30 * * * *` | Check for stale agents |
| `maintenance` | `0 3 * * *` | Prune old sessions and logs |

Security: the bot only responds to the chat ID configured in `TELEGRAM_CHAT_ID`.

## Cron Jobs

```
*/10 * * * * ~/projects/agent-swarm-lab/scripts/check-agents.sh >> ~/.seabone-cron.log 2>&1
0 3 * * *   ~/projects/agent-swarm-lab/scripts/cleanup-worktrees.sh >> ~/.seabone-cron.log 2>&1
```

## Configuration

Edit `.seabone/config.json`:

```json
{
  "max_concurrent_agents": 3,
  "max_retries": 3,
  "agent_timeout_minutes": 30,
  "model": "deepseek-chat",
  "auto_cleanup_days": 7
}
```
