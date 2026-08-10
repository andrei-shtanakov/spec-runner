"""Fail-closed state-DB <-> tasks.md reconciliation in the `run --all` loop (#124).

Task 3 of the task-status-integrity plan: after Task 1/2 narrowed how a
mismatch between the state DB and tasks.md can arise, the run loop must
still fail closed (not exit 0) when one occurs — whether from a silent
`update_task_status` no-op, an agent editing tasks.md mid-run, or stale
state-DB rows left over from an earlier run.

Two gates, both funneled through the existing `last_run_stop_reason`
mechanism (never a parallel one):

1. Immediately after each `run_with_retries` call: if the state DB just
   recorded "success" for the task that ran, tasks.md must show it "done".
2. Backstop in the "no more ready tasks" branch: if any task's state-DB
   status is "success" but tasks.md never caught up to "done" — even for a
   task not touched by gate 1 in this run — the run must not report 0.
"""

import argparse
from pathlib import Path

import pytest

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


def _task_block(task_id: str, name: str, depends_on: str = "—") -> str:
    return (
        f"### {task_id}: {name}\n"
        "\U0001f534 P0 | ⬜ TODO | Est: 0.5d\n\n"
        "**Description:** x\n\n**Checklist:**\n- [ ] do it\n\n"
        f"**Traces to:** [NFR-1]\n**Depends on:** {depends_on}\n"
    )


def _dep_ref(task_id: str) -> str:
    return f"[{task_id}]"


class TestGate1ImmediateMismatch:
    """Test 1: a success recorded in the state DB that tasks.md never
    reflects as done must stop the run immediately, non-zero."""

    def test_success_not_written_as_done_stops_run_nonzero(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text("# Spec\n\n## M0\n\n" + _task_block("TASK-001", "Demo"))
        cfg = _cfg(tmp_path)

        import spec_runner.cli as cli_mod

        def _fake_run_with_retries(task, config, state):
            # Simulate the DB recording success while tasks.md is left
            # untouched — the exact shape of the disputatio D3 incident
            # (a silent update_task_status no-op), reproduced directly
            # rather than through the full hook stack.
            state.record_attempt(task.id, success=True, duration=1.0)
            return True

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake_run_with_retries)

        with pytest.raises(SystemExit) as excinfo:
            _run_tasks(_run_args(), cfg)
        assert excinfo.value.code == 1

        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "state_spec_mismatch"
            detail = state.get_meta("last_run_stop_detail") or ""
            assert "TASK-001" in detail

        # The neighboring/target task itself was never mutated by the gate.
        assert "⬜ TODO" in tasks_md.read_text()


class TestGate2Backstop:
    """Test 2: a stale state-DB success for a task tasks.md never marked
    done — for a task the loop never touches this run — must still be
    caught when the loop runs dry with no more ready tasks."""

    def test_stale_success_caught_when_no_ready_tasks_remain(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text(
            "# Spec\n\n## M0\n\n"
            + _task_block("TASK-000", "Blocker")
            + "\n"
            + _task_block("TASK-001", "Dependent", depends_on=_dep_ref("TASK-000"))
        )
        cfg = _cfg(tmp_path)

        # Stale leftover: TASK-001 already recorded "success" in the state
        # DB (e.g. a corrupted prior run) but tasks.md was never updated —
        # and this run's loop never reaches TASK-001 at all, because its
        # sole dependency (TASK-000) is about to fail.
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)

        import spec_runner.cli as cli_mod
        from spec_runner.task import update_task_status

        def _fake_run_with_retries(task, config, state):
            assert task.id == "TASK-000"
            state.record_attempt(task.id, success=False, duration=1.0, error="boom")
            update_task_status(config.tasks_file, task.id, "blocked")
            return "SKIP"

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake_run_with_retries)

        with pytest.raises(SystemExit) as excinfo:
            _run_tasks(_run_args(), cfg)
        assert excinfo.value.code == 1

        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "state_spec_mismatch"
            detail = state.get_meta("last_run_stop_detail") or ""
            assert "TASK-001" in detail


