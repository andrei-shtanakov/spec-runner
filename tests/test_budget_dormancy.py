"""No cap configured means no cap — asked the same way at every site.

`budget_is_active` is the project's definition of "there is a budget": the
pre-call guard returns on it before touching state, and `stop_cause` asks the
equivalent question of the run axis before it reads an authorization. The
between-attempts task check did not ask it. It called `effective_limits`
unconditionally, and that function answers with a standing authorization
whichever way the config points — so an authorization written during an
experiment kept binding a task after the operator had removed every budget key
from the YAML, which is the documented way to have no budget at all.

The failure is quiet and it is the wrong shape twice over: the ceiling it
enforces is one nobody configured, and the site that enforces it is the only
one still awake. What this file pins is the pair — dormancy where nothing is
configured, and the #230 behaviour untouched where something is.
"""

from pathlib import Path

from spec_runner.budget import check_before_call
from spec_runner.config import ExecutorConfig
from spec_runner.execution import _check_task_budget
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace

TASK = "TASK-101"
REASON = "an experiment, last month"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "spec" / ".executor-state.db",
        "logs_dir": tmp_path / "spec" / ".logs",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _authorize(state: ExecutorState, cfg: ExecutorConfig, scope: str, limit: float) -> None:
    """A row exactly as `budget authorize` writes one, without the CLI's checks
    — the point is a row that outlived the config that justified it."""
    state.record_budget_authorization(
        scope=scope,
        new_limit_usd=limit,
        recorded_spend_usd=0.0,
        unmeasured_calls=0,
        actor="andrei",
        reason=REASON,
        task_id=TASK if scope == "task" else None,
        namespace=resolve_namespace(cfg) if scope == "task" else None,
    )


class TestNothingConfiguredBindsNothing:
    def test_a_leftover_task_authorization_does_not_bind(self, tmp_path):
        """The regression: $0.02 spent against a $0.01 ceiling nobody asked for."""
        cfg = _cfg(tmp_path)
        assert cfg.task_budget_usd is None and cfg.budget_usd is None

        with ExecutorState(cfg) as state:
            _authorize(state, cfg, "task", 0.01)
            state.record_attempt(TASK, True, 1.0, cost_usd=0.02)

            assert _check_task_budget(TASK, cfg, state, 0) is None

    def test_every_site_agrees_there_is_no_budget(self, tmp_path):
        """Dormancy is a property of the run, not of one call site: an operator
        who removed the keys must not find one guard still awake."""
        cfg = _cfg(tmp_path)

        with ExecutorState(cfg) as state:
            _authorize(state, cfg, "task", 0.01)
            _authorize(state, cfg, "run", 0.01)
            state.record_attempt(TASK, True, 1.0, cost_usd=5.0)
            state.record_agent_call(TASK, "review", cost_usd=None)  # unpriced

            assert _check_task_budget(TASK, cfg, state, 1) is None
            assert check_before_call(cfg, state, TASK, "green") is None
            assert state.stop_cause() is None


class TestAConfiguredCapIsUnchanged:
    def test_the_authorization_still_raises_the_ceiling(self, tmp_path):
        """#230, from the other side: where a cap *is* configured, this site
        keeps reading the authorization rather than the YAML."""
        cfg = _cfg(tmp_path, task_budget_usd=1.82, budget_usd=1.82)

        with ExecutorState(cfg) as state:
            state.record_agent_call(TASK, "green", cost_usd=2.53)
            assert _check_task_budget(TASK, cfg, state, 1) is not None

            _authorize(state, cfg, "task", 6.0)
            assert _check_task_budget(TASK, cfg, state, 1) is None

    def test_the_configured_cap_alone_still_binds(self, tmp_path):
        """No authorization anywhere — the plain case must not have been swept
        up by the dormancy question."""
        cfg = _cfg(tmp_path, task_budget_usd=1.0)

        with ExecutorState(cfg) as state:
            state.record_attempt(TASK, True, 1.0, cost_usd=1.5)
            verdict = _check_task_budget(TASK, cfg, state, 0)

        assert verdict is not None
        assert "1.50" in verdict and "1.00" in verdict

    def test_the_retry_cap_is_independent_of_a_budget(self, tmp_path):
        """`max_retry_cost_usd` is its own cap with its own key (LABS-41), and
        `budget_is_active` does not describe it. It must still fire for a
        project that set only that one."""
        cfg = _cfg(tmp_path, max_retry_cost_usd=0.5)
        assert cfg.task_budget_usd is None and cfg.budget_usd is None

        with ExecutorState(cfg) as state:
            state.record_attempt(TASK, False, 1.0, cost_usd=0.1)  # the first attempt
            state.record_attempt(TASK, False, 1.0, cost_usd=0.6)  # a retry
            verdict = _check_task_budget(TASK, cfg, state, 1)

        assert verdict is not None
        assert "Retry budget exceeded" in verdict
