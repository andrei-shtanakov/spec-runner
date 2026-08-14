"""#255 part 1: a completion over the ceiling said nothing on the retry path.

The reported premise needed one correction, measured before this was built: the
pre-call guard is **not** absent on the success path. It runs at all three paid
sites, and the green's cost — which `record_attempt` writes only after
`post_done_hook` — reaches the review check as `pending_cost`. So $0.92 over a
$5.00 ceiling is the documented guarantee working: *once recorded spend reaches
the limit no new paid call starts; the overshoot is bounded by one call.*

What differed was `retry` versus `run`, not success versus failure. Same
over-ceiling completion, both paths, measured on 2.31.0:

    run --all   [warning] Stopping run … 'total cost $5.92 > budget $5.00'   exit 0
    retry       nothing                                                     exit 0

`run` asks `should_stop()` after every task; `cmd_retry` never asked. The
pilot's completing operation was a retry.

The owner's decision for part 1, and what this file pins: one resolver, both
paths reporting the effective ceiling the same way; a successful task stays
successful; an overshoot after a completed call stops further paid work and is
said out loud; the authorization and its reserve are quoted. **No pre-stage
estimation** — that would be an unproven heuristic wearing the clothes of a
financial guarantee.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from spec_runner.budget_cmd import authorize
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

REASON = "continuing the pilot after the instrument failures were fixed"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "spec" / ".executor-state.db",
        "logs_dir": tmp_path / "spec" / ".logs",
        "budget_usd": 5.00,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _spend(cfg: ExecutorConfig, amount: float, task_id: str = "TASK-101") -> None:
    with ExecutorState(cfg) as state:
        state.record_agent_call(task_id, "green", cost_usd=amount)


class TestTheAnnouncement:
    def test_an_overshoot_is_said_out_loud(self, tmp_path, capsys):
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)

        with ExecutorState(cfg) as state:
            announced = cli._announce_budget_stop(state, cfg)

        out = capsys.readouterr().err
        assert announced is True
        assert "$5.92" in out and "$5.00" in out

    def test_it_says_what_happens_to_the_work_already_done(self, tmp_path, capsys):
        """#219's guarantee, in words: what finished, finished. An operator
        reading a budget stop must not think the task was rolled back."""
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)

        with ExecutorState(cfg) as state:
            cli._announce_budget_stop(state, cfg)

        out = capsys.readouterr().err
        assert "already finished stands" in out
        assert "no further paid work" in out

    def test_it_quotes_the_authorization_and_its_reserve(self, tmp_path, capsys):
        """The same sentence a refusal carries (#230 §7.3): the id to pass to
        `--after`, who set it, and what part of the ceiling is reserved."""
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        _spend(cfg, 9.50)

        with ExecutorState(cfg) as state:
            authorize(
                cfg,
                state,
                reason=REASON,
                run_budget_usd=9.00,
                reserve=("review", 2.00),
                actor="operator@example.com",
            )
            cli._announce_budget_stop(state, cfg)

        out = capsys.readouterr().err
        assert "authorization #1" in out
        assert "operator@example.com" in out
        assert "reserved for review" in out
        assert "--after 1" in out

    def test_it_is_silent_under_the_ceiling(self, tmp_path, capsys):
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        _spend(cfg, 1.00)

        with ExecutorState(cfg) as state:
            announced = cli._announce_budget_stop(state, cfg)

        assert announced is False
        assert capsys.readouterr().err == ""

    def test_it_says_nothing_about_a_different_kind_of_stop(self, tmp_path, capsys):
        """`max_consecutive_failures` is not a budget event, and a budget line
        printed for it would send an operator to `budget authorize` for a
        problem money cannot fix."""
        from spec_runner import cli

        cfg = _cfg(tmp_path, max_consecutive_failures=1)

        with ExecutorState(cfg) as state:
            state.consecutive_failures = 1
            announced = cli._announce_budget_stop(state, cfg)

        assert announced is False
        assert capsys.readouterr().err == ""


class TestBothPathsRead:
    """The defect: `run` asked, `retry` did not."""

    def _tasks(self, cfg: ExecutorConfig) -> None:
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text(
            "### TASK-101: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )

    def _args(self, **kw):
        defaults = {
            "task": "TASK-101",
            "all": False,
            "dry_run": False,
            "milestone": None,
            "no_reset_failed": False,
            "hitl_review": False,
            "tui": False,
            "force": False,
            "allow_dirty_spec": True,
            "json_result": False,
        }
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def _stub(self, monkeypatch, cost: float):
        """A task that completes and spends — no agent, no money."""
        from spec_runner import cli

        def _execute(task, config, state, *a, **k):
            state.record_agent_call(task.id, "green", cost_usd=cost)
            return True

        # Both entry points: `retry` calls `execute_task`, the run loops call
        # `run_with_retries`. Patching one and asserting on the other is how a
        # test proves nothing (learned here — the run case passed silently
        # because no money was ever spent).
        monkeypatch.setattr(cli, "execute_task", _execute)
        monkeypatch.setattr(cli, "run_with_retries", _execute)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_untracked_state", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_spec_governance", lambda *a, **k: None)
        monkeypatch.setattr(cli, "update_task_status", lambda *a, **k: True)
        monkeypatch.setattr(cli, "mark_all_checklist_done", lambda *a, **k: True)

    def test_retry_now_reports_it(self, tmp_path, capsys, monkeypatch):
        """The pilot's own path, which said nothing at all."""
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        self._tasks(cfg)
        _spend(cfg, 3.16)
        self._stub(monkeypatch, 2.76)

        cli.cmd_retry(argparse.Namespace(task_id="TASK-101", fresh=False), cfg)

        out = capsys.readouterr().err
        assert "Budget:" in out
        assert "$5.92" in out

    def test_run_reports_the_same_thing(self, tmp_path, capsys, monkeypatch):
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        self._tasks(cfg)
        _spend(cfg, 3.16)
        self._stub(monkeypatch, 2.76)

        with pytest.raises(SystemExit):
            cli.cmd_run(self._args(), cfg)

        out = capsys.readouterr().err
        assert "Budget:" in out
        assert "$5.92" in out

    def test_the_retried_task_is_still_done(self, tmp_path, monkeypatch):
        """#219, unmoved: reporting an overshoot must not retract a task that
        finished. The announcement is a statement, not a verdict."""
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        self._tasks(cfg)
        _spend(cfg, 3.16)
        recorded: list[tuple] = []

        def _execute(task, config, state, *a, **k):
            state.record_agent_call(task.id, "green", cost_usd=2.76)
            return True

        # Both entry points: `retry` calls `execute_task`, the run loops call
        # `run_with_retries`. Patching one and asserting on the other is how a
        # test proves nothing (learned here — the run case passed silently
        # because no money was ever spent).
        monkeypatch.setattr(cli, "execute_task", _execute)
        monkeypatch.setattr(cli, "run_with_retries", _execute)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_untracked_state", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_spec_governance", lambda *a, **k: None)
        monkeypatch.setattr(cli, "mark_all_checklist_done", lambda *a, **k: True)
        monkeypatch.setattr(
            cli,
            "update_task_status",
            lambda _file, task_id, status: recorded.append((task_id, status)) or True,
        )

        cli.cmd_retry(argparse.Namespace(task_id="TASK-101", fresh=False), cfg)

        assert ("TASK-101", "done") in recorded
        assert ("TASK-101", "blocked") not in recorded

    def test_json_result_stdout_stays_parseable(self, tmp_path, capsys, monkeypatch):
        """The announcement is an operator sentence, and `run --json-result`
        stdout is a **pinned interop surface** — Maestro parses the whole
        stream (`docs/state-schema.md` §3). Measured before this was written:
        that stdout is pure JSON today, every human line already going to
        stderr. A friendly print there would have broken a contract (Copilot,
        PR #279).

        Pinned as the invariant rather than as "this one function uses
        stderr", so the next line someone adds is caught too.
        """
        import json

        from spec_runner import cli

        cfg = _cfg(tmp_path)
        self._tasks(cfg)
        _spend(cfg, 3.16)
        self._stub(monkeypatch, 2.76)

        with pytest.raises(SystemExit):
            cli.cmd_run(self._args(json_result=True), cfg)

        captured = capsys.readouterr()
        assert "Budget:" in captured.err, "the operator still hears about it"
        payload = json.loads(captured.out)
        assert payload["task_id"] == "TASK-101"

    def test_the_next_run_is_still_refused_before_it_starts(self, tmp_path, capsys, monkeypatch):
        """The other half of "further paid work stops": the announcement is
        after the fact, and the run that follows must not begin at all."""
        from spec_runner import cli

        cfg = _cfg(tmp_path)
        self._tasks(cfg)
        _spend(cfg, 5.92)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_untracked_state", lambda *a, **k: None)

        with pytest.raises(SystemExit):
            cli.cmd_run(self._args(), cfg)

        out = capsys.readouterr().out
        assert "Refusing to run" in out
        assert "budget_exceeded" in out
