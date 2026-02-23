"""E2E integration tests using fake_claude.sh.

These tests exercise the full execution pipeline without mocking subprocess.
All tests are marked @pytest.mark.slow.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.state import ExecutorState
from spec_runner.task import parse_tasks

FAKE_CLI = Path(__file__).parent / "fixtures" / "fake_claude.sh"

MINIMAL_TASKS_MD = """\
# Tasks

### TASK-001: Add login page
\U0001f7e0 P1 | \u2b1c TODO | Est: 1h

**Checklist:**
- [ ] Create login form
- [ ] Add validation
"""


def _make_e2e_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create config pointing at fake CLI."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(exist_ok=True)

    defaults = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "claude_command": str(FAKE_CLI),
        "command_template": "{cmd} -p {prompt}",
        "skip_permissions": True,
        "max_retries": 3,
        "retry_delay_seconds": 0,
        "task_timeout_minutes": 1,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _write_tasks(tmp_path: Path, content: str = MINIMAL_TASKS_MD) -> Path:
    """Write tasks.md and return its path."""
    tasks_file = tmp_path / "spec" / "tasks.md"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(content)
    return tasks_file


def _write_response(tmp_path: Path, filename: str, content: str) -> Path:
    """Write a response file for fake CLI."""
    resp_dir = tmp_path / "responses"
    resp_dir.mkdir(exist_ok=True)
    resp = resp_dir / filename
    resp.write_text(content)
    return resp


@pytest.mark.slow
class TestE2ESingleTask:
    """Single task execution through the full pipeline."""

    def test_single_task_success(self, tmp_path: Path, monkeypatch):
        """Full cycle: tasks.md -> parse -> execute -> state.db shows success."""
        config = _make_e2e_config(tmp_path)
        state = ExecutorState(config)

        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        assert len(tasks) == 1
        task = tasks[0]

        response_file = _write_response(
            tmp_path, "success.txt", "Implemented login form.\nTASK_COMPLETE"
        )

        monkeypatch.setenv("FAKE_RESPONSE_FILE", str(response_file))
        monkeypatch.delenv("FAKE_COUNTER_FILE", raising=False)
        monkeypatch.delenv("FAKE_EXIT_CODE", raising=False)
        monkeypatch.delenv("FAKE_STDERR", raising=False)
        monkeypatch.delenv("FAKE_DELAY", raising=False)

        result = execute_task(task, config, state)
        assert result is True

        ts = state.get_task_state(task.id)
        assert ts is not None
        assert len(ts.attempts) == 1
        assert ts.attempts[0].success is True
