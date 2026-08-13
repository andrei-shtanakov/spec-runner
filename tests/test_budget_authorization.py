"""#230 part 2: an operator raises a ceiling, and the record says who and why.

Refund and a separate infrastructure budget were rejected at design time; both
stop *"the number bounds the money"* from being true. What ships is the one
candidate that keeps it: a human raises a specific absolute limit, deliberately,
with a reason, and the guard reads that instead of the config value.

The pilot is the acceptance case. A cap of $1.82 refused at a recorded $2.53
with the work finished and green; every direction the issue offered would have
had to answer "how does this task ever finish?", and this one answers it in a
way that leaves the #213 guarantee quotable — the limit still bounds the money,
a human just named a bigger number and signed for it.

What this file pins beyond the happy path:

- **scope**, exactly as signed off: a task ceiling is `(domain, namespace,
  task)`; a run ceiling belongs to the whole domain and carries **no**
  namespace, or several workstreams would each hold an independent "global" cap;
- **the domain is the state DB** — a new state file inherits no authorization,
  which is the mechanism behind the rule the pilot broke by accident;
- **monotonic**, **CAS**, and every refusal quoting the id an operator needs.
"""

import sqlite3
from pathlib import Path

import pytest

from spec_runner.budget import check_before_call, effective_limits
from spec_runner.budget_cmd import AuthorizationError, authorize
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

REASON = "kapelle pilot continues after F-25…F-28 were fixed"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "spec" / ".executor-state.db",
        "logs_dir": tmp_path / "spec" / ".logs",
        "task_budget_usd": 1.82,
        "budget_usd": 1.82,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _spend(cfg: ExecutorConfig, amount: float, task_id: str = "TASK-101") -> None:
    with ExecutorState(cfg) as state:
        state.record_agent_call(task_id, "green", cost_usd=amount)


class TestTheWedgeOpens:
    def test_the_guard_refuses_before_the_authorization(self, tmp_path):
        """The pilot's state: $2.53 recorded against a $1.82 cap."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-101", "review")

        assert refusal is not None
        assert refusal.kind == "task_budget"

    def test_and_proceeds_after_it(self, tmp_path):
        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            authorize(cfg, state, reason=REASON, run_budget_usd=6.0)
            assert check_before_call(cfg, state, "TASK-101", "review") is None

    def test_both_axes_are_needed(self, tmp_path):
        """Raising only the task ceiling leaves `budget_usd` refusing the very
        next call — which is why the command takes both."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            refusal = check_before_call(cfg, state, "TASK-101", "review")

        assert refusal is not None
        assert refusal.kind == "run_budget"

    def test_the_between_attempts_check_honours_it_too(self, tmp_path):
        """Two sites read a limit. An authorization honoured at one and ignored
        at the other would unblock the call and still lose the task."""
        from spec_runner.execution import _check_task_budget

        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)

        with ExecutorState(cfg) as state:
            assert _check_task_budget("TASK-101", cfg, state, 1) is not None
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            assert _check_task_budget("TASK-101", cfg, state, 1) is None


