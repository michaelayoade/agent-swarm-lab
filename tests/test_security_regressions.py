"""
Security regression tests for agent-swarm-lab.

These tests ensure previously-fixed security issues don't reappear.
Run: pytest tests/ -v
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
SEABONE_DIR = PROJECT_DIR / ".seabone"


# --- Hardcoded credentials ---

class TestNoHardcodedCredentials:
    """Ensure no hardcoded API keys, passwords, or tokens in tracked files."""

    CREDENTIAL_PATTERNS = [
        r'''(?:api[_-]?key|apikey|secret|password|passwd|token)\s*[:=]\s*['"][A-Za-z0-9+/=_-]{20,}['"]''',
        r'''Bearer\s+[A-Za-z0-9+/=_-]{20,}(?![\w{$])''',
        r'''postgres(?:ql)?://\w+:[^${\s]{3,}@''',
    ]

    EXCLUDE_PATTERNS = [r'\.example$', r'\.md$', r'tests/', r'\.git/', r'node_modules/']

    def test_no_hardcoded_secrets_in_config(self):
        config = SEABONE_DIR / "config.json"
        if not config.exists():
            pytest.skip("config.json not found")
        content = config.read_text()
        assert "postgres:postgres@" not in content, \
            "Hardcoded postgres credentials found in config.json"

    def test_no_hardcoded_secrets_in_scripts(self):
        violations = []
        for script in SCRIPTS_DIR.glob("*.sh"):
            content = script.read_text()
            for pattern in self.CREDENTIAL_PATTERNS:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    violations.append(f"{script.name}: {matches[0][:40]}...")
        assert not violations, f"Hardcoded credentials found:\n" + "\n".join(violations)

    def test_env_example_exists(self):
        examples = list(PROJECT_DIR.glob(".env*.example"))
        assert len(examples) > 0, \
            "No .env.example file found -- required env vars must be documented"


# --- Input validation ---

class TestInputValidation:
    @pytest.fixture
    def telegram_bot_source(self):
        bot = SCRIPTS_DIR / "telegram-bot.py"
        if not bot.exists():
            pytest.skip("telegram-bot.py not found")
        return bot.read_text()

    def test_sanitize_task_id_has_length_limit(self, telegram_bot_source):
        assert "MAX_TASK_ID_LEN" in telegram_bot_source

    def test_spawn_agent_validates_description(self, telegram_bot_source):
        assert "MAX_DESCRIPTION_LEN" in telegram_bot_source

    def test_write_memory_validates_content_size(self, telegram_bot_source):
        assert "MAX_MEMORY_CONTENT" in telegram_bot_source

    def test_model_allowlist_exists(self, telegram_bot_source):
        assert "ALLOWED_MODELS" in telegram_bot_source

    def test_no_bash_c_with_user_input(self, telegram_bot_source):
        dangerous = re.findall(
            r'bash\s+-c.*\{.*(?:task_id|tid|user_input)',
            telegram_bot_source
        )
        assert not dangerous, f"Dangerous bash -c with user input: {dangerous}"

    def test_file_path_validation_exists(self, telegram_bot_source):
        assert "PROJECT_DIR" in telegram_bot_source or "project_dir" in telegram_bot_source

    def test_path_validation_uses_relative_to_not_startswith(self, telegram_bot_source):
        """relative_to() rejects sibling dirs like 'agent-swarm-lab-evil/'; startswith() does not."""
        assert "relative_to(PROJECT_DIR)" in telegram_bot_source, (
            "Path validation must use relative_to() instead of startswith() to prevent "
            "prefix-match bypass (e.g. a sibling dir named 'project-evil' would fool startswith)"
        )

    def test_find_command_has_path_restriction(self, telegram_bot_source):
        """find must restrict search paths to the project directory."""
        # The find block must contain both a path_args collection and relative_to check.
        find_block_idx = telegram_bot_source.find('if prog == "find"')
        assert find_block_idx != -1, "find validation block not found"
        # Grab a window of source after the find block (up to the safe_single block)
        safe_single_idx = telegram_bot_source.find('safe_single', find_block_idx)
        find_block = telegram_bot_source[find_block_idx:safe_single_idx]
        assert "relative_to" in find_block, (
            "find command must apply relative_to() path restriction to prevent "
            "enumeration of files outside the project directory (e.g. find /etc)"
        )


# --- Config integrity ---

class TestConfigIntegrity:
    @pytest.fixture
    def config(self):
        config_path = SEABONE_DIR / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not found")
        return json.loads(config_path.read_text())

    def test_max_concurrent_agents_bounded(self, config):
        max_agents = config.get("max_concurrent_agents", 5)
        assert 1 <= max_agents <= 10, f"max_concurrent_agents={max_agents} out of range"

    def test_agent_timeout_set(self, config):
        timeout = config.get("agent_timeout_minutes", 0)
        assert 5 <= timeout <= 120, f"agent_timeout_minutes={timeout} out of range"

    def test_max_retries_bounded(self, config):
        retries = config.get("max_retries", 3)
        assert retries <= 5, f"max_retries={retries} too high"

    def test_review_model_set(self, config):
        model = config.get("review_model", "")
        assert model, "review_model is not set"

    def test_all_json_files_valid(self):
        for json_file in SEABONE_DIR.glob("*.json"):
            try:
                json.loads(json_file.read_text())
            except json.JSONDecodeError as e:
                pytest.fail(f"{json_file.name} is invalid JSON: {e}")


# --- Script integrity ---

class TestScriptIntegrity:
    REQUIRED_SCRIPTS = [
        "spawn-agent.sh", "check-agents.sh", "cleanup-worktrees.sh",
        "list-tasks.sh", "seabone.sh",
    ]

    def test_required_scripts_exist(self):
        for script in self.REQUIRED_SCRIPTS:
            assert (SCRIPTS_DIR / script).exists(), f"Missing: {script}"

    def test_scripts_have_valid_bash_syntax(self):
        errors = []
        for script in SCRIPTS_DIR.glob("*.sh"):
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            if result.returncode != 0:
                errors.append(f"{script.name}: {result.stderr.strip()}")
        assert not errors, f"Bash syntax errors:\n" + "\n".join(errors)

    def test_python_scripts_have_valid_syntax(self):
        errors = []
        for script in SCRIPTS_DIR.glob("*.py"):
            result = subprocess.run(
                ["python3", "-m", "py_compile", str(script)], capture_output=True, text=True
            )
            if result.returncode != 0:
                errors.append(f"{script.name}: {result.stderr.strip()}")
        assert not errors, f"Python syntax errors:\n" + "\n".join(errors)

    def test_scripts_are_executable(self):
        non_exec = [s.name for s in SCRIPTS_DIR.glob("*.sh") if not os.access(s, os.X_OK)]
        assert not non_exec, f"Scripts not executable: {non_exec}"
