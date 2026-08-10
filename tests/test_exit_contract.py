"""Exit-contract tests: a run that did not finish must not look finished.

One class of defect, six entry points (issues #127/#129/#130/#131/#132/#134/#136).
The shared failure mode: the process exits 0 — the code Maestro and CI read as
"workstream done" — while work is blocked, refused, or never executed. Caught in
production: a workstream closed DONE at 1/11 tasks and was merged (#136).

Each test pins the *observable* contract (exit code + stop reason), not the
internal control flow that produces it.
"""

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runner.cli import _run_tasks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

TASK_TMPL = """### {tid}: {name}
{emoji} {prio} | {status_emoji} {status} | Est: 0.1d

**Description:** {name}

**Checklist:**
- [ ] work

**Traces to:** [REQ-0]
**Depends on:** {deps}
"""

_STATUS_EMOJI = {
    "TODO": "⬜",
    "IN_PROGRESS": "🔄",
    "DONE": "✅",
    "BLOCKED": "⏸️",
}


def _task_block(tid: str, name: str, status: str = "TODO", deps: str = "—") -> str:
    return TASK_TMPL.format(
        tid=tid,
        name=name,
        emoji="🔴",
        prio="P0",
        status_emoji=_STATUS_EMOJI[status],
        status=status,
        deps=deps,
    )


def _write_tasks(tmp_path: Path, *blocks: str) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    p = spec / "tasks.md"
    p.write_text("# Spec\n\n## M0\n\n" + "\n".join(blocks))
    return p


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


def _stop_reason(cfg: ExecutorConfig) -> str:
    with ExecutorState(cfg) as state:
        return state.get_meta("last_run_stop_reason") or ""


# --------------------------------------------------------------------------
# #136 — on_task_failure: stop must actually stop, with a non-zero exit
# --------------------------------------------------------------------------


class TestOnTaskFailureStop:
    """`on_task_failure: stop` is the documented remedy for orchestrator-managed
    runs (release notes 2.22.0). It marked the task blocked and returned False,
    but leaving the loop depended on `should_stop()` — "consecutive failures >=
    max_consecutive_failures OR budget". A single failed task never tripped it,
    so the next iteration found no ready tasks and broke out as `completed`/0.
    """

    def _failing_run(self, tmp_path, **cfg_overrides):
        tasks_file = _write_tasks(
            tmp_path,
            _task_block("TASK-001", "root"),
            _task_block("TASK-002", "dependent", deps="[TASK-001]"),
        )
        cfg = _cfg(tmp_path, on_task_failure="stop", **cfg_overrides)

        def _fail(task, config, state):
            # Mirror what run_with_retries does on the "stop" path.
            from spec_runner.task import update_task_status

            update_task_status(tasks_file, task.id, "blocked")
            state.record_attempt(task.id, False, 1.0, error="boom")
            return False

        return tasks_file, cfg, _fail

    def test_stop_exits_non_zero(self, tmp_path):
        _, cfg, fail = self._failing_run(tmp_path)
        with (
            patch("spec_runner.cli.run_with_retries", side_effect=fail),
            pytest.raises(SystemExit) as exc,
        ):
            _run_tasks(_run_args(), cfg)
        assert exc.value.code != 0, (
            "on_task_failure=stop exited 0 with 1 of 2 tasks done — "
            "this is the production false-DONE (#136)"
        )

    def test_stop_records_honest_stop_reason(self, tmp_path):
        _, cfg, fail = self._failing_run(tmp_path)
        with (
            patch("spec_runner.cli.run_with_retries", side_effect=fail),
            pytest.raises(SystemExit),
        ):
            _run_tasks(_run_args(), cfg)
        assert _stop_reason(cfg) == "task_failed_stop"

    def test_stop_does_not_execute_further_tasks(self, tmp_path):
        """The dependent task must never be attempted after the stop."""
        tasks_file = _write_tasks(
            tmp_path,
            _task_block("TASK-001", "root"),
            _task_block("TASK-002", "independent"),
        )
        cfg = _cfg(tmp_path, on_task_failure="stop")
        attempted: list[str] = []

        def _fail(task, config, state):
            from spec_runner.task import update_task_status

            attempted.append(task.id)
            update_task_status(tasks_file, task.id, "blocked")
            state.record_attempt(task.id, False, 1.0, error="boom")
            return False

        with (
            patch("spec_runner.cli.run_with_retries", side_effect=_fail),
            pytest.raises(SystemExit),
        ):
            _run_tasks(_run_args(), cfg)
        assert attempted == ["TASK-001"], (
            f"stop continued into {attempted[1:]} — 'stop' must mean stop"
        )

    def test_skip_mode_still_continues(self, tmp_path):
        """Guard against over-correction: default `skip` keeps draining the queue."""
        tasks_file = _write_tasks(
            tmp_path,
            _task_block("TASK-001", "root"),
            _task_block("TASK-002", "independent"),
        )
        cfg = _cfg(tmp_path, on_task_failure="skip")
        attempted: list[str] = []

        def _skip(task, config, state):
            from spec_runner.task import update_task_status

            attempted.append(task.id)
            update_task_status(tasks_file, task.id, "blocked")
            state.record_attempt(task.id, False, 1.0, error="boom")
            return "SKIP"

        with (
            patch("spec_runner.cli.run_with_retries", side_effect=_skip),
            pytest.raises(SystemExit),
        ):
            _run_tasks(_run_args(), cfg)
        assert attempted == ["TASK-001", "TASK-002"]


