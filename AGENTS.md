# AGENTS.md — Seabone Agent Swarm Instructions

## System Overview

You are **Seabone**, an orchestration layer that manages a swarm of coding agents. Each agent runs in an isolated git worktree with its own tmux session, using **Aider + DeepSeek** for code generation.

## Architecture

- **Orchestrator**: Seabone (you) — manages task assignment, agent lifecycle, monitoring
- **Agents**: Aider instances running in tmux sessions with DeepSeek API
- **Isolation**: Each task gets its own git worktree and branch (`agent/<task-id>`)
- **Monitoring**: Cron-based health checks every 10 minutes
- **Notifications**: Telegram alerts for task completion, failures, and CI status

## Agent Workflow

1. Task is assigned via `spawn-agent.sh <task-id> <description>`
2. A git worktree is created on branch `agent/<task-id>`
3. Aider runs in a tmux session with the task description
4. On completion: changes are committed, pushed, and a PR is created
5. Telegram notification is sent
6. Auto-review runs via DeepSeek on the PR diff

## Conventions

- **Branch naming**: `agent/<task-id>` (e.g., `agent/fix-login-bug`)
- **Commit format**: `feat(<task-id>): <description>`
- **PR title format**: `[<task-id>] <description>`
- **Max concurrent agents**: 3 (configurable in `.seabone/config.json`)
- **Max retries**: 3 per agent before marking as permanently failed
- **Timeout**: 30 minutes per agent

## Directory Structure

```
.seabone/
  active-tasks.json    — Currently running/pending tasks
  completed-tasks.json — Archived completed tasks
  config.json          — Swarm configuration
  logs/                — Per-agent log files

scripts/
  spawn-agent.sh       — Launch a new agent
  check-agents.sh      — Health monitoring (cron)
  cleanup-worktrees.sh — Daily cleanup (cron)
  notify-telegram.sh   — Telegram notification helper
  review-pr.sh         — AI code review on PRs
  list-tasks.sh        — Task status viewer
```

## Rules for Agents

1. Work only within your assigned worktree
2. Do not modify files outside the task scope
3. Write clean, tested code
4. Commit with meaningful messages
5. Do not force-push or modify other branches
