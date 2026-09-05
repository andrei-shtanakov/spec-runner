"""Execution summary reports THIS run's numbers (#104, battle-testing F-24).

`total_completed`/`total_failed` are cumulative monotonic counters in
executor_meta; a single-task run used to end with `completed=2` because
earlier runs' completions leaked into the summary (run d4d33ad0).
"""

import argparse
import subprocess as _sp
from pathlib import Path
from unittest.mock import patch

from spec_runner.cli import _run_tasks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

TASKS_MD = (
    "# Spec\n\n## M0\n\n### TASK-001: Done earlier\n"
    "🟢 P0 | ✅ DONE | Est: 0.1d\n\n"
    "**Description:** prior run\n\n**Checklist:**\n- [x] done\n\n"
    "**Traces to:** [REQ-0]\n**Depends on:** —\n\n"
    "### TASK-002: Fresh task\n"
    "🔴 P0 | ⬜ TODO | Est: 0.5d\n\n"
    "**Description:** x\n\n**Checklist:**\n- [ ] do it\n\n"
    "**Traces to:** [REQ-0]\n**Depends on:** —\n"
)


def _cfg(tmp_path: Path) -> ExecutorConfig:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "tasks.md").write_text(TASKS_MD)
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
    )
    cfg.logs_dir.mkdir(exist_ok=True)
    return cfg


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
        "max_retries": 1,
        "timeout": None,
        "no_tests": False,
        "no_branch": False,
        "no_commit": False,
        "no_review": False,
        "hitl_review": False,
        "callback_url": "",
        "allow_dirty_spec": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestRunSummaryDelta:
    def test_prior_run_completions_not_counted(self, tmp_path, monkeypatch):
        """The F-24 scenario: one completed task in state history, this run
        completes exactly one task → summary must say completed=1, not 2."""
        cfg = _cfg(tmp_path)
        # Prior run: TASK-001 completed (bumps the cumulative counter to 1)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)
            assert state.total_completed == 1
            state._save()

        from spec_runner import execution

        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(
            execution,
            "_run_agent_process",
            lambda *a, **k: _sp.CompletedProcess(
                args=["x"], returncode=0, stdout="TASK_COMPLETE", stderr=""
            ),
        )

        with patch("spec_runner.notifications.notify_run_complete") as mock_notify:
            _run_tasks(_run_args(), cfg)

        assert mock_notify.call_count == 1
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["completed"] == 1, kwargs
        assert kwargs["failed"] == 0, kwargs
        # The cumulative counter itself keeps growing (2 total) — only the
        # run summary switched to the delta.
        with ExecutorState(cfg) as state:
            assert state.total_completed == 2

    def test_empty_run_reports_zero(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)
            state.record_attempt("TASK-002", success=True, duration=1.0)
            state._save()
        # Both tasks done → nothing ready; summary must report 0, not 2
        (tmp_path / "spec" / "tasks.md").write_text(
            TASKS_MD.replace("🔴 P0 | ⬜ TODO", "🟢 P0 | ✅ DONE")
        )
        with patch("spec_runner.notifications.notify_run_complete") as mock_notify:
            _run_tasks(_run_args(), cfg)
        if mock_notify.call_count:  # summary only printed when the run loop ran
            assert mock_notify.call_args.kwargs["completed"] == 0

    def test_failed_attempts_scoped_to_run(self, tmp_path, monkeypatch):
        """Historic failed attempts must not inflate this run's counter."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            # Prior history: a failed attempt on the already-done task
            state.record_attempt("TASK-001", success=False, duration=1.0, error="old")
            state.record_attempt("TASK-001", success=True, duration=1.0)
            state._save()

        from spec_runner import execution

        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(
            execution,
            "_run_agent_process",
            lambda *a, **k: _sp.CompletedProcess(
                args=["x"], returncode=0, stdout="TASK_COMPLETE", stderr=""
            ),
        )
        with patch("spec_runner.cli.logger") as mock_logger:
            _run_tasks(_run_args(), cfg)
        summary_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "Execution summary"
        ]
        assert summary_calls, mock_logger.info.call_args_list
        kw = summary_calls[0].kwargs
        assert kw["completed"] == 1, kw
        assert kw["failed_attempts"] is None, kw  # 0 this run → suppressed
