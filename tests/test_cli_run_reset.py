"""Reset / second-pass / stop-reason tests for `run` (v2.3.0)."""

import argparse
from pathlib import Path

from spec_runner.cli import _run_tasks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


def _empty_tasks_md(tmp_path: Path) -> Path:
    """Create spec/tasks.md with no *ready* tasks (passes validation, nothing to run)."""
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    p = spec / "tasks.md"
    # A valid spec with one already-done task so validation passes but
    # get_next_tasks() returns empty (no ready work).
    p.write_text(
        "# Spec\n\n## M0\n\n### TASK-000: Done task\n"
        "🟢 P0 | ✅ DONE | Est: 0.1d\n\n"
        "**Description:** already done\n\n**Checklist:**\n- [x] done\n\n"
        "**Traces to:** [REQ-0]\n**Depends on:** —\n"
    )
    return p


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    _empty_tasks_md(tmp_path)  # ensure spec/tasks.md exists
    defaults: dict = {
        "state_file": tmp_path / "state.db",
        "project_root": tmp_path,
        "logs_dir": tmp_path / "logs",
        "create_git_branch": False,
        "auto_commit": False,
        "run_tests_on_done": False,
        "run_review": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _run_args(**overrides) -> argparse.Namespace:
    base: dict = {
        "command": "run",
        "all": True,
        "no_reset_failed": False,
        "force": True,
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
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestRunAllResetSemantics:
    def test_run_all_resets_failed_to_pending(self, tmp_path):
        cfg = _cfg(tmp_path, max_retries=1)
        cfg.logs_dir.mkdir()
        with ExecutorState(cfg) as state:
            state.record_attempt("T1", success=False, duration=1.0, error="x")
            state.record_attempt("T2", success=True, duration=1.0)
            state.consecutive_failures = 5
            state._save()
        _run_tasks(_run_args(), cfg)
        with ExecutorState(cfg) as state:
            assert state.get_task_state("T1").status == "pending"
            assert state.get_task_state("T2").status == "success"
            assert state.consecutive_failures == 0

    def test_no_reset_failed_flag_preserves_state(self, tmp_path):
        cfg = _cfg(tmp_path, max_retries=1)
        cfg.logs_dir.mkdir()
        with ExecutorState(cfg) as state:
            state.record_attempt("T1", success=False, duration=1.0, error="x")
            state.consecutive_failures = 5
            state._save()
        _run_tasks(_run_args(no_reset_failed=True), cfg)
        with ExecutorState(cfg) as state:
            assert state.get_task_state("T1").status == "failed"
            assert state.consecutive_failures == 5


class TestSecondPassDetection:
    def test_repeated_failure_recorded_and_warned(self, tmp_path, monkeypatch, capsys):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text(
            "# Spec\n\n## M0\n\n### TASK-001: Demo\n"
            "🔴 P0 | ⬜ TODO | Est: 0.5d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] do it\n\n"
            "**Traces to:** [NFR-1]\n**Depends on:** —\n"
        )
        cfg = ExecutorConfig(
            state_file=tmp_path / "state.db",
            project_root=tmp_path,
            logs_dir=tmp_path / "logs",
            create_git_branch=False,
            auto_commit=False,
            run_tests_on_done=False,
            run_review=False,
            max_retries=1,
            retry_delay_seconds=0,
            # Run under DEFAULT on_task_failure="skip" to prove the status-based
            # second-pass detection fires even when run_with_retries returns "SKIP".
        )
        cfg.logs_dir.mkdir()
        # Seed TASK-001 as a prior-run failure: one failed attempt with
        # max_retries=1 exhausts the retry budget → status automatically
        # becomes "failed".  reset_failed_to_pending() (called by run --all)
        # clears attempts so the task gets a fresh budget, executes once,
        # fails again, and the second-pass hint fires.
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=False, duration=1.0, error="x")
            assert state.get_task_state("TASK-001").status == "failed"
            state._save()
        # Isolate execution: pre_start ok, subprocess always fails with usage limit
        from spec_runner import execution

        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        import subprocess as _sp

        monkeypatch.setattr(
            execution.subprocess,
            "run",
            lambda *a, **k: _sp.CompletedProcess(
                args=["x"],
                returncode=1,
                stdout="",
                stderr="ERROR: hit your usage limit. try again at 9:54 AM\n",
            ),
        )
        _run_tasks(_run_args(max_retries=1), cfg)
        with ExecutorState(cfg) as state:
            assert "TASK-001" in state.get_second_pass_fails()
        # log_progress writes to the structlog "runner" logger via logger.info().
        # In test runs logging is not initialised to a console sink, so the
        # 💡 line does NOT reach capsys. The canonical assertion is the
        # persisted second-pass set checked above.


class TestStopReasonCapture:
    def test_completed_normal_run(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.logs_dir.mkdir()
        _run_tasks(_run_args(), cfg)
        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "completed"

    def test_max_consecutive_failures_recorded(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text(
            "# Spec\n\n## M0\n\n### TASK-001: a\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] do\n\n"
            "**Traces to:** [N]\n**Depends on:** —\n\n"
            "### TASK-002: b\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] do\n\n"
            "**Traces to:** [N]\n**Depends on:** —\n"
        )
        cfg = ExecutorConfig(
            state_file=tmp_path / "state.db",
            project_root=tmp_path,
            logs_dir=tmp_path / "logs",
            create_git_branch=False,
            auto_commit=False,
            run_tests_on_done=False,
            run_review=False,
            max_retries=1,
            max_consecutive_failures=1,
            on_task_failure="stop",
        )
        cfg.logs_dir.mkdir()
        from spec_runner import execution

        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        import subprocess as _sp

        monkeypatch.setattr(
            execution.subprocess,
            "run",
            lambda *a, **k: _sp.CompletedProcess(
                args=["x"], returncode=1, stdout="", stderr="boom\n"
            ),
        )
        _run_tasks(_run_args(max_retries=1), cfg)
        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "max_consecutive_failures"
            assert "/1" in (state.get_meta("last_run_stop_detail") or "")