class TestScope:
    def test_a_run_authorization_carries_no_namespace(self, tmp_path):
        """The sign-off's correction, enforced by a CHECK rather than a rule:
        `budget_usd` bounds the whole domain, and a namespaced run ceiling
        would give each workstream its own "global" cap."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=5.0)
            row = state.latest_budget_authorization("run")

        assert row is not None
        assert row["namespace"] is None
        assert row["task_id"] is None

    def test_the_schema_refuses_a_namespaced_run_row(self, tmp_path):
        """Unrepresentable, not merely undone: a future writer cannot make one."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            conn = state._conn
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO budget_authorizations (domain_id, scope, task_id, namespace, "
                    "new_limit_usd, recorded_spend_usd, unmeasured_calls, actor, reason, "
                    "timestamp) VALUES ('d', 'run', NULL, 'ws-1', 5.0, 0.0, 0, 'a', 'r', 't')"
                )

    def test_a_task_authorization_does_not_leak_across_tasks(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            task_limit, _run = effective_limits(cfg, state, "TASK-999")

        assert task_limit == 1.82, "another task keeps the configured ceiling"

    def test_a_task_authorization_does_not_leak_across_namespaces(self, tmp_path):
        cfg = _cfg(tmp_path, tdd_namespace="ws-1")
        other = _cfg(tmp_path, tdd_namespace="ws-2")
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            assert effective_limits(cfg, state, "TASK-101")[0] == 6.0
            assert effective_limits(other, state, "TASK-101")[0] == 1.82


class TestTheDomainIsTheStateFile:
    def test_a_new_state_file_inherits_nothing(self, tmp_path):
        """The rule the pilot broke by accident: three attempts, three state
        files, and a cap blind to $1.19 of earlier spend."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)

        fresh = _cfg(tmp_path, state_file=tmp_path / "spec" / ".executor-state.attempt2.db")
        with ExecutorState(fresh) as state:
            assert effective_limits(fresh, state, "TASK-101")[0] == 1.82

    def test_the_domain_id_is_stable_within_a_file(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            first = state.budget_domain_id()
        with ExecutorState(cfg) as state:
            assert state.budget_domain_id() == first

    def test_two_files_are_two_domains(self, tmp_path):
        a = _cfg(tmp_path)
        b = _cfg(tmp_path, state_file=tmp_path / "spec" / ".other.db")
        with ExecutorState(a) as state:
            first = state.budget_domain_id()
        with ExecutorState(b) as state:
            assert state.budget_domain_id() != first


class TestRefusals:
    def test_a_reason_is_required(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="reason"):
            authorize(cfg, state, reason="  ", task_id="TASK-101", task_budget_usd=6.0)

    def test_naming_no_axis_is_refused(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="nothing to"):
            authorize(cfg, state, reason=REASON)

    def test_a_task_ceiling_needs_a_task(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="needs the task"):
            authorize(cfg, state, reason=REASON, task_budget_usd=6.0)

    def test_lowering_is_refused(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="only raises"):
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=1.0)

    def test_the_same_ceiling_again_is_refused(self, tmp_path):
        """Not idempotent-and-silent: repeating a decision is either a mistake
        or a no-op, and both deserve to be said out loud."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            with pytest.raises(AuthorizationError, match="only raises"):
                authorize(
                    cfg,
                    state,
                    reason=REASON,
                    task_id="TASK-101",
                    task_budget_usd=6.0,
                    after=1,
                )

    def test_a_second_authorization_needs_the_cas(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            with pytest.raises(AuthorizationError, match="pass --after 1"):
                authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=9.0)

    def test_a_stale_cas_names_the_standing_one(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            with pytest.raises(AuthorizationError, match="stale"):
                authorize(
                    cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=9.0, after=99
                )

    def test_an_agent_cannot_raise_its_own_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SPEC_RUNNER_AGENT", "1")
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="operator"):
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)

    def test_it_refuses_while_a_run_holds_the_lock(self, tmp_path):
        """The guard reads limits mid-call; moving them under a live loop makes
        "what was authorised when this call started" unanswerable."""
        from spec_runner.config import ExecutorLock

        cfg = _cfg(tmp_path)
        lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
        assert lock.acquire()
        try:
            with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="running"):
                authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
        finally:
            lock.release()


class TestTheRecord:
    def test_it_captures_what_the_human_was_looking_at(self, tmp_path):
        """`recorded_spend` and `unmeasured_calls` are what make the row honest:
        $6.00 authorised against a proven $2.53 means something different from
        $6.00 against a floor of $2.53 with unpriced calls behind it."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)
        with ExecutorState(cfg) as state:
            state.record_agent_call("TASK-101", "review", cost_usd=None)
            authorize(
                cfg,
                state,
                reason=REASON,
                task_id="TASK-101",
                task_budget_usd=6.0,
                actor="owner@example.com",
            )
            from spec_runner.tdd import resolve_namespace

            row = state.latest_budget_authorization(
                "task", task_id="TASK-101", namespace=resolve_namespace(cfg)
            )

        assert row is not None
        assert row["previous_limit_usd"] == 1.82
        assert row["new_limit_usd"] == 6.0
        assert row["recorded_spend_usd"] == 2.53
        assert row["unmeasured_calls"] == 1
        assert row["actor"] == "owner@example.com"
        assert row["reason"] == REASON

    def test_it_is_append_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=9.0, after=1)
            rows = state._conn.execute(
                "SELECT id, previous_limit_usd, new_limit_usd FROM budget_authorizations "
                "ORDER BY id"
            ).fetchall()

        assert [(r[1], r[2]) for r in rows] == [(1.82, 6.0), (6.0, 9.0)]

    def test_the_newest_wins_over_the_config(self, tmp_path):
        """Not `max()`: an operator who edits the YAML afterwards deserves an
        answer that does not depend on which number is larger."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=6.0)
            raised = _cfg(tmp_path, task_budget_usd=50.0)
            assert effective_limits(raised, state, "TASK-101")[0] == 6.0


class TestTheRefusalTeaches:
    def test_it_quotes_the_authorization_the_operator_must_supersede(self, tmp_path):
        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=2.0)
            refusal = check_before_call(cfg, state, "TASK-101", "review")

        assert refusal is not None
        assert "authorization #1" in refusal.reason
        assert "--after 1" in refusal.reason
        assert "$2.00" in refusal.reason

    def test_without_one_it_says_the_limit_is_the_configured_one(self, tmp_path):
        cfg = _cfg(tmp_path)
        _spend(cfg, 2.53)
        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-101", "review")

        assert refusal is not None
        assert "configured one" in refusal.reason
        assert "budget authorize" in refusal.reason
