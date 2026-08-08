"""Honest stop_reason for `run --all` blocked-after-skip (owner decision, round 2).

Before this fix, the "no more ready tasks" branch in `_run_tasks_inner` only
distinguished "some task is still todo" (blocked_tasks diagnostics) from
"nothing is todo" — and treated the latter as "All tasks completed" even
when a task was left `blocked` (on_task_failure="skip" ran out of retries
and the task never recovered). The run's `last_run_stop_reason` stayed the
default "completed", so a caller (Maestro) reading `status`/the audit trail
had no way to tell a clean finish from a stuck one.

Exit code is intentionally unchanged (still 0) — this is diagnostics only.
A future interop change to make this non-zero is a separate follow-up.
"""

import argparse
from pathlib import Path

from spec_runner.cli import _run_tasks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
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
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
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


def _task_block(task_id: str, name: str) -> str:
    return (
        f"### {task_id}: {name}\n"
        "\U0001f534 P0 | ⬜ TODO | Est: 0.5d\n\n"
        "**Description:** x\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [NFR-1]\n**Depends on:** —\n"
    )


class TestBlockedAfterSkipHonestStopReason:
    """(a) A task left `blocked` with nothing else todo must be reported
    honestly — not as "All tasks completed" — without raising the exit
    code."""

    def test_blocked_task_reports_dependency_blocked_after_skip(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text("# Spec\n\n## M0\n\n" + _task_block("TASK-000", "Solo"))
        cfg = _cfg(tmp_path)

        import spec_runner.cli as cli_mod
        from spec_runner.task import update_task_status

        def _fake_run_with_retries(task, config, state):
            assert task.id == "TASK-000"
            state.record_attempt(task.id, success=False, duration=1.0, error="boom")
            update_task_status(config.tasks_file, task.id, "blocked")
            return "SKIP"

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake_run_with_retries)

        # No SystemExit — exit code stays 0, only the diagnostics change.
        _run_tasks(_run_args(), cfg)

        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "dependency_blocked_after_skip"
            detail = state.get_meta("last_run_stop_detail") or ""
            assert "TASK-000" in detail

        text = tasks_md.read_text()
        assert "BLOCKED" in text


class TestAllDoneStillReportsCompleted:
    """(b) When every task genuinely reaches done, behavior is unchanged:
    stop_reason stays "completed", same as before this fix."""

    def test_all_tasks_done_still_reports_completed(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text("# Spec\n\n## M0\n\n" + _task_block("TASK-001", "Demo"))
        cfg = _cfg(tmp_path)

        import spec_runner.cli as cli_mod
        from spec_runner.task import update_task_status

        def _fake_run_with_retries(task, config, state):
            state.record_attempt(task.id, success=True, duration=1.0)
            update_task_status(config.tasks_file, task.id, "done")
            return True

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake_run_with_retries)

        _run_tasks(_run_args(), cfg)  # must not raise SystemExit

        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "completed"
        assert "✅ DONE" in tasks_md.read_text()