# --------------------------------------------------------------------------
# #136 (2) / #131 — leftover blocked work must not read as a clean finish
# --------------------------------------------------------------------------


class TestBlockedRemainderExit:
    """ "No more ready tasks" while blocked/failed work remains is not `completed`.

    Before: the `elif nonterminal_tasks` branch only fired once *nothing* was
    todo, so one blocked task plus N waiting TODOs fell into the plain
    "No more ready tasks" path — stop_reason `completed`, exit 0 (#136 item 2).
    """

    def test_blocked_task_with_waiting_todos_exits_non_zero(self, tmp_path):
        """The production shape: root task gives up, dependents wait forever.

        Default `on_task_failure: skip` — TASK-001 exhausts its retries and is
        marked blocked, TASK-002 can never become ready.
        """
        tasks_file = _write_tasks(
            tmp_path,
            _task_block("TASK-001", "root"),
            _task_block("TASK-002", "dependent", deps="[TASK-001]"),
        )
        cfg = _cfg(tmp_path, on_task_failure="skip")

        def _skip(task, config, state):
            from spec_runner.task import update_task_status

            update_task_status(tasks_file, task.id, "blocked")
            state.record_attempt(task.id, False, 1.0, error="boom")
            return "SKIP"

        with (
            patch("spec_runner.cli.run_with_retries", side_effect=_skip),
            pytest.raises(SystemExit) as exc,
        ):
            _run_tasks(_run_args(), cfg)
        assert exc.value.code != 0, "1 of 2 tasks done, dependent stuck — exited 0"
        assert _stop_reason(cfg) == "dependency_blocked_after_skip"

    def test_nothing_ready_closes_the_audit_pair(self, tmp_path):
        """The "no tasks ready" early return recorded EVENT_RUN_STARTED and
        then returned, leaving a dangling start in the audit trail (Copilot,
        PR #144). It stays exit 0 — see the comment at that branch — but the
        trail must not be left half-open.
        """
        import json as _json

        _write_tasks(tmp_path, _task_block("TASK-001", "root", status="DONE"))
        trail = tmp_path / "audit.jsonl"
        cfg = _cfg(tmp_path, audit_log_path=str(trail))

        _run_tasks(_run_args(), cfg)  # must not raise

        events = [_json.loads(line)["event"] for line in trail.read_text().splitlines() if line]
        assert events.count("run_started") == events.count("run_ended") == 1, (
            f"audit pair not closed: {events}"
        )

    def test_all_done_still_exits_zero(self, tmp_path):
        """The clean finish must stay clean — no false alarms."""
        _write_tasks(
            tmp_path,
            _task_block("TASK-001", "root", status="DONE"),
            _task_block("TASK-002", "second", status="DONE"),
        )
        cfg = _cfg(tmp_path)
        _run_tasks(_run_args(), cfg)  # must not raise SystemExit
        assert _stop_reason(cfg) == "completed"


