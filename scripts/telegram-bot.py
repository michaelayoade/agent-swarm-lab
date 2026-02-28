#!/usr/bin/env python3
"""Seabone Telegram Bot v2 — Persistent Agent Architecture.

An autonomous agent (powered by DeepSeek function calling) that manages
the Seabone coding agent swarm via Telegram. Features persistent JSONL
sessions, workspace identity files, inline keyboards, cron scheduler,
GitHub webhook listener, and pre-compaction memory flush.

Uses only Python stdlib (no pip dependencies).
"""

import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import select as _select_mod
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# 2. Path constants + new dirs
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SEABONE_DIR = PROJECT_DIR / ".seabone"
LOG_DIR = SEABONE_DIR / "logs"
ACTIVE_FILE = SEABONE_DIR / "active-tasks.json"
COMPLETED_FILE = SEABONE_DIR / "completed-tasks.json"
QUEUE_FILE = SEABONE_DIR / "queue.json"
CONFIG_FILE = SEABONE_DIR / "config.json"
HISTORY_FILE = SEABONE_DIR / "chat-history.json"

# v2 paths
WORKSPACE_DIR = SEABONE_DIR / "workspace"
SESSIONS_DIR = SEABONE_DIR / "sessions"
MEMORY_DIR = WORKSPACE_DIR / "memory"

SOUL_FILE = WORKSPACE_DIR / "SOUL.md"
USER_FILE = WORKSPACE_DIR / "USER.md"
TOOLS_FILE = WORKSPACE_DIR / "TOOLS.md"
MEMORY_FILE = WORKSPACE_DIR / "MEMORY.md"

VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# 3. Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("seabone-bot")

# ---------------------------------------------------------------------------
# 4. Env loading (unchanged)
# ---------------------------------------------------------------------------

def load_env(path: Path) -> None:
    if not path.is_file():
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_env(PROJECT_DIR / ".env.agent-swarm")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

if not BOT_TOKEN or not CHAT_ID:
    log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    sys.exit(1)
if not DEEPSEEK_API_KEY:
    log.error("DEEPSEEK_API_KEY must be set")
    sys.exit(1)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DS_API = "https://api.deepseek.com/chat/completions"

# ---------------------------------------------------------------------------
# 5. Config loading (deep-merge with new defaults)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "max_concurrent_agents": 3,
    "max_retries": 3,
    "agent_timeout_minutes": 30,
    "model": "deepseek-chat",
    "reason_model": "deepseek-reasoner",
    "worktree_base": "agent",
    "auto_review": True,
    "auto_cleanup_days": 7,
    "quality_gates": [],
    "quality_gate_retries": 1,
    "max_review_cycles": 2,
    "queue_enabled": True,
    "max_queue_size": 50,
    "heartbeat_timeout_minutes": 15,
    "stale_state_minutes": 20,
    "adaptive_reason_keywords": [
        "security", "auth", "authentication", "authorization",
        "migration", "payment", "architecture", "refactor",
    ],
    "model_learning_enabled": True,
    "log_retention_days": 14,
    "queue_dispatch_batch": 1,
    # v2 keys
    "session": {
        "reset_mode": "manual",
        "reset_hour": 4,
        "idle_minutes": 120,
        "maintenance": {
            "prune_after_days": 30,
            "max_entries": 10000,
            "max_disk_mb": 100,
        },
    },
    "context": {
        "max_messages": 100,
        "keep_recent": 30,
        "prune_tool_results_after": 15,
    },
    "cron_jobs": [
        {"id": "morning-status", "schedule": "0 8 * * *",
         "action": "Morning status report.", "enabled": True},
        {"id": "stale-check", "schedule": "*/30 * * * *",
         "action": "Check for stale agents.", "enabled": True},
        {"id": "maintenance", "schedule": "0 3 * * *",
         "action": "__maintenance__", "enabled": True},
    ],
    "webhook": {"enabled": False, "port": 18790, "secret": ""},
    "mcp_servers": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (base is not mutated)."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config() -> dict:
    """Load config.json and deep-merge with defaults. No write-back."""
    user_cfg = {}
    if CONFIG_FILE.is_file():
        try:
            with open(CONFIG_FILE) as fh:
                user_cfg = json.load(fh)
        except Exception as exc:
            log.warning("Failed to parse config.json: %s", exc)
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


CFG = load_config()

# ---------------------------------------------------------------------------
# 6. Session class (JSONL transcript, fcntl write lock)
# ---------------------------------------------------------------------------

