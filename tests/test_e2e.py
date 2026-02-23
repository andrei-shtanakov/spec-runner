"""E2E integration tests using fake_claude.sh.

These tests exercise the full execution pipeline without mocking subprocess.
All tests are marked @pytest.mark.slow.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task, run_with_retries
from spec_runner.state import ExecutorState
from spec_runner.task import get_next_tasks, parse_tasks, resolve_dependencies, update_task_status
from spec_runner.validate import validate_tasks

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


@pytest.mark.slow
class TestE2ERetry:
    """Retry scenarios through the full pipeline."""

    def test_failure_then_success(self, tmp_path: Path, monkeypatch):
        """First attempt TASK_FAILED, second succeeds."""
        config = _make_e2e_config(tmp_path, max_retries=3)
        state = ExecutorState(config)
        _write_tasks(tmp_path)
        tasks = parse_tasks(tmp_path / "spec" / "tasks.md")
        task = tasks[0]

        resp_dir = tmp_path / "responses"
        resp_dir.mkdir(exist_ok=True)
        base = resp_dir / "retry"
        (resp_dir / "retry.0").write_text("Could not complete.\nTASK_FAILED: syntax error")
        (resp_dir / "retry.1").write_text("Fixed and done.\nTASK_COMPLETE")

        counter = tmp_path / "counter.txt"
        monkeypatch.setenv("FAKE_RESPONSE_FILE", str(base))
        monkeypatch.setenv("FAKE_COUNTER_FILE", str(counter))
        monkeypatch.delenv("FAKE_EXIT_CODE", raising=False)
        monkeypatch.delenv("FAKE_STDERR", raising=False)
        monkeypatch.delenv("FAKE_DELAY", raising=False)

        result = run_with_retries(task, config, state)
        assert result is True

        ts = state.get_task_state(task.id)
        assert len(ts.attempts) == 2
        assert ts.attempts[0].success is False
        assert ts.attempts[1].success is True

    def test_rate_limit_retries_and_succeeds(self, tmp_path: Path, monkeypatch):
        """Rate limit triggers backoff retry, then succeeds."""
        config = _make_e2e_config(tmp_path, max_retries=3)
        state = ExecutorState(config)
        _write_tasks(tmp_path)
        tasks = parse_tasks(tmp_path / "spec" / "tasks.md")
        task = tasks[0]

        resp_dir = tmp_path / "responses"
        resp_dir.mkdir(exist_ok=True)
        base = resp_dir / "ratelimit"
        (resp_dir / "ratelimit.0").write_text("you've hit your limit")
        (resp_dir / "ratelimit.1").write_text("Done!\nTASK_COMPLETE")

        counter = tmp_path / "counter.txt"
        monkeypatch.setenv("FAKE_RESPONSE_FILE", str(base))
        monkeypatch.setenv("FAKE_COUNTER_FILE", str(counter))
        monkeypatch.delenv("FAKE_EXIT_CODE", raising=False)
        monkeypatch.delenv("FAKE_STDERR", raising=False)
        monkeypatch.delenv("FAKE_DELAY", raising=False)

        result = run_with_retries(task, config, state)
        assert result is True

        ts = state.get_task_state(task.id)
        assert len(ts.attempts) == 2
        assert ts.attempts[0].success is False
        assert ts.attempts[0].error_code is not None
        assert ts.attempts[0].error_code.value == "RATE_LIMIT"

    def test_all_attempts_fail(self, tmp_path: Path, monkeypatch):
        """All attempts fail — task gets skipped (default on_task_failure=skip)."""
        config = _make_e2e_config(tmp_path, max_retries=2)
        state = ExecutorState(config)
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        task = tasks[0]

        response_file = _write_response(
            tmp_path, "fail.txt", "Cannot do this.\nTASK_FAILED: impossible"
        )

        monkeypatch.setenv("FAKE_RESPONSE_FILE", str(response_file))
        monkeypatch.delenv("FAKE_COUNTER_FILE", raising=False)
        monkeypatch.delenv("FAKE_EXIT_CODE", raising=False)
        monkeypatch.delenv("FAKE_STDERR", raising=False)
        monkeypatch.delenv("FAKE_DELAY", raising=False)

        result = run_with_retries(task, config, state)
        assert result == "SKIP"

        ts = state.get_task_state(task.id)
        assert len(ts.attempts) == 2
        assert all(not a.success for a in ts.attempts)


# ---------------------------------------------------------------------------
# Multi-task / dependency / validation E2E data
# ---------------------------------------------------------------------------

MULTI_TASKS_MD = """\
# Tasks

### TASK-001: Setup database
\U0001f534 P0 | \u2b1c TODO | Est: 1h

**Checklist:**
- [ ] Create schema

### TASK-002: Add API endpoints
\U0001f7e0 P1 | \u2b1c TODO | Est: 2h

**Depends on:** [TASK-001]

**Checklist:**
- [ ] Create REST endpoints
"""

INVALID_TASKS_MD = """\
# Tasks

### TASK-001: First task
\U0001f534 P0 | \u2b1c TODO | Est: 1h

**Depends on:** [TASK-999]

**Checklist:**
- [ ] Do something
"""


@pytest.mark.slow
class TestE2EMultiTask:
    """Multi-task and dependency scenarios."""

    def test_dependency_ordering(self, tmp_path: Path):
        """TASK-002 depends on TASK-001 — only TASK-001 is next."""
        tasks_file = _write_tasks(tmp_path, MULTI_TASKS_MD)
        tasks = parse_tasks(tasks_file)
        resolve_dependencies(tasks)

        next_tasks = get_next_tasks(tasks)
        assert len(next_tasks) == 1
        assert next_tasks[0].id == "TASK-001"

        # After TASK-001 done, TASK-002 becomes available
        update_task_status(tasks_file, "TASK-001", "done")
        tasks = parse_tasks(tasks_file)
        resolve_dependencies(tasks)
        next_tasks = get_next_tasks(tasks)
        assert len(next_tasks) == 1
        assert next_tasks[0].id == "TASK-002"

    def test_validation_catches_missing_dependency(self, tmp_path: Path):
        """Invalid tasks.md with missing dependency ref triggers error."""
        tasks_file = _write_tasks(tmp_path, INVALID_TASKS_MD)
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("TASK-999" in e for e in result.errors)
