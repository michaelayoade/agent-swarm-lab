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
tmux attach -t agent-fix-bug-123
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