# --------------------------------------------------------------------------
# #132 — orphaned-success warning must not depend on unfinished work existing
# --------------------------------------------------------------------------


class TestOrphanedSuccessWarning:
    def test_warning_fires_when_everything_is_done(self, tmp_path, capsys):
        """success row in the state DB for an ID no longer in tasks.md.

        The warning used to live inside `if nonterminal_tasks`, so the
        "all done + orphaned row" case passed silently.
        """
        _write_tasks(tmp_path, _task_block("TASK-001", "root", status="DONE"))
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-999", True, 1.0)
            state._save()

        _run_tasks(_run_args(), cfg)

        assert "TASK-999" in capsys.readouterr().err, (
            "orphaned success row went unreported on an all-done run (#132)"
        )


# --------------------------------------------------------------------------
# #134 (1) — governance refusal is a refusal, not "nothing to do"
# --------------------------------------------------------------------------


class TestGovernanceRefusalExit:
    """`run --strict` on an unapproved tasks.md printed to stdout and returned,
    so CI could not tell a policy rejection from an empty queue (steward's live
    V1 run, #134 item 1). Diagnostics also belong on stderr: stdout carries
    `--json-result`.
    """

    def _draft_cfg(self, tmp_path):
        from spec_runner.spec import LITE, SpecMeta, write_spec

        cfg = _cfg(tmp_path, spec_governance="strict")
        write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "# Tasks\n")
        cfg.resolve_spec_profile = lambda: LITE  # type: ignore[method-assign]
        return cfg

    @pytest.mark.parametrize("command", ["run", "watch", "retry"])
    def test_refusal_exits_non_zero(self, tmp_path, command, capsys):
        from spec_runner import cli

        cfg = self._draft_cfg(tmp_path)
        fn = {"run": cli.cmd_run, "watch": cli.cmd_watch, "retry": cli.cmd_retry}[command]
        args = _run_args(command=command, task_id="TASK-001", allow_dirty_spec=True)

        with pytest.raises(SystemExit) as exc:
            fn(args, cfg)
        assert exc.value.code != 0, f"{command} refused the run but exited 0"

        captured = capsys.readouterr()
        assert "governance" in captured.err.lower()
        assert captured.out == "", "refusal diagnostics must not pollute stdout"


# --------------------------------------------------------------------------
# #130 — the heaviest stop must still notify
# --------------------------------------------------------------------------


class TestNotifyOnStateSpecMismatch:
    def test_mismatch_exit_sends_run_complete(self, tmp_path):
        """state_spec_mismatch ended the run before notify_run_complete, so the
        owners of the Telegram/webhook channel never heard about the worst stop.
        """
        from spec_runner.cli import _exit_on_state_spec_mismatch

        cfg = _cfg(tmp_path)
        with (
            ExecutorState(cfg) as state,
            patch("spec_runner.notifications.notify_run_complete") as mock_notify,
            pytest.raises(SystemExit),
        ):
            _exit_on_state_spec_mismatch(
                state,
                config=cfg,
                detail="TASK-001: state-DB=success but tasks.md=todo",
                completed=1,
                failed=0,
                remaining=2,
            )
        assert mock_notify.called, "no run_complete notification on state_spec_mismatch (#130)"
        assert mock_notify.call_args.kwargs["stop_reason"] == "state_spec_mismatch"


