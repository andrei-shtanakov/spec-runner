"""E2E integration tests using fake_claude.sh.

These tests exercise the full execution pipeline without mocking subprocess.
All tests are marked @pytest.mark.slow.
"""

import argparse
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task, run_with_retries
from spec_runner.state import ExecutorState
from spec_runner.task import get_next_tasks, parse_tasks, resolve_dependencies, update_task_status
from spec_runner.validate import validate_tasks

FAKE_CLI = Path(__file__).parent / "fixtures" / "fake_claude.sh"
INCIDENT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "maestro-interop" / "alternating-bullet-tasks.md"
)

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

        with ExecutorState(config) as state:
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

        with ExecutorState(config) as state:
            result = run_with_retries(task, config, state)
            assert result is True

            ts = state.get_task_state(task.id)
            assert len(ts.attempts) == 2
            assert ts.attempts[0].success is False
            assert ts.attempts[1].success is True

    def test_rate_limit_retries_and_succeeds(self, tmp_path: Path, monkeypatch):
        """Rate limit triggers backoff retry, then succeeds."""
        config = _make_e2e_config(tmp_path, max_retries=3)
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

        with ExecutorState(config) as state:
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

        with ExecutorState(config) as state:
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


def _run_all_args(**overrides) -> argparse.Namespace:
    """Namespace for `_run_tasks(args, config)` in `run --all` mode.

    Mirrors the flag set `cmd_run`'s argparse subparser produces — see
    `tests/test_run_reconciliation.py` for the same convention.
    """
    base: dict = {
        "command": "run",
        "all": True,
        "task": None,
        "milestone": None,
        "restart": False,
        "dry_run": False,
        "json_result": False,
        "max_retries": None,
        "timeout": None,
        "no_tests": False,
        "no_branch": False,
        "no_commit": False,
        "no_review": False,
        "hitl_review": False,
        "callback_url": "",
        "tui": False,
        "no_reset_failed": False,
        "force": True,
        "allow_dirty_spec": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.slow
class TestE2ETaskStatusIntegrityIncident:
    """Full `run --all` regression for the disputatio D3 incident (#123,
    #124): `update_task_status` painting a neighboring task, `TASK_META`
    missing bullet-format meta lines, and `run --all` exiting 0 on a
    state-DB/tasks.md mismatch.

    The golden fixture (`alternating-bullet-tasks.md`) is the live forensic
    snapshot that triggered the incident — 11 real tasks with a real
    dependency graph, alternating bullet-format (`- 🔴 P0 | ...`) and plain
    (`🔴 P0 | ...`) meta lines, one already `IN_PROGRESS`.
    """

    def _config(self, tmp_path: Path) -> ExecutorConfig:
        return _make_e2e_config(tmp_path, max_retries=1)

    def _arm_fake_cli(self, tmp_path: Path, monkeypatch) -> None:
        response_file = _write_response(tmp_path, "complete.txt", "Done.\nTASK_COMPLETE")
        monkeypatch.setenv("FAKE_RESPONSE_FILE", str(response_file))
        monkeypatch.delenv("FAKE_COUNTER_FILE", raising=False)
        monkeypatch.delenv("FAKE_EXIT_CODE", raising=False)
        monkeypatch.delenv("FAKE_STDERR", raising=False)
        monkeypatch.delenv("FAKE_DELAY", raising=False)

    def test_run_all_executes_every_task_state_and_file_stay_in_sync(
        self, tmp_path: Path, monkeypatch
    ):
        """`run --all` against the golden fixture drives all 11 tasks to
        done; state-DB and tasks.md agree after every single task (not just
        at the end), and no task's `update_task_status` call ever changes a
        different task's status (the exact shape of the #123 regression:
        a bullet-meta boundary miss painting the next task's meta line)."""
        from spec_runner.cli import _run_tasks

        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True)
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(INCIDENT_FIXTURE.read_text())

        all_ids = [t.id for t in parse_tasks(tasks_file)]
        assert len(all_ids) == 11  # golden fixture shape (plan's acceptance)

        config = self._config(tmp_path)
        self._arm_fake_cli(tmp_path, monkeypatch)

        # Instrument update_task_status (as seen by execution.py/hooks.py,
        # the two call sites the run loop actually exercises) to confirm
        # every write leaves every OTHER task's status untouched.
        import spec_runner.execution as execution_mod
        import spec_runner.hooks as hooks_mod
        from spec_runner.task import update_task_status as real_update_task_status

        calls: list[tuple[str, str]] = []

        def _tracking_update(filepath: Path, task_id: str, new_status: str) -> bool:
            before = {t.id: t.status for t in parse_tasks(filepath)}
            ok = real_update_task_status(filepath, task_id, new_status)
            after = {t.id: t.status for t in parse_tasks(filepath)}
            for other_id, other_status in before.items():
                if other_id == task_id:
                    continue
                assert after.get(other_id) == other_status, (
                    f"update_task_status({task_id!r}, {new_status!r}) changed "
                    f"{other_id}: {other_status!r} -> {after.get(other_id)!r}"
                )
            calls.append((task_id, new_status))
            return ok

        monkeypatch.setattr(execution_mod, "update_task_status", _tracking_update)
        monkeypatch.setattr(hooks_mod, "update_task_status", _tracking_update)

        _run_tasks(_run_all_args(), config)  # must not raise SystemExit

        with ExecutorState(config) as state:
            assert state.get_meta("last_run_stop_reason") == "completed"
            for task_id in all_ids:
                ts = state.get_task_state(task_id)
                assert ts is not None and ts.status == "success", (
                    f"{task_id}: {ts.status if ts else 'missing'}"
                )

        final_tasks = parse_tasks(tasks_file)
        assert len(final_tasks) == 11
        for t in final_tasks:
            assert t.status == "done", f"{t.id}: {t.status}"

        # Every task got exactly one "done" write.
        done_calls = [tid for tid, status in calls if status == "done"]
        assert sorted(done_calls) == sorted(all_ids)

    def test_corrupted_meta_after_success_fails_closed(self, tmp_path: Path, monkeypatch):
        """Owner acceptance: corrupting a task's meta right after its
        success is recorded must make the run exit non-zero — Maestro's
        "workstream never reaches DONE" failure mode is entirely downstream
        of this exit code (Task 3's `state_spec_mismatch` gate)."""
        import spec_runner.cli as cli_mod
        from spec_runner.cli import _run_tasks
        from spec_runner.executor import run_with_retries as real_run_with_retries

        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True)
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(INCIDENT_FIXTURE.read_text())

        config = self._config(tmp_path)
        self._arm_fake_cli(tmp_path, monkeypatch)

        def _corrupting_run_with_retries(task, cfg, state):
            result = real_run_with_retries(task, cfg, state)
            if task.id == "TASK-001":
                # The meta gets reverted right after success is recorded —
                # e.g. a concurrent edit, or a regression reintroducing the
                # pre-#123 TASK_META that can no longer see the bullet-meta
                # line it just wrote.
                update_task_status(cfg.tasks_file, task.id, "todo")
            return result

        monkeypatch.setattr(cli_mod, "run_with_retries", _corrupting_run_with_retries)

        with pytest.raises(SystemExit) as excinfo:
            _run_tasks(_run_all_args(), config)
        assert excinfo.value.code == 1

        with ExecutorState(config) as state:
            assert state.get_meta("last_run_stop_reason") == "state_spec_mismatch"
            detail = state.get_meta("last_run_stop_detail") or ""
            assert "TASK-001" in detail