class TestGate2OrphanedSuccessNotFatal:
    """F1 (final review): a state-DB success whose task ID is no longer
    present in tasks.md at all (removed, not merely left non-done) must not
    trip gate 2 — there is nothing in the file to reconcile against. The
    signal isn't dropped silently though: it is surfaced as a warning."""

    def test_orphaned_success_id_does_not_stop_run_but_warns(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        # TASK-999 (recorded success in the state DB) is absent from
        # tasks.md entirely — deleted, e.g. by a spec edit mid-run.
        tasks_md.write_text("# Spec\n\n## M0\n\n" + _task_block("TASK-000", "Blocker"))
        cfg = _cfg(tmp_path)

        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-999", success=True, duration=1.0)

        import spec_runner.cli as cli_mod
        from spec_runner.task import update_task_status

        def _fake_run_with_retries(task, config, state):
            assert task.id == "TASK-000"
            state.record_attempt(task.id, success=False, duration=1.0, error="boom")
            update_task_status(config.tasks_file, task.id, "blocked")
            return "SKIP"

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake_run_with_retries)

        # Capture the warning directly on the module logger — asserting via
        # capsys/stderr is order-dependent on global structlog sink state
        # set up by other test modules in a full-suite run.
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            cli_mod.logger,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        # An orphaned success (ID absent from tasks.md) is not a mismatch to
        # fail closed on — it stays a warning. TASK-000 itself ends up blocked
        # (the fake retries fail it), and since #136 that alone exits 1 under
        # "dependency_blocked_after_skip". The two remain independent: the
        # orphan never escalates to "state_spec_mismatch", which is what this
        # test is about.
        with pytest.raises(SystemExit) as exc:
            _run_tasks(_run_args(), cfg)
        assert exc.value.code == 1

        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "dependency_blocked_after_skip"

        assert any("TASK-999" in kw.get("task_ids", []) for _event, kw in warnings)


class TestLegitimateBlockUnchanged:
    """Test 3: TODO tasks blocked by a genuinely failed dependency (the
    default on_task_failure=skip flow) must not trip the *mismatch* gate —
    state-DB and tasks.md agree (nothing succeeded, nothing is done).

    This is the #136 shape exactly: a blocker gives up, its dependent waits
    forever. Until #136 the run reported `completed`/0 here, and this test
    asserted it — the bug was pinned as the contract. The mismatch gate still
    must not fire (there is nothing inconsistent), but the run is no longer a
    clean finish: reason `dependency_blocked_after_skip`, exit 1."""

    def test_failed_dependency_blocks_without_mismatch(self, tmp_path, monkeypatch):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        tasks_md = spec_dir / "tasks.md"
        tasks_md.write_text(
            "# Spec\n\n## M0\n\n"
            + _task_block("TASK-000", "Blocker")
            + "\n"
            + _task_block("TASK-001", "Dependent", depends_on=_dep_ref("TASK-000"))
        )
        cfg = _cfg(tmp_path)

        import spec_runner.cli as cli_mod
        from spec_runner.task import update_task_status

        def _fake_run_with_retries(task, config, state):
            assert task.id == "TASK-000"
            state.record_attempt(task.id, success=False, duration=1.0, error="boom")
            update_task_status(config.tasks_file, task.id, "blocked")
            return "SKIP"

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake_run_with_retries)

        with pytest.raises(SystemExit) as exc:
            _run_tasks(_run_args(), cfg)
        assert exc.value.code == 1, "blocker failed, dependent stranded — this is not success"

        with ExecutorState(cfg) as state:
            # NOT state_spec_mismatch: nothing is inconsistent, work is stuck.
            assert state.get_meta("last_run_stop_reason") == "dependency_blocked_after_skip"

        text = tasks_md.read_text()
        assert "TASK-000" in text and "BLOCKED" in text
        assert "TASK-001" in text  # still pending, untouched by the gate


class TestAllDoneIsZero:
    """Test 4: when every task legitimately reaches done, the run must
    complete with no exception (exit 0), state and file agreeing."""

    def test_all_tasks_done_completes_cleanly(self, tmp_path, monkeypatch):
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