# --------------------------------------------------------------------------
# #129 — a fail-closed gate inside the TUI thread must reach the process exit
# --------------------------------------------------------------------------


class TestTuiExitPropagation:
    def test_thread_sys_exit_becomes_process_exit(self, tmp_path):
        """`cmd_run --tui` runs `_run_tasks` in a daemon thread; `sys.exit(1)`
        there dies with the thread, so every fail-closed gate exited 0 in TUI
        mode. The code must survive the thread boundary.
        """
        import spec_runner.cli as cli

        cfg = _cfg(tmp_path)
        _write_tasks(tmp_path, _task_block("TASK-001", "root"))

        class _FakeApp:
            def __init__(self, config):
                self._later = None

            def call_later(self, fn):
                self._later = fn

            def run(self):
                assert self._later is not None
                self._later()

        def _boom(args, config, lock_held=False):
            raise SystemExit(1)

        with (
            patch("spec_runner.tui.SpecRunnerApp", _FakeApp, create=True),
            patch.object(cli, "_run_tasks", _boom),
            patch("spec_runner.cli._acquire_run_lock", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_run(_run_args(tui=True, force=True), cfg)
        assert exc.value.code == 1, "fail-closed gate exited 0 under --tui (#129)"


# --------------------------------------------------------------------------
# #127 — budget exhaustion must not crash on an unknown tasks.md status
# --------------------------------------------------------------------------


class TestBudgetPathStatusWrite:
    def test_budget_failure_does_not_raise(self, tmp_path):
        """`_fail_for_budget` wrote status "failed", which is not in
        STATUS_EMOJI (todo/in_progress/review/done/blocked) — KeyError at
        task.py:278. Never exercised by a test, so it survived.
        """
        from spec_runner.execution import _fail_for_budget
        from spec_runner.task import get_task_by_id, parse_tasks

        tasks_file = _write_tasks(tmp_path, _task_block("TASK-001", "root"))
        cfg = _cfg(tmp_path, max_retries=1)
        assert cfg.tasks_file == tasks_file
        task = get_task_by_id(parse_tasks(tasks_file), "TASK-001")
        assert task is not None

        with ExecutorState(cfg) as state:
            _fail_for_budget(task, cfg, state, "Budget exceeded")
            assert state.get_task_state("TASK-001").status == "failed"

        reread = get_task_by_id(parse_tasks(tasks_file), "TASK-001")
        assert reread is not None
        assert reread.status == "blocked", (
            "tasks.md has no 'failed' status; the terminal outcome is recorded "
            "in the state DB (error_code=BUDGET_EXCEEDED)"
        )

    def test_unknown_status_is_fail_closed_not_crash(self, tmp_path):
        """Any future caller passing a status outside the vocabulary gets a
        False (which callers already treat as a write failure), not a KeyError.
        """
        from spec_runner.task import update_task_status

        tasks_file = _write_tasks(tmp_path, _task_block("TASK-001", "root"))
        assert update_task_status(tasks_file, "TASK-001", "nonsense") is False


# --------------------------------------------------------------------------
# Regression guard for the reason vocabulary consumers key off
# --------------------------------------------------------------------------


def test_stop_reasons_are_stable_strings():
    """`status` and Maestro read these; renaming one silently is a contract break."""
    from spec_runner.cli import RUN_STOP_REASONS

    assert {"completed", "task_failed_stop", "dependency_blocked_after_skip"} <= set(
        RUN_STOP_REASONS
    )


def test_simplenamespace_config_still_accepted():
    """`spec_run_gate_ok` is called with duck-typed configs in tests/tools."""
    from spec_runner.cli import spec_run_gate_ok

    ok, _ = spec_run_gate_ok(SimpleNamespace(spec_governance="off"))
    assert ok