class Session:
    """Append-only JSONL transcript with file locking."""

    def __init__(self, session_key: str):
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_key)
        self.path = SESSIONS_DIR / f"{safe}.jsonl"
        self.lock_path = SESSIONS_DIR / f"{safe}.lock"
        self._lock_fd = None

    def acquire_lock(self) -> bool:
        """Acquire non-blocking flock. Returns True if acquired."""
        try:
            self._lock_fd = open(self.lock_path, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            return False

    def release_lock(self) -> None:
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None

    def append(self, entry: dict) -> None:
        """Append a single JSONL line with timestamp."""
        entry = {**entry, "_ts": datetime.now(timezone.utc).isoformat()}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def load_context(self, config: dict) -> list:
        """Load messages from JSONL, apply pruning rules."""
        if not self.path.is_file():
            return []

        ctx_cfg = config.get("context", {})
        max_msgs = ctx_cfg.get("max_messages", 100)
        prune_after = ctx_cfg.get("prune_tool_results_after", 15)

        entries = []
        start_idx = 0

        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(entry)
                if entry.get("_compact_marker"):
                    # Marker position is based on parsed entries, not raw line index.
                    start_idx = len(entries)

        # Skip everything before last compaction marker
        if start_idx > 0:
            entries = entries[start_idx:]

        # Convert to messages, stripping internal keys
        messages = []
        for entry in entries:
            msg = {k: v for k, v in entry.items() if not k.startswith("_")}
            if "role" in msg:
                messages.append(msg)

        # Trim to max
        if len(messages) > max_msgs:
            messages = messages[-max_msgs:]

        # Prune old tool results
        if prune_after > 0 and len(messages) > prune_after:
            cutoff = len(messages) - prune_after
            for i in range(cutoff):
                if messages[i].get("role") == "tool":
                    content = messages[i].get("content", "")
                    if len(content) > 100:
                        messages[i]["content"] = "[truncated]"

        return messages

    def clear(self) -> None:
        """Write compaction marker — load_context skips everything before it."""
        self.append({"_compact_marker": True})

    def message_count(self) -> int:
        """Count non-marker messages since last compaction."""
        if not self.path.is_file():
            return 0
        count = 0
        last_compact = False
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("_compact_marker"):
                    count = 0
                    continue
                if "role" in entry:
                    count += 1
        return count

    def last_message_time(self) -> float:
        """Return timestamp of last message as epoch, or 0."""
        if not self.path.is_file():
            return 0.0
        last_ts = 0.0
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("_ts", "")
                    if ts_str and "role" in entry:
                        dt = datetime.fromisoformat(ts_str)
                        last_ts = dt.timestamp()
                except Exception:
                    continue
        return last_ts


# ---------------------------------------------------------------------------
# Migration: chat-history.json → JSONL
# ---------------------------------------------------------------------------

def _migrate_history():
    """One-time migration from flat JSON history to JSONL session."""
    if not HISTORY_FILE.is_file():
        return
    migrated = HISTORY_FILE.with_suffix(".json.migrated")
    if migrated.exists():
        return
    try:
        with open(HISTORY_FILE) as fh:
            data = json.load(fh)
        if not isinstance(data, list) or not data:
            HISTORY_FILE.rename(migrated)
            return
        session = Session(f"telegram_dm_{CHAT_ID}")
        for msg in data:
            if isinstance(msg, dict) and "role" in msg:
                session.append(msg)
        HISTORY_FILE.rename(migrated)
        log.info("Migrated %d messages from chat-history.json to JSONL", len(data))
    except Exception as exc:
        log.warning("History migration failed: %s", exc)


# ---------------------------------------------------------------------------
# 7. Workspace loader (SOUL/USER/TOOLS/MEMORY.md)
# ---------------------------------------------------------------------------
MAX_WORKSPACE_FILE = 8192   # 8KB per file
MAX_DAILY_LOG = 4096        # 4KB for daily log

DEFAULT_SOUL = """\
# Seabone — Agent Identity

You are **Seabone**, an autonomous AI agent managing a coding agent swarm.

- Be concise but warm. Use tools proactively.
- Write to memory when you learn something worth remembering.
- Break complex tasks into multiple agents.
"""

DEFAULT_USER = """\
# Operator Profile

(Edit .seabone/workspace/USER.md to add your preferences.)
"""

DEFAULT_TOOLS = """\
# Tool Usage Guidance

Use the appropriate tool for each situation. Never fabricate data.
"""


def ensure_workspace():
    """Create workspace dirs and default files if missing."""
    for d in (WORKSPACE_DIR, MEMORY_DIR, SESSIONS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    defaults = {
        SOUL_FILE: DEFAULT_SOUL,
        USER_FILE: DEFAULT_USER,
        TOOLS_FILE: DEFAULT_TOOLS,
        MEMORY_FILE: "# Seabone Long-Term Memory\n",
    }
    for path, content in defaults.items():
        if not path.exists():
            path.write_text(content)


def _read_workspace_file(path: Path, max_bytes: int = MAX_WORKSPACE_FILE) -> str:
    """Read a workspace file, capped at max_bytes."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text()
        return text[:max_bytes]
    except Exception:
        return ""


def _read_daily_log() -> str:
    """Read today's daily log from memory/YYYY-MM-DD.md."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = MEMORY_DIR / f"{today}.md"
    return _read_workspace_file(log_file, MAX_DAILY_LOG)


# ---------------------------------------------------------------------------
# 8. Context builder (layered system prompt, tool result pruning)
# ---------------------------------------------------------------------------

def _live_stats() -> dict:
    """Read live swarm stats from JSON files."""
    stats = {"active": 0, "queue": 0, "completed": 0}
    for key, path in [("active", ACTIVE_FILE), ("queue", QUEUE_FILE),
                       ("completed", COMPLETED_FILE)]:
        try:
            with open(path) as f:
                stats[key] = len(json.load(f))
        except Exception:
            pass
    return stats


def _build_system_prompt() -> str:
    """Build layered system prompt from workspace files + live stats."""
    stats = _live_stats()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = []

    # Agent identity
    soul = _read_workspace_file(SOUL_FILE)
    if soul:
        sections.append(soul.strip())

    # Operator profile
    user = _read_workspace_file(USER_FILE)
    if user:
        sections.append(user.strip())

    # Tool guidance
    tools = _read_workspace_file(TOOLS_FILE)
    if tools:
        sections.append(tools.strip())

    # Long-term memory
    memory = _read_workspace_file(MEMORY_FILE)
    if memory and memory.strip() != "# Seabone Long-Term Memory":
        sections.append(memory.strip())

    # Today's daily log
    daily = _read_daily_log()
    if daily:
        sections.append(f"# Today's Log\n{daily.strip()}")

    # Live status
    status_lines = [
        f"# Live Status",
        f"- Project: {PROJECT_DIR}",
        f"- Active tasks: {stats['active']}",
        f"- Queued tasks: {stats['queue']}",
        f"- Completed tasks: {stats['completed']}",
        f"- Timestamp: {now}",
    ]

    # MCP server status
    mcp_health = mcp_manager.health_check()
    if mcp_health:
        status_lines.append("")
        status_lines.append("## External Tools (MCP)")
        for srv_name, srv_info in mcp_health.items():
            state = "alive" if srv_info["alive"] else "DEAD"
            desc = f" — {srv_info['description']}" if srv_info["description"] else ""
            status_lines.append(
                f"- {srv_name}: {state}, {srv_info['tools']} tools{desc}"
            )

    sections.append("\n".join(status_lines))

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# 9. Telegram helpers (send_message with inline keyboard, callback handling)
# ---------------------------------------------------------------------------
MAX_MSG_LEN = 4096


def tg_request(method: str, payload: dict | None = None,
               timeout: int = 35) -> dict:
    url = f"{TG_API}/{method}"
    if payload:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
    else:
        req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def send_message(text: str, *, parse_mode: str | None = None,
                 reply_markup: dict | None = None,
                 chat_id: str | None = None) -> dict | None:
    """Send a Telegram message, splitting if needed. Returns last response."""
    target = chat_id or CHAT_ID
    last_resp = None
    chunks = _split_message(text)
    for i, chunk in enumerate(chunks):
        payload: dict = {
            "chat_id": target,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # Only attach keyboard to last chunk
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            last_resp = tg_request("sendMessage", payload)
        except Exception as exc:
            log.error("sendMessage failed: %s", exc)
            if parse_mode:
                payload.pop("parse_mode", None)
                try:
                    last_resp = tg_request("sendMessage", payload)
                except Exception:
                    pass
    return last_resp


def _split_message(text: str) -> list[str]:
    if len(text) <= MAX_MSG_LEN:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = MAX_MSG_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback query to dismiss loading state."""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
    try:
        tg_request("answerCallbackQuery", payload)
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", exc)


# Keyboard builders

def spawn_keyboard(task_id: str) -> dict:
    """Inline keyboard for a newly spawned agent."""
    return {"inline_keyboard": [[
        {"text": "View Logs", "callback_data": f"logs:{task_id}"},
        {"text": "Kill", "callback_data": f"kill:{task_id}"},
        {"text": "Status", "callback_data": "status:"},
    ]]}


def completion_keyboard(task_id: str, pr_url: str = "") -> dict:
    """Inline keyboard for a completed agent."""
    buttons = []
    if pr_url:
        buttons.append({"text": "View PR", "url": pr_url})
    buttons.append({"text": "View Logs", "callback_data": f"logs:{task_id}"})
    buttons.append({"text": "Retry", "callback_data": f"retry:{task_id}"})
    return {"inline_keyboard": [buttons]}


def help_keyboard() -> dict:
    """Inline keyboard for /help."""
    return {"inline_keyboard": [[
        {"text": "Status", "callback_data": "status:"},
        {"text": "Queue", "callback_data": "queue:"},
        {"text": "Active", "callback_data": "active:"},
        {"text": "Completed", "callback_data": "completed:"},
    ]]}


# ---------------------------------------------------------------------------
# 10. Tool implementations (8 existing + 5 new = 13 total)
# ---------------------------------------------------------------------------

# Sanitisation
VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_TASK_ID_LEN = 64
MAX_DESCRIPTION_LEN = 4096
MAX_MEMORY_CONTENT = 10_000
ALLOWED_MODELS = frozenset({"", "deepseek-chat", "deepseek-reasoner"})


def sanitize_task_id(raw: str) -> str | None:
    cleaned = raw.strip().lower()
    # Truncate before further processing to avoid regex DoS on huge inputs
    cleaned = cleaned[:MAX_TASK_ID_LEN * 2]
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
    cleaned = cleaned.strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    # Enforce max length after normalisation
    cleaned = cleaned[:MAX_TASK_ID_LEN]
    cleaned = cleaned.strip("-")
    if not cleaned or not VALID_ID_RE.match(cleaned):
        return None
    return cleaned


def generate_task_id(text: str) -> str:
    words = re.sub(r"[^a-zA-Z0-9 ]", "", text).lower().split()
    slug = "-".join(words[:4]) or "task"
    slug = slug[:30]
    short_hash = hashlib.md5(f"{text}{time.time()}".encode()).hexdigest()[:5]
    return f"{slug}-{short_hash}"


# --- Existing tools ---

def tool_spawn_agent(task_id: str, description: str, model: str = "",
                     priority: int = 5) -> str:
    """Spawn a coding agent."""
    if not description:
        return "ERROR: description is required"
    if len(description) > MAX_DESCRIPTION_LEN:
        return f"ERROR: description too long ({len(description)} chars, max {MAX_DESCRIPTION_LEN})"
    if model and model not in ALLOWED_MODELS:
        return f"ERROR: invalid model '{model}'"
    try:
        priority = max(1, min(10, int(priority)))
    except (TypeError, ValueError):
        priority = 5

    tid = sanitize_task_id(task_id)
    if not tid:
        tid = generate_task_id(description)

    cmd = [str(SCRIPT_DIR / "spawn-agent.sh"), tid, description]
    if model:
        cmd += ["--model", model]
    if priority != 5:
        cmd += ["--priority", str(priority)]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_DIR),
            env={**os.environ,
                 "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"},
        )
        output = (result.stdout + result.stderr).strip() or "(no output)"
        if result.returncode != 0:
            return f"[FAILED exit {result.returncode}]\n{output}"
        return f"[SPAWNED task_id={tid}]\n{output}"
    except subprocess.TimeoutExpired:
        return "ERROR: spawn timed out after 60s"
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_swarm_status() -> str:
    """Get full swarm status."""
    try:
        result = subprocess.run(
            [str(SCRIPT_DIR / "list-tasks.sh")],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_DIR),
        )
        return (result.stdout + result.stderr).strip() or "(no output)"
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_read_logs(task_id: str, lines: int = 50) -> str:
    """Read the last N lines of an agent's log."""
    tid = sanitize_task_id(task_id)
    if not tid:
        return f"ERROR: invalid task ID '{task_id}'"

    log_file = LOG_DIR / f"{tid}.log"
    if not log_file.is_file():
        return f"No log file found for '{tid}'"

    try:
        with open(log_file) as fh:
            all_lines = fh.readlines()
        lines = max(1, min(lines, 500))
        tail = all_lines[-lines:]
        header = f"--- {tid}.log (last {len(tail)}/{len(all_lines)} lines) ---\n"
        return header + "".join(tail).strip()
    except Exception as exc:
        return f"ERROR reading log: {exc}"


def tool_kill_task(task_id: str) -> str:
    """Kill an agent's tmux session and mark it killed."""
    tid = sanitize_task_id(task_id)
    if not tid:
        return f"ERROR: invalid task ID '{task_id}'"

    project_name = PROJECT_DIR.name
    session_name = f"agent-{project_name}-{tid}"
    parts: list[str] = []

    try:
        r = subprocess.run(["tmux", "kill-session", "-t", session_name],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            parts.append(f"Killed tmux session '{session_name}'")
        else:
            parts.append(f"No tmux session '{session_name}' found")
    except Exception as exc:
        parts.append(f"tmux error: {exc}")

    # Update JSON directly in Python — avoids bash -c with string interpolation.
    try:
        if ACTIVE_FILE.is_file():
            lock_path = ACTIVE_FILE.with_suffix(".lock")
            with open(lock_path, "w") as lock_fd:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    with open(ACTIVE_FILE) as fh:
                        tasks = json.load(fh)
                    for task in tasks:
                        if isinstance(task, dict) and task.get("id") == tid:
                            task["status"] = "killed"
                    tmp = ACTIVE_FILE.with_suffix(".tmp")
                    with open(tmp, "w") as fh:
                        json.dump(tasks, fh, indent=2)
                    tmp.replace(ACTIVE_FILE)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
        parts.append(f"Marked '{tid}' as killed in active-tasks.json")
    except Exception as exc:
        parts.append(f"JSON update error: {exc}")

    return "\n".join(parts)


def tool_view_queue() -> str:
    """View the task queue."""
    if not QUEUE_FILE.is_file():
        return "Queue file not found"
    try:
        with open(QUEUE_FILE) as fh:
            data = json.load(fh)
        if not data:
            return "Queue is empty"
        data.sort(key=lambda t: (t.get("priority", 99), t.get("queued_at", "")))
        lines = [f"TASK QUEUE ({len(data)} tasks)", "=" * 40]
        for i, t in enumerate(data, 1):
            lines.append(f"{i}. [{t.get('model','?')}] p={t.get('priority','?')} "
                         f"id={t.get('id','?')} queued={t.get('queued_at','?')}")
            if t.get("description"):
                lines.append(f"   {t['description']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_active_tasks() -> str:
    """List active tasks as structured data."""
    try:
        with open(ACTIVE_FILE) as fh:
            data = json.load(fh)
        if not data:
            return "No active tasks"
        lines = [f"ACTIVE TASKS ({len(data)})", "=" * 40]
        for t in data:
            lines.append(f"[{t.get('status','?')}] {t.get('id','?')} | "
                         f"model={t.get('model','?')} | "
                         f"started={t.get('started_at','?')}")
            if t.get("description"):
                lines.append(f"  {t['description']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_completed_tasks(limit: int = 10) -> str:
    """List recently completed tasks."""
    try:
        with open(COMPLETED_FILE) as fh:
            data = json.load(fh)
        if not data:
            return "No completed tasks"
        data.sort(key=lambda t: t.get("completed_at", ""), reverse=True)
        data = data[:limit]
        lines = [f"COMPLETED TASKS (last {len(data)})", "=" * 40]
        for t in data:
            lines.append(f"[{t.get('status','?')}] {t.get('id','?')} | "
                         f"completed={t.get('completed_at','?')}")
            if t.get("description"):
                lines.append(f"  {t['description']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def _validate_readonly_shell_command(command: str) -> tuple[bool, str, list[str]]:
    """Validate and parse a shell command against a strict read-only allowlist."""
    raw = command.strip()
    if not raw:
        return False, "empty command", []

    # Reject shell composition/operators outright.
    forbidden_fragments = ("&&", "||", ";", "|", ">", "<", "`", "$(")
    if any(fragment in raw for fragment in forbidden_fragments):
        return False, "shell operators are not allowed", []

    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        return False, f"invalid shell syntax: {exc}", []

    if not tokens:
        return False, "empty command", []

    prog = tokens[0]

    if prog == "git":
        if len(tokens) < 2:
            return False, "git subcommand required", []
        sub = tokens[1]
        safe_subs = {"status", "log", "diff", "show", "rev-parse", "ls-files"}
        if sub in safe_subs:
            return True, "", tokens
        if sub == "worktree":
            if len(tokens) >= 3 and tokens[2] == "list":
                return True, "", tokens
            return False, "only 'git worktree list' is allowed", []
        if sub == "remote":
            if len(tokens) == 3 and tokens[2] == "-v":
                return True, "", tokens
            if len(tokens) >= 3 and tokens[2] == "show":
                return True, "", tokens
            return False, "only 'git remote -v' and 'git remote show' are allowed", []
        if sub == "branch":
            # Allow listing-only forms.
            allowed_flags = {"-a", "--all", "-r", "--remotes", "--list"}
            for tok in tokens[2:]:
                if tok.startswith("-") and tok not in allowed_flags:
                    return False, "only listing flags are allowed for 'git branch'", []
            return True, "", tokens
        return False, f"git subcommand '{sub}' is not allowed", []

    if prog == "gh":
        if len(tokens) >= 3 and tokens[1:3] in (["pr", "list"], ["pr", "view"], ["repo", "view"]):
            return True, "", tokens
        return False, "only 'gh pr list', 'gh pr view', and 'gh repo view' are allowed", []

    if prog == "find":
        # Block destructive/execute forms while allowing normal read-only searches.
        forbidden_find = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf"}
        for tok in tokens[1:]:
            if tok in forbidden_find:
                return False, f"find option '{tok}' is not allowed", []
        # Restrict path arguments to within the project directory.
        path_args = [t for t in tokens[1:] if not t.startswith("-")]
        for arg in path_args:
            try:
                resolved = Path(arg).resolve()
            except Exception:
                return False, f"invalid path argument: {arg!r}", []
            try:
                resolved.relative_to(PROJECT_DIR)
            except ValueError:
                return False, f"path '{arg}' is outside the project directory", []
        return True, "", tokens

    safe_single = {"ls", "pwd", "cat", "head", "tail", "wc", "rg", "jq"}
    if prog in safe_single:
        # Restrict path arguments to within the project directory to prevent
        # reading sensitive files outside the repo (e.g. /etc/passwd, ~/.ssh).
        # Use relative_to() rather than startswith() to avoid prefix-match bypass
        # (e.g. a sibling directory named 'agent-swarm-lab-evil' would fool startswith).
        path_args = [t for t in tokens[1:] if not t.startswith("-")]
        for arg in path_args:
            try:
                resolved = Path(arg).resolve()
            except Exception:
                return False, f"invalid path argument: {arg!r}", []
            try:
                resolved.relative_to(PROJECT_DIR)
            except ValueError:
                return False, f"path '{arg}' is outside the project directory", []
        return True, "", tokens

    return False, f"command '{prog}' is not in the read-only allowlist", []


def tool_shell_command(command: str) -> str:
    """Run a read-only shell command (git status, ls, cat, etc.)."""
    ok, reason, tokens = _validate_readonly_shell_command(command)
    if not ok:
        return f"BLOCKED: {reason}"

    try:
        result = subprocess.run(
            tokens,
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_DIR),
            env={**os.environ,
                 "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"},
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            output = f"[exit {result.returncode}]\n{output}"
        return output[:3000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out (30s)"
    except Exception as exc:
        return f"ERROR: {exc}"


# --- New tools (v2) ---

def tool_write_memory(content: str, target: str = "memory") -> str:
    """Write to long-term memory (MEMORY.md) or today's daily log."""
    if target not in ("memory", "daily"):
        return "ERROR: target must be 'memory' or 'daily'"
    if not content:
        return "ERROR: content is required"
    if len(content) > MAX_MEMORY_CONTENT:
        return f"ERROR: content too long ({len(content)} chars, max {MAX_MEMORY_CONTENT})"
    try:
        if target == "daily":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = MEMORY_DIR / f"{today}.md"
            timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
            entry = f"\n## {timestamp}\n{content.strip()}\n"
        else:
            path = MEMORY_FILE
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            entry = f"\n## {timestamp}\n{content.strip()}\n"

        with open(path, "a") as fh:
            fh.write(entry)

        return f"Written to {path.name}"
    except Exception as exc:
        return f"ERROR writing memory: {exc}"


def tool_read_memory(query: str = "") -> str:
    """Read MEMORY.md + search last 7 daily logs."""
    parts: list[str] = []

    # Read main memory
    memory = _read_workspace_file(MEMORY_FILE)
    if memory:
        parts.append("=== MEMORY.md ===")
        if query:
            # Filter to paragraphs containing query
            sections = memory.split("\n## ")
            matches = [s for s in sections if query.lower() in s.lower()]
            if matches:
                parts.append("\n## ".join(matches))
            else:
                parts.append("(no matching entries in MEMORY.md)")
        else:
            parts.append(memory)

    # Search last 7 daily logs
    today = datetime.now(timezone.utc)
    daily_parts = []
    for i in range(7):
        from datetime import timedelta
        day = today - timedelta(days=i)
        day_file = MEMORY_DIR / f"{day.strftime('%Y-%m-%d')}.md"
        if day_file.is_file():
            content = _read_workspace_file(day_file, MAX_DAILY_LOG)
            if query:
                sections = content.split("\n## ")
                matches = [s for s in sections if query.lower() in s.lower()]
                if matches:
                    daily_parts.append(f"--- {day.strftime('%Y-%m-%d')} ---")
                    daily_parts.append("\n## ".join(matches))
            else:
                daily_parts.append(f"--- {day.strftime('%Y-%m-%d')} ---")
                daily_parts.append(content)

    if daily_parts:
        parts.append("=== Daily Logs ===")
        parts.extend(daily_parts)

    return "\n".join(parts) if parts else "No memories found."


def tool_list_prs(state: str = "open") -> str:
    """List pull requests."""
    if state not in ("open", "closed", "all"):
        state = "open"
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", state, "--limit", "20"],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_DIR),
            env={**os.environ,
                 "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"},
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"[exit {result.returncode}]\n{output}"
        return output or "No PRs found."
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_view_pr(number: int) -> str:
    """View details of a pull request."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(number)],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_DIR),
            env={**os.environ,
                 "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"},
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"[exit {result.returncode}]\n{output}"
        return output[:3000] or "(no output)"
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_merge_pr(number: int) -> str:
    """Merge a pull request with squash and delete branch."""
    try:
        result = subprocess.run(
            ["gh", "pr", "merge", str(number), "--squash", "--delete-branch"],
            capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_DIR),
            env={**os.environ,
                 "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"},
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"[FAILED exit {result.returncode}]\n{output}"
        return output or "PR merged successfully."
    except Exception as exc:
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# 10b. MCP Client (JSON-RPC 2.0 over stdio)
# ---------------------------------------------------------------------------

class MCPClient:
    """Manages a single MCP server subprocess."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.command = config["command"]
        self.args = config.get("args", [])
        self.cwd = config.get("cwd")
        self.env = config.get("env", {})
        self.description = config.get("description", "")
        self.timeout = config.get("timeout", 30)
        self.tools: list[dict] = []
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> bool:
        """Spawn process, run initialize handshake, discover tools."""
        try:
            env = {**os.environ, **self.env}
            self._proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )

            # Drain stderr in daemon thread to prevent pipe deadlock
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True)
            self._stderr_thread.start()

            # Initialize handshake
            resp = self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "seabone-bot", "version": VERSION},
            })
            if not resp:
                log.error("MCP[%s]: initialize failed (no response)", self.name)
                self.stop()
                return False

            if "error" in resp:
                log.error("MCP[%s]: initialize error: %s", self.name,
                          resp["error"].get("message", resp["error"]))
                self.stop()
                return False

            # Send initialized notification
            self._notify("notifications/initialized", {})

            # Discover tools
            tools_resp = self._request("tools/list", {})
            if tools_resp and "result" in tools_resp:
                self.tools = tools_resp["result"].get("tools", [])

            log.info("MCP[%s]: connected, %d tools discovered",
                     self.name, len(self.tools))
            return True

        except Exception as exc:
            log.error("MCP[%s]: start failed: %s", self.name, exc)
            self.stop()
            return False

    def _request(self, method: str, params: dict) -> dict | None:
        """Send JSON-RPC request and wait for matching response."""
        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            msg = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            return self._send_and_receive(msg, req_id)

    def _notify(self, method: str, params: dict) -> None:
        """Send JSON-RPC notification (no id, no response expected)."""
        with self._lock:
            msg = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
            self._write(msg)

    def _send_and_receive(self, msg: dict, req_id: int) -> dict | None:
        """Write message and read response with matching id."""
        self._write(msg)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            line = self._readline_with_timeout(remaining)
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip server-sent notifications (no "id" field)
            if "id" not in resp:
                continue
            if resp.get("id") == req_id:
                return resp
        return None

    def _write(self, msg: dict) -> None:
        """Write a JSON-RPC message to stdin."""
        if not self._proc or not self._proc.stdin:
            return
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        try:
            self._proc.stdin.write(line.encode())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log.warning("MCP[%s]: write failed: %s", self.name, exc)

    def _readline_with_timeout(self, timeout: float) -> str:
        """Read a line from stdout with select-based timeout."""
        if not self._proc or not self._proc.stdout:
            return ""
        try:
            fd = self._proc.stdout.fileno()
            ready, _, _ = _select_mod.select([fd], [], [], timeout)
            if ready:
                line = self._proc.stdout.readline()
                return line.decode().strip() if line else ""
        except (ValueError, OSError):
            pass
        return ""

    def _drain_stderr(self) -> None:
        """Daemon thread: read stderr to prevent pipe deadlock."""
        if not self._proc or not self._proc.stderr:
            return
        try:
            for line in self._proc.stderr:
                text = line.decode().strip() if isinstance(line, bytes) else line.strip()
                if text:
                    log.debug("MCP[%s] stderr: %s", self.name, text[:200])
        except (ValueError, OSError):
            pass

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call an MCP tool and return text result."""
        resp = self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if not resp:
            return f"ERROR: MCP[{self.name}] tool call timed out"
        if "error" in resp:
            err = resp["error"]
            return f"ERROR: {err.get('message', 'unknown error')}"
        result = resp.get("result", {})
        content = result.get("content", [])
        parts = []
        for item in content:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts) if parts else "(no output)"

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """Terminate the server process."""
        if not self._proc:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass
        self._proc = None
        self.tools = []

    def restart(self) -> bool:
        """Stop and restart the server."""
        log.info("MCP[%s]: restarting...", self.name)
        self.stop()
        return self.start()


class MCPManager:
    """Manages multiple MCP server connections."""

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}

    def start_all(self, config: dict) -> None:
        """Start all enabled MCP servers from config."""
        servers = config.get("mcp_servers", {})
        if not servers:
            return
        for name, srv_cfg in servers.items():
            if not srv_cfg.get("enabled", True):
                log.info("MCP[%s]: skipped (disabled)", name)
                continue
            if "command" not in srv_cfg:
                log.warning("MCP[%s]: skipped (no command)", name)
                continue
            client = MCPClient(name, srv_cfg)
            if client.start():
                self._clients[name] = client
            else:
                log.error("MCP[%s]: failed to start", name)

    def stop_all(self) -> None:
        """Stop all MCP servers."""
        if not self._clients:
            return
        for name, client in self._clients.items():
            log.info("MCP[%s]: stopping...", name)
            client.stop()
        self._clients.clear()
        log.info("MCP: all servers stopped")

    def get_openai_tools(self) -> list[dict]:
        """Convert MCP tools to OpenAI function-calling format."""
        tools = []
        for server_name, client in self._clients.items():
            if not client.alive:
                continue
            for tool in client.tools:
                namespaced = f"mcp_{server_name}_{tool['name']}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": namespaced,
                        "description": (
                            tool.get("description", "") +
                            f" [MCP: {server_name}]"
                        ),
                        "parameters": tool.get("inputSchema", {
                            "type": "object", "properties": {}
                        }),
                    },
                })
        return tools

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return name.startswith("mcp_") and any(
            name.startswith(f"mcp_{sn}_") for sn in self._clients
        )

    def call_tool(self, namespaced_name: str, arguments: dict) -> str:
        """Route a tool call to the correct MCP server."""
        for server_name, client in self._clients.items():
            prefix = f"mcp_{server_name}_"
            if namespaced_name.startswith(prefix):
                tool_name = namespaced_name[len(prefix):]
                if not client.alive:
                    log.warning("MCP[%s]: server dead, attempting restart",
                                server_name)
                    if not client.restart():
                        return f"ERROR: MCP[{server_name}] failed to restart"
                return client.call_tool(tool_name, arguments)
        return f"ERROR: no MCP server found for tool '{namespaced_name}'"

    def health_check(self) -> dict:
        """Return health status for all servers."""
        status = {}
        for name, client in self._clients.items():
            status[name] = {
                "alive": client.alive,
                "tools": len(client.tools),
                "description": client.description,
            }
        return status


mcp_manager = MCPManager()


# ---------------------------------------------------------------------------
# 11. Tool registry + TOOLS list + dispatch map
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": "Spawn a new autonomous coding agent to work on a task. The agent runs aider+DeepSeek in an isolated git worktree and auto-creates a PR when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Short kebab-case task identifier (e.g. 'fix-login-bug'). Will be auto-generated if empty."},
                    "description": {"type": "string", "description": "What the agent should implement — be specific and detailed."},
                    "model": {"type": "string", "description": "Model override (e.g. 'deepseek-chat', 'deepseek-reasoner'). Leave empty for auto-selection."},
                    "priority": {"type": "integer", "description": "Priority 1-10 (1=urgent, 5=normal, 10=low). Default 5."},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swarm_status",
            "description": "Get full swarm status: active tasks, queued tasks, tmux sessions, model memory, completed tasks, open PRs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_logs",
            "description": "Read the last N lines of a specific agent's log file to see what it's doing or what happened.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID whose logs to read."},
                    "lines": {"type": "integer", "description": "Number of lines to tail (default 50, max 500)."},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_task",
            "description": "Kill a running agent — stops its tmux session and marks it killed in the task registry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID to kill."},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_queue",
            "description": "View all tasks waiting in the queue, sorted by priority.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "active_tasks",
            "description": "List currently active/running tasks with their status, model, and timestamps.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "completed_tasks",
            "description": "List recently completed tasks and their final status (pr_created, failed, no_changes, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many recent tasks to show (default 10)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_command",
            "description": "Run a read-only shell command in the project directory (git log, ls, cat, gh pr list, etc.). Destructive commands are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                },
                "required": ["command"],
            },
        },
    },
    # --- New tools (v2) ---
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": "Write to long-term memory (persists across sessions). Use proactively to remember operator preferences, project patterns, task outcomes, recurring issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What to remember. Be concise and structured."},
                    "target": {"type": "string", "enum": ["memory", "daily"], "description": "'memory' for MEMORY.md (permanent), 'daily' for today's log. Default: 'memory'."},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Search long-term memory (MEMORY.md) and recent daily logs. Use before complex tasks to check for prior context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term to filter memories. Leave empty to read all."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_prs",
            "description": "List GitHub pull requests for this repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "PR state filter. Default: 'open'."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_pr",
            "description": "View details of a specific GitHub pull request (title, description, diff summary, review status).",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "The PR number."},
                },
                "required": ["number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_pr",
            "description": "Merge a GitHub pull request using squash merge and delete the source branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "The PR number to merge."},
                },
                "required": ["number"],
            },
        },
    },
]

TOOL_DISPATCH: dict[str, callable] = {
    "spawn_agent": lambda args: tool_spawn_agent(
        args.get("task_id", ""), args["description"],
        args.get("model", ""), args.get("priority", 5)),
    "swarm_status": lambda args: tool_swarm_status(),
    "read_logs": lambda args: tool_read_logs(args["task_id"], args.get("lines", 50)),
    "kill_task": lambda args: tool_kill_task(args["task_id"]),
    "view_queue": lambda args: tool_view_queue(),
    "active_tasks": lambda args: tool_active_tasks(),
    "completed_tasks": lambda args: tool_completed_tasks(args.get("limit", 10)),
    "shell_command": lambda args: tool_shell_command(args["command"]),
    # v2
    "write_memory": lambda args: tool_write_memory(
        args["content"], args.get("target", "memory")),
    "read_memory": lambda args: tool_read_memory(args.get("query", "")),
    "list_prs": lambda args: tool_list_prs(args.get("state", "open")),
    "view_pr": lambda args: tool_view_pr(args["number"]),
    "merge_pr": lambda args: tool_merge_pr(args["number"]),
}

WEBHOOK_ALLOWED_TOOL_NAMES = {
    "spawn_agent",
    "swarm_status",
    "read_logs",
    "view_queue",
    "active_tasks",
    "completed_tasks",
    "write_memory",
    "read_memory",
    "list_prs",
    "view_pr",
}


def _filter_tools(allowed: set[str] | None) -> list[dict]:
    """Return tool descriptors filtered by name, including MCP tools."""
    if not allowed:
        return TOOLS + mcp_manager.get_openai_tools()
    native = [tool for tool in TOOLS if tool["function"]["name"] in allowed]
    mcp_tools = [t for t in mcp_manager.get_openai_tools()
                 if t["function"]["name"] in allowed]
    return native + mcp_tools


# ---------------------------------------------------------------------------
# 12. DeepSeek agent loop (with compaction trigger)
# ---------------------------------------------------------------------------

def _ds_request(messages: list[dict], tools: list | None = None) -> dict:
    """Call DeepSeek chat completions API."""
    body: dict = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.7,
    }
    if tools:
        body["tools"] = tools

    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        DS_API, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def _compact_session(session: Session) -> None:
    """Pre-compaction memory flush: summarize + write to memory + compact."""
    log.info("Compacting session (message count exceeded threshold)")
    messages = session.load_context(CFG)
    if not messages:
        session.clear()
        log.info("Session compacted (empty context)")
        return

    # Build a summary request
    summary_messages = [
        {"role": "system", "content": (
            "You are a memory extraction assistant. Analyze the conversation below "
            "and extract key facts, decisions, operator preferences, and task outcomes "
            "worth remembering. Call write_memory for each important item. "
            "Be concise — only save genuinely useful information."
        )},
        *messages,
        {"role": "user", "content": (
            "Summarize the key facts from this conversation and write them to memory. "
            "Use write_memory with target='memory' for permanent facts and "
            "target='daily' for session-specific notes."
        )},
    ]

    summary_tools = [t for t in TOOLS if t["function"]["name"] == "write_memory"]
    memory_flush_ok = False

    try:
        response = _ds_request(summary_messages, tools=summary_tools)
        choice = response["choices"][0]
        msg = choice["message"]
        write_calls = 0
        write_failures = 0

        # Execute any write_memory calls
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc["function"]["name"] == "write_memory":
                    write_calls += 1
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                        result = tool_write_memory(fn_args["content"], fn_args.get("target", "memory"))
                        if result.startswith("ERROR"):
                            write_failures += 1
                            log.warning("Compaction write_memory returned error: %s", result)
                        else:
                            log.info("Compaction: wrote memory entry")
                    except Exception as exc:
                        write_failures += 1
                        log.warning("Compaction write_memory failed: %s", exc)

            if write_calls > 0 and write_failures == 0:
                memory_flush_ok = True
        elif msg.get("content"):
            # Fallback: persist model summary so compaction never drops context silently.
            result = tool_write_memory(msg["content"], "daily")
            if result.startswith("ERROR"):
                log.warning("Compaction fallback memory write failed: %s", result)
            else:
                memory_flush_ok = True
    except Exception as exc:
        log.warning("Compaction summary request failed: %s", exc)

    if memory_flush_ok:
        session.clear()
        log.info("Session compacted")
    else:
        log.warning("Compaction skipped: memory flush failed; keeping session context intact")


def agent_respond(session: Session, user_message: str,
                  source: str = "user",
                  allowed_tool_names: set[str] | None = None) -> str | None:
    """Run the full agent loop. Returns the final assistant reply text."""
    session.append({"role": "user", "content": user_message})

    # Check compaction threshold
    ctx_cfg = CFG.get("context", {})
    max_msgs = ctx_cfg.get("max_messages", 100)
    if session.message_count() > max_msgs:
        _compact_session(session)

    max_iterations = 5
    spawned_task_id = None
    final_reply = None
    active_tools = _filter_tools(allowed_tool_names)

    for iteration in range(max_iterations):
        context = session.load_context(CFG)
        messages = [{"role": "system", "content": _build_system_prompt()}] + context

        try:
            response = _ds_request(messages, tools=active_tools)
        except Exception as exc:
            log.error("DeepSeek API error: %s", exc)
            reply = f"(DeepSeek API error: {exc})"
            session.append({"role": "assistant", "content": reply})
            send_message(reply)
            return reply

        choice = response["choices"][0]
        msg = choice["message"]

        # If the model wants to call tools
        if msg.get("tool_calls"):
            session.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": msg["tool_calls"],
            })

            if msg.get("content"):
                send_message(msg["content"])

            for tc in msg["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    fn_args = {}

                log.info("Tool call: %s(%s)", fn_name,
                         json.dumps(fn_args)[:200])

                executor = TOOL_DISPATCH.get(fn_name)
                if allowed_tool_names is not None and fn_name not in allowed_tool_names:
                    tool_result = f"ERROR: tool '{fn_name}' is not allowed in this context"
                elif executor:
                    try:
                        tool_result = executor(fn_args)
                    except Exception as exc:
                        tool_result = f"ERROR: {exc}"
                elif mcp_manager.is_mcp_tool(fn_name):
                    try:
                        tool_result = mcp_manager.call_tool(fn_name, fn_args)
                    except Exception as exc:
                        tool_result = f"ERROR: {exc}"
                else:
                    tool_result = f"Unknown tool: {fn_name}"

                log.info("Tool result (%s): %s", fn_name, tool_result[:200])

                # Track spawn_agent calls for keyboard attachment
                if fn_name == "spawn_agent" and "[SPAWNED" in tool_result:
                    m = re.search(r"task_id=([a-zA-Z0-9._-]+)", tool_result)
                    if m:
                        spawned_task_id = m.group(1)

                session.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result[:3000],
                })

            continue

        # No tool calls — final text response
        reply = msg.get("content", "").strip()
        if not reply:
            reply = "(no response)"

        session.append({"role": "assistant", "content": reply})

        # Attach inline keyboard if we spawned an agent
        markup = None
        if spawned_task_id:
            markup = spawn_keyboard(spawned_task_id)

        send_message(reply, reply_markup=markup)
        final_reply = reply
        return final_reply

    # Exhausted iterations
    exhaust_msg = "(max tool iterations reached)"
    session.append({"role": "assistant", "content": exhaust_msg})
    send_message("(reached max tool iterations — try a simpler request)")
    return exhaust_msg


# ---------------------------------------------------------------------------
# 13. Cron scheduler (thread, minimal cron parser)
# ---------------------------------------------------------------------------

def _cron_field_matches(field: str, value: int, max_val: int) -> bool:
    """Check if a cron field matches a value.

    Supports: number, *, */N, comma-separated, ranges (N-M).
    """
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
                if step > 0 and value % step == 0:
                    return True
            except ValueError:
                pass
            continue
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                lo_i = int(lo)
                hi_i = int(hi)
                if max_val == 7:
                    # Cron DOW allows 7 as Sunday; normalize to 0.
                    if lo_i == 7:
                        lo_i = 0
                    if hi_i == 7:
                        hi_i = 0
                if lo_i <= hi_i and lo_i <= value <= hi_i:
                    return True
                if lo_i > hi_i and (value >= lo_i or value <= hi_i):
                    return True
            except ValueError:
                pass
            continue
        try:
            part_i = int(part)
            if max_val == 7 and part_i == 7:
                part_i = 0
            if part_i == value:
                return True
        except ValueError:
            pass
    return False


def _cron_matches(expression: str, dt: datetime) -> bool:
    """Check if a 5-field cron expression matches a datetime.

    Fields: minute hour day-of-month month day-of-week (0=Sun or 7=Sun).
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        return False

    minute, hour, dom, month, dow = fields
    vals = [
        (minute, dt.minute, 59),
        (hour, dt.hour, 23),
        (dom, dt.day, 31),
        (month, dt.month, 12),
        (dow, dt.weekday() + 1 if dt.weekday() < 6 else 0, 7),
        # Python weekday: Mon=0..Sun=6 → cron: Sun=0, Mon=1..Sat=6
    ]
    # Fix: convert Python weekday to cron dow
    # Python: Mon=0, Tue=1, ..., Sun=6
    # Cron: Sun=0, Mon=1, ..., Sat=6
    py_dow = (dt.weekday() + 1) % 7  # Mon=1, ..., Sat=6, Sun=0
    vals[4] = (dow, py_dow, 7)

    for field, value, max_val in vals:
        if not _cron_field_matches(field, value, max_val):
            return False
    return True


def _trim_session_entries(path: Path, max_entries: int) -> None:
    """Trim a JSONL session file to keep only the newest max_entries entries."""
    if max_entries <= 0 or not path.is_file():
        return

    lock_path = path.with_suffix(".lock")
    lock_fh = None
    try:
        lock_fh = open(lock_path, "a+")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)

        with open(path) as fh:
            lines = fh.readlines()
        if len(lines) <= max_entries:
            return

        trimmed = lines[-max_entries:]
        tmp_path = path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w") as out:
            out.writelines(trimmed)
        os.replace(tmp_path, path)
        log.info("Trimmed session %s to %d entries", path.name, max_entries)
    except Exception as exc:
        log.warning("Failed trimming session %s: %s", path.name, exc)
    finally:
        if lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass


def _run_maintenance():
    """Session & memory maintenance task."""
    log.info("Running maintenance...")
    maint_cfg = CFG.get("session", {}).get("maintenance", {})
    prune_days = maint_cfg.get("prune_after_days", 30)
    max_entries = maint_cfg.get("max_entries", 10000)
    max_disk_mb = maint_cfg.get("max_disk_mb", 100)
    log_retention = CFG.get("log_retention_days", 14)

    now = time.time()
    total_size = 0

    # Prune old session files
    session_files = []
    if SESSIONS_DIR.is_dir():
        for f in SESSIONS_DIR.glob("*.jsonl"):
            age_days = (now - f.stat().st_mtime) / 86400
            size = f.stat().st_size
            total_size += size
            session_files.append((age_days, size, f))

            if age_days > prune_days:
                log.info("Pruning old session: %s (%.0f days old)", f.name, age_days)
                f.unlink(missing_ok=True)
                # Clean up lock file too
                lock = f.with_suffix(".lock")
                lock.unlink(missing_ok=True)
            else:
                _trim_session_entries(f, max_entries)

    # If total disk exceeds limit, remove oldest
    if total_size > max_disk_mb * 1024 * 1024:
        session_files.sort(key=lambda x: x[0], reverse=True)  # oldest first
        for age_days, size, f in session_files:
            if total_size <= max_disk_mb * 1024 * 1024:
                break
            if f.is_file():
                log.info("Disk limit: removing %s", f.name)
                f.unlink(missing_ok=True)
                lock = f.with_suffix(".lock")
                lock.unlink(missing_ok=True)
                total_size -= size

    # Clean old daily logs
    if MEMORY_DIR.is_dir():
        for f in MEMORY_DIR.glob("*.md"):
            age_days = (now - f.stat().st_mtime) / 86400
            if age_days > log_retention:
                log.info("Pruning old daily log: %s", f.name)
                f.unlink(missing_ok=True)

    log.info("Maintenance complete")


def _cron_loop():
    """Cron scheduler loop — runs in daemon thread."""
    last_run: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"

    while _running:
        try:
            now = datetime.now(timezone.utc)
            minute_key = now.strftime("%Y-%m-%d %H:%M")

            for job in CFG.get("cron_jobs", []):
                if not job.get("enabled", True):
                    continue
                job_id = job.get("id", "")
                schedule = job.get("schedule", "")
                action = job.get("action", "")

                if not job_id or not schedule or not action:
                    continue
                if last_run.get(job_id) == minute_key:
                    continue
                if not _cron_matches(schedule, now):
                    continue

                last_run[job_id] = minute_key
                log.info("Cron firing: %s (%s)", job_id, schedule)

                if action == "__maintenance__":
                    try:
                        _run_maintenance()
                    except Exception:
                        log.exception("Maintenance cron failed")
                else:
                    # Feed action into agent_respond with a cron session
                    try:
                        session = Session(f"cron_{job_id}")
                        session.append({"role": "system",
                                        "content": f"[Cron job '{job_id}' triggered]"})
                        agent_respond(session, action, source="cron")
                    except Exception:
                        log.exception("Cron job '%s' failed", job_id)

        except Exception:
            log.exception("Cron loop error")

        # Sleep ~60s but check _running frequently
        for _ in range(60):
            if not _running:
                break
            time.sleep(1)


# ---------------------------------------------------------------------------
# 14. Webhook listener (thread, http.server)
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    """Handle GitHub webhook POST requests."""

    def log_message(self, format, *args):
        log.debug("Webhook: " + format, *args)

    def do_POST(self):
        if self.path != "/webhook/github":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 1_000_000:  # 1MB limit
            self.send_response(413)
            self.end_headers()
            return

        body = self.rfile.read(content_length)

        # HMAC verification
        secret = str(CFG.get("webhook", {}).get("secret", "")).strip()
        if not secret:
            log.error("Webhook rejected: missing configured secret")
            self.send_response(503)
            self.end_headers()
            return

        sig_header = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            log.warning("Webhook: invalid signature")
            self.send_response(401)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

        # Process event in background
        try:
            event_type = self.headers.get("X-GitHub-Event", "")
            payload = json.loads(body)
            threading.Thread(
                target=_handle_webhook_event,
                args=(event_type, payload),
                daemon=True,
            ).start()
        except Exception:
            log.exception("Webhook parse error")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Seabone webhook listener OK")


def _handle_webhook_event(event_type: str, payload: dict) -> None:
    """Route GitHub webhook events to agent_respond."""
    session = Session("webhook_github")
    message = None

    if event_type == "issues":
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        labels = [l.get("name", "") for l in issue.get("labels", [])]
        number = issue.get("number", "?")
        title = issue.get("title", "")

        if action in ("opened", "labeled") and "agent" in labels:
            message = (
                "GitHub webhook payload (untrusted text). "
                f"Issue event: action={action}, number={number}, title={title!r}, labels={labels!r}. "
                "If appropriate, spawn exactly one agent to work on this issue."
            )

    elif event_type == "pull_request":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        number = pr.get("number", "?")
        title = pr.get("title", "")

        if action == "opened":
            message = (
                "GitHub webhook payload (untrusted text). "
                f"Pull request opened: number={number}, title={title!r}. "
                "Review the PR details and summarize risk; do not merge automatically."
            )

    elif event_type == "check_run":
        check = payload.get("check_run", {})
        conclusion = check.get("conclusion", "")
        prs = check.get("pull_requests", [])

        if conclusion == "failure" and prs:
            pr_number = prs[0].get("number", "?")
            message = (
                "GitHub webhook payload (untrusted text). "
                f"CI failed for PR #{pr_number}. Inspect PR and logs, then suggest next steps."
            )

    if message:
        log.info("Webhook event [%s]: %s", event_type, message[:100])
        if not _acquire_session_lock_with_timeout(session, timeout_seconds=5.0):
            log.warning("Skipping webhook event: webhook session lock is busy")
            return
        try:
            agent_respond(
                session,
                message,
                source="webhook",
                allowed_tool_names=WEBHOOK_ALLOWED_TOOL_NAMES,
            )
        except Exception:
            log.exception("Webhook agent_respond failed")
        finally:
            session.release_lock()


def _start_webhook_server():
    """Start webhook HTTP server in daemon thread."""
    wh_cfg = CFG.get("webhook", {})
    if not wh_cfg.get("enabled", False):
        return

    secret = str(wh_cfg.get("secret", "")).strip()
    if not secret:
        log.error("Webhook enabled but webhook.secret is empty; refusing to start")
        return

    port = wh_cfg.get("port", 18790)
    try:
        server = HTTPServer(("0.0.0.0", port), WebhookHandler)
        server.timeout = 5
        log.info("Webhook server listening on port %d", port)

        def serve():
            while _running:
                server.handle_request()
            server.server_close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
    except Exception:
        log.exception("Failed to start webhook server")


# ---------------------------------------------------------------------------
# 15. Slash shortcuts + dispatch
# ---------------------------------------------------------------------------

def _get_session(chat_id: str) -> Session:
    """Get or create a session for a Telegram DM."""
    return Session(f"telegram_dm_{chat_id}")


def _acquire_session_lock_with_timeout(session: Session, timeout_seconds: float = 5.0) -> bool:
    """Acquire a session lock with short retry loop."""
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while _running and time.monotonic() < deadline:
        if session.acquire_lock():
            return True
        time.sleep(0.1)
    return False


def _check_session_reset(session: Session) -> bool:
    """Check if session should be auto-reset. Returns True if reset."""
    sess_cfg = CFG.get("session", {})
    mode = sess_cfg.get("reset_mode", "manual")

    if mode == "daily":
        reset_hour = sess_cfg.get("reset_hour", 4)
        now = datetime.now(timezone.utc)
        last_ts = session.last_message_time()
        if last_ts > 0:
            last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)
            # Reset if we've passed the reset hour since last message
            if last_dt.date() < now.date() or (
                last_dt.date() == now.date()
                and last_dt.hour < reset_hour <= now.hour
            ):
                session.clear()
                log.info("Daily session reset triggered")
                return True

    elif mode == "idle":
        idle_minutes = sess_cfg.get("idle_minutes", 120)
        last_ts = session.last_message_time()
        if last_ts > 0:
            idle_secs = time.time() - last_ts
            if idle_secs > idle_minutes * 60:
                session.clear()
                log.info("Idle session reset triggered (%.0f min idle)",
                         idle_secs / 60)
                return True

    return False


def cmd_help(session: Session) -> None:
    text = (
        "*Seabone Agent v2*\n\n"
        "Just talk to me \u2014 I'll decide what to do.\n\n"
        "*Quick commands:*\n"
        "`/status` \u2014 swarm status\n"
        "`/logs <id> [N]` \u2014 tail agent logs\n"
        "`/kill <id>` \u2014 kill an agent\n"
        "`/queue` \u2014 view task queue\n"
        "`/prs [state]` \u2014 list pull requests\n"
        "`/memory [query]` \u2014 read memory\n"
        "`/mcp` \u2014 MCP server status\n"
        "`/clear` \u2014 clear conversation\n"
        "`/help` \u2014 this message\n\n"
        "Or just say things like:\n"
        '_"Fix the login bug in auth.py"_\n'
        '_"What agents are running?"_\n'
        '_"Show me the logs for fix-bug-123"_\n'
        '_"Merge PR #5"_'
    )
    send_message(text, parse_mode="Markdown", reply_markup=help_keyboard())


def cmd_clear(session: Session) -> None:
    session.clear()
    send_message("Conversation cleared. Fresh start!")


def cmd_mcp() -> None:
    """Show MCP server health status."""
    health = mcp_manager.health_check()
    if not health:
        send_message("No MCP servers configured.")
        return
    lines = ["*MCP Servers*\n"]
    for name, info in health.items():
        icon = "\u2705" if info["alive"] else "\u274c"
        desc = f" \u2014 {info['description']}" if info["description"] else ""
        lines.append(f"{icon} `{name}`: {info['tools']} tools{desc}")
    send_message("\n".join(lines), parse_mode="Markdown")


SHORTCUTS: dict[str, callable] = {
    "/help": lambda s, _: cmd_help(s),
    "/start": lambda s, _: cmd_help(s),
    "/clear": lambda s, _: cmd_clear(s),
    "/status": lambda s, _: agent_respond(s, "Show me the current swarm status."),
    "/queue": lambda s, _: agent_respond(s, "Show me the task queue."),
    "/logs": lambda s, a: agent_respond(
        s, f"Show me the logs for {a}" if a else "Which task logs should I show?"),
    "/kill": lambda s, a: agent_respond(
        s, f"Kill the agent {a}" if a else "Which task should I kill?"),
    "/spawn": lambda s, a: agent_respond(
        s, f"Spawn an agent: {a}" if a else "What should the agent work on?"),
    "/do": lambda s, a: agent_respond(
        s, f"Spawn an agent to: {a}" if a else "What should I do?"),
    "/prs": lambda s, a: agent_respond(
        s, f"List {a} pull requests." if a else "List open pull requests."),
    "/memory": lambda s, a: agent_respond(
        s, f"Read memory about: {a}" if a else "Read all memory."),
    "/mcp": lambda s, _: cmd_mcp(),
}


def dispatch(chat_id: str, text: str) -> None:
    """Route incoming text to handler."""
    text = text.strip()
    if not text:
        return

    session = _get_session(chat_id)

    # Try to acquire session lock (non-blocking)
    if not session.acquire_lock():
        send_message("I'm already processing a message. Please wait a moment.")
        return

    try:
        # Check auto-reset
        was_reset = _check_session_reset(session)

        if text.startswith("/"):
            parts = text.split(None, 1)
            cmd = parts[0].split("@")[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            handler = SHORTCUTS.get(cmd)
            if handler:
                handler(session, args)
            else:
                send_message(f"Unknown command: {cmd}\nTry /help")
        else:
            agent_respond(session, text)

    finally:
        session.release_lock()


def handle_callback_query(callback_query: dict) -> None:
    """Handle inline keyboard button presses."""
    cb_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))

    if not data or chat_id != str(CHAT_ID):
        answer_callback_query(cb_id, "Unauthorized")
        return

    # Acknowledge immediately
    answer_callback_query(cb_id)

    # Parse "action:param"
    if ":" in data:
        action, param = data.split(":", 1)
    else:
        action, param = data, ""

    session = _get_session(chat_id)
    if not session.acquire_lock():
        send_message("I'm already processing a request. Please wait.")
        return

    try:
        action_map = {
            "logs": lambda p: agent_respond(
                session, f"Show me the logs for {p}" if p else "Show me recent logs."),
            "kill": lambda p: agent_respond(
                session, f"Kill the agent {p}" if p else "Which task should I kill?"),
            "status": lambda p: agent_respond(
                session, "Show me the current swarm status."),
            "queue": lambda p: agent_respond(
                session, "Show me the task queue."),
            "active": lambda p: agent_respond(
                session, "Show me the active tasks."),
            "completed": lambda p: agent_respond(
                session, "Show me the completed tasks."),
            "retry": lambda p: agent_respond(
                session, f"Retry the failed task {p}. Read its logs first, "
                         f"then spawn a new agent with a fix." if p
                else "Which task should I retry?"),
        }

        handler = action_map.get(action)
        if handler:
            handler(param)
        else:
            send_message(f"Unknown action: {action}")
    finally:
        session.release_lock()


