"""#289: `budget authorize` applied half a decision and said nothing.

Found while preparing a real authorization on the published v2.33.1, in the
exact shape that authorization needed — both ceilings raised at once, which is
the documented way (#230: "neither implies the other").

Measured before the fix, in a domain whose two scopes had different latest
authorizations — the ordinary state after any pair of separate raises:

```
$ budget authorize TASK-104 --task-limit 9.00 --run-limit 9.00 --after 1 --reason "…"
[info] Budget authorized  id=3 scope=task new_limit=9.0     ← written
⛔ --after 1 is stale: the standing run authorization is #2   ← refused
$ echo $?
1
```

The task ceiling moved, the run ceiling did not, the command exited 1, and the
refusal named none of it. Worse than losing the write: the operator's *next*
attempt, with the corrected id for the run scope, is then refused on the task
scope as stale against the row this failure had just written.

The CAS exists so that an authorization is made against a state the operator
has **seen**. A half-applied write is the one outcome that guarantees they have
not. Both checks are pure reads, so all of them now run before any write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spec_runner.budget_cmd import AuthorizationError, authorize
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

REASON = "raising both axes for one evidence run"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "spec" / ".executor-state.db",
        "logs_dir": tmp_path / "spec" / ".logs",
        "budget_usd": 5.00,
        "task_budget_usd": 5.00,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _two_scopes_apart(cfg: ExecutorConfig, state: ExecutorState) -> tuple[int, int]:
    """The ordinary state: each scope raised separately, so their latest ids
    differ. Returns `(task_id, run_id)`."""
    task = authorize(cfg, state, reason="task scope first", task_id="TASK-104", task_budget_usd=6.0)
    run = authorize(cfg, state, reason="run scope second", run_budget_usd=7.0)
    return task[0]["id"], run[0]["id"]


class TestOneInvocationOneDecision:
    def test_a_stale_run_id_leaves_the_task_ceiling_alone(self, tmp_path):
        """The measured failure: the task row used to be written before the run
        scope was even looked at."""
        from spec_runner.tdd import resolve_namespace

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            task_id, _run_id = _two_scopes_apart(cfg, state)
            namespace = resolve_namespace(cfg)

            with pytest.raises(AuthorizationError, match="stale"):
                authorize(
                    cfg,
                    state,
                    reason=REASON,
                    task_id="TASK-104",
                    task_budget_usd=9.0,
                    run_budget_usd=9.0,
                    after=task_id,  # correct for the task scope, stale for the run scope
                )

            standing_task = state.latest_budget_authorization(
                "task", task_id="TASK-104", namespace=namespace
            )
            standing_run = state.latest_budget_authorization("run")

        assert standing_task["new_limit_usd"] == 6.0, "the task ceiling must not have moved"
        assert standing_run["new_limit_usd"] == 7.0

    def test_a_stale_task_id_leaves_the_run_ceiling_alone(self, tmp_path):
        """The mirror, so the fix is not just an ordering accident."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _task_id, run_id = _two_scopes_apart(cfg, state)

            with pytest.raises(AuthorizationError, match="stale"):
                authorize(
                    cfg,
                    state,
                    reason=REASON,
                    task_id="TASK-104",
                    task_budget_usd=9.0,
                    run_budget_usd=9.0,
                    after=run_id,  # correct for the run scope, stale for the task scope
                )

            standing_run = state.latest_budget_authorization("run")

        assert standing_run["new_limit_usd"] == 7.0, "the run ceiling must not have moved"

    def test_a_refused_monotonic_check_writes_nothing_either(self, tmp_path):
        """The other refusal that can hit the second scope: lowering. Set up
        with no prior authorizations, so CAS is not in play and the monotonic
        check is what refuses — otherwise this test would pass for the wrong
        reason."""
        from spec_runner.tdd import resolve_namespace

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            with pytest.raises(AuthorizationError, match="only raises"):
                authorize(
                    cfg,
                    state,
                    reason=REASON,
                    task_id="TASK-104",
                    task_budget_usd=9.0,  # a raise
                    run_budget_usd=1.0,  # below the configured $5.00
                )

            standing_task = state.latest_budget_authorization(
                "task", task_id="TASK-104", namespace=resolve_namespace(cfg)
            )

        assert standing_task is None, "nothing at all should have been written"

    def test_when_every_check_passes_both_are_written(self, tmp_path):
        """The fix must not make the working case unreachable."""
        from spec_runner.tdd import resolve_namespace

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            rows = authorize(
                cfg,
                state,
                reason=REASON,
                task_id="TASK-104",
                task_budget_usd=9.0,
                run_budget_usd=9.0,
            )
            standing_task = state.latest_budget_authorization(
                "task", task_id="TASK-104", namespace=resolve_namespace(cfg)
            )
            standing_run = state.latest_budget_authorization("run")

        assert [r["scope"] for r in rows] == ["task", "run"]
        assert standing_task["new_limit_usd"] == 9.0
        assert standing_run["new_limit_usd"] == 9.0

    def test_a_single_axis_is_unaffected(self, tmp_path):
        """The common case stays exactly as it was."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            rows = authorize(cfg, state, reason=REASON, run_budget_usd=6.0)

        assert [r["scope"] for r in rows] == ["run"]
        assert rows[0]["new_limit_usd"] == 6.0


class TestTheRecordedSpendIsStillTheOneSeen:
    def test_each_row_carries_the_spend_at_the_decision(self, tmp_path):
        """Hoisting the reads must not detach `recorded_spend_usd` from what the
        operator was looking at — it is the field that distinguishes "$6 against
        a proven $2.53" from "$6 against a floor" (#230)."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_agent_call("TASK-104", "green", cost_usd=2.50)
            rows = authorize(
                cfg,
                state,
                reason=REASON,
                task_id="TASK-104",
                task_budget_usd=9.0,
                run_budget_usd=9.0,
            )

        by_scope = {r["scope"]: r for r in rows}
        assert by_scope["task"]["recorded_spend_usd"] == pytest.approx(2.50)
        assert by_scope["run"]["recorded_spend_usd"] == pytest.approx(2.50)
        assert by_scope["task"]["previous_limit_usd"] == 5.00