# ---------------------------------------------------------------------------
# 16. Main loop (Telegram poller + threads)
# ---------------------------------------------------------------------------
_running = True


def _shutdown(signum, _frame):
    global _running
    log.info("Received signal %s, shutting down...", signum)
    _running = False


def _interruptible_sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        time.sleep(min(0.5, end - time.monotonic()))


def is_authorised(chat_id) -> bool:
    return str(chat_id) == str(CHAT_ID)


def main() -> None:
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Bootstrap
    ensure_workspace()
    _migrate_history()

    # Start background threads
    cron_thread = threading.Thread(target=_cron_loop, daemon=True)
    cron_thread.start()
    log.info("Cron scheduler started")

    _start_webhook_server()

    # Start MCP servers
    mcp_manager.start_all(CFG)

    session = _get_session(CHAT_ID)
    msg_count = session.message_count()
    mcp_count = sum(len(c.tools) for c in mcp_manager._clients.values())
    log.info("Seabone Agent Bot v2 started (chat_id=%s, session=%d messages, "
             "%d MCP tools)", CHAT_ID, msg_count, mcp_count)

    offset = 0

    while _running:
        try:
            result = tg_request("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=35)

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                # Handle callback queries (inline keyboard)
                if "callback_query" in update:
                    cb = update["callback_query"]
                    try:
                        handle_callback_query(cb)
                    except Exception:
                        log.exception("Error handling callback query")
                    continue

                # Handle messages
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")

                if not is_authorised(chat_id):
                    log.warning("Ignoring unauthorised chat %s", chat_id)
                    continue

                if text:
                    log.info("Received: %s", text[:120])
                    try:
                        dispatch(str(chat_id), text)
                    except Exception:
                        log.exception("Error in dispatch")
                        try:
                            send_message("Internal error — check server logs.")
                        except Exception:
                            pass

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("Network error: %s (retrying in 5s)", exc)
            _interruptible_sleep(5)
        except Exception:
            log.exception("Unexpected error (retrying in 5s)")
            _interruptible_sleep(5)

    # Shutdown MCP servers
    mcp_manager.stop_all()


if __name__ == "__main__":
    main()
