"""#267 (F-39): the review call is structurally the one the budget starves.

Three of four pilot tasks completed unreviewed, every skip the same shape:

- TASK-102: review refused, `$3.44 >= $1.82`.
- TASK-103: review refused, `$13.80 >= $13.00` — the task itself was under its
  own per-task limit.
- TASK-104: the operator raised the ceiling **naming a ~$2 review reserve in
  the authorization's reason**; the confirmation exec pass then cost $3.97 and
  left the review $0.15 short.

Review is the last paid call of an attempt, so whatever the earlier stages
spend, it is the one that meets the ceiling. A reserve written in the reason
field is prose: nothing in the mechanics stopped the exec pass from spending
it. Combined with `review_policy: advisory` the result is quietly corrosive —
the task completes and merges unreviewed, correctly per config, while the
operator who *funded* a review structurally cannot buy one.

The invariant, in the owner's words: **if the operator explicitly funds the
review stage, earlier stages must not be able to spend that funding.**

A reserve therefore rides on the authorization that carries the ceiling it
partitions. Every call that is not the reserved stage's sees `ceiling -
reserve`; the reserved stage's own calls see the whole ceiling.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from spec_runner.budget import check_before_call, effective_limits, is_reserved_for
from spec_runner.budget_cmd import AuthorizationError, authorize, cmd_budget, parse_reserve
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

REASON = "continuing the pilot; ~$2 is meant for the review of TASK-104"


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


def _spend(cfg: ExecutorConfig, amount: float, task_id: str = "TASK-104") -> None:
    with ExecutorState(cfg) as state:
        state.record_agent_call(task_id, "green", cost_usd=amount)


class TestThePilotsCase:
    """Run-scope, so the run ceiling is the one that binds — TASK-103's shape,
    where the task was under its own limit and the run ceiling refused."""

    def test_the_exec_pass_cannot_spend_the_reserve(self, tmp_path):
        """TASK-104's shape: a $7.00 ceiling with $2.00 meant for review. The
        exec pass is refused at $5.00, not at $7.00."""
        cfg = _cfg(tmp_path, task_budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))
            refusal = check_before_call(cfg, state, "TASK-104", "green")

        assert refusal is not None
        assert refusal.kind == "run_budget"

    def test_and_the_review_can(self, tmp_path):
        """The other half, and the whole point: the money set aside is there
        when the stage it was set aside for arrives."""
        cfg = _cfg(tmp_path, task_budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))

            assert check_before_call(cfg, state, "TASK-104", "review") is None

    def test_without_a_reserve_nothing_changes(self, tmp_path):
        """The mechanism is dormant unless an operator asks for it: the same
        numbers, no reserve, and the exec pass proceeds exactly as before."""
        cfg = _cfg(tmp_path, task_budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason="more budget", run_budget_usd=7.00)

            assert check_before_call(cfg, state, "TASK-104", "green") is None

    def test_every_review_role_is_covered(self, tmp_path):
        """Review runs per role as `review:<role>`. A reserve that covered only
        the bare name would be spent by the first role and starve the rest."""
        cfg = _cfg(tmp_path, task_budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))

            assert check_before_call(cfg, state, "TASK-104", "review:security") is None
            assert check_before_call(cfg, state, "TASK-104", "red_authoring") is not None

    def test_the_refusal_explains_the_arithmetic(self, tmp_path):
        """Otherwise the operator sees a ceiling they raised to $7.00 and a
        refusal below it, with nothing saying why."""
        cfg = _cfg(tmp_path, task_budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))
            refusal = check_before_call(cfg, state, "TASK-104", "green")

        assert refusal is not None
        assert "reserved for review" in refusal.reason
        assert "$2.00" in refusal.reason


class TestBothAxes:
    def test_a_task_reserve_binds_the_task_ceiling(self, tmp_path):
        cfg = _cfg(tmp_path, budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(
                cfg,
                state,
                reason=REASON,
                task_id="TASK-104",
                task_budget_usd=7.00,
                reserve=("review", 2.00),
            )

            assert check_before_call(cfg, state, "TASK-104", "green") is not None
            assert check_before_call(cfg, state, "TASK-104", "review") is None

    def test_another_tasks_calls_are_untouched(self, tmp_path):
        """A task-scoped reserve is scoped like the ceiling it rides on."""
        cfg = _cfg(tmp_path, budget_usd=None)
        _spend(cfg, 5.10, task_id="TASK-104")

        with ExecutorState(cfg) as state:
            authorize(
                cfg,
                state,
                reason=REASON,
                task_id="TASK-104",
                task_budget_usd=7.00,
                reserve=("review", 2.00),
            )

            assert check_before_call(cfg, state, "TASK-105", "green") is None


class TestTheResolverStillAnswersOneQuestion:
    def test_without_a_provenance_the_ceiling_is_the_authorised_one(self, tmp_path):
        """#256's invariant survives: readers asking about the run as a whole —
        the preflight, `costs` — get the ceiling as authorised, not one stage's
        view of it."""
        cfg = _cfg(tmp_path)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))
            _task, run_limit = effective_limits(cfg, state, "TASK-104")

        assert run_limit == 7.00

    def test_the_preflight_does_not_stop_a_run_over_a_reserve(self, tmp_path):
        """A reserve partitions a ceiling between stages; it is not a lower
        ceiling. A run under the ceiling must still start."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))

            assert state.stop_cause() is None

    def test_the_stage_view_is_the_ceiling_minus_the_reserve(self, tmp_path):
        cfg = _cfg(tmp_path)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))
            _task, green = effective_limits(cfg, state, "TASK-104", "green")
            _task, review = effective_limits(cfg, state, "TASK-104", "review")

        assert (green, review) == (5.00, 7.00)


class TestWhatIsRefusedAtAuthorizeTime:
    def test_a_reserve_that_leaves_nothing_is_refused(self, tmp_path):
        """A reserve at or above the ceiling refuses every earlier call
        immediately — a wedge, not a reserve."""
        cfg = _cfg(tmp_path)

        with ExecutorState(cfg) as state, pytest.raises(AuthorizationError, match="leaves nothing"):
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 7.00))

    def test_a_non_positive_reserve_is_refused(self, tmp_path):
        cfg = _cfg(tmp_path)

        with (
            ExecutorState(cfg) as state,
            pytest.raises(AuthorizationError, match="reserves nothing"),
        ):
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 0.0))

    @pytest.mark.parametrize("value", ["review", "review=", "=2.00", "review=lots"])
    def test_a_malformed_flag_is_refused(self, value):
        with pytest.raises(AuthorizationError):
            parse_reserve(value)

    def test_the_flag_parses(self):
        assert parse_reserve("review=2.00") == ("review", 2.00)
        assert parse_reserve(None) is None


class TestTheProseWarning:
    """The minimum the report asked for, kept even though the real mechanism
    exists: authorization #8 stated its reserve in the reason field, and
    nothing said that this was decoration."""

    def _args(self, reason: str, **kw):
        defaults = {
            "budget_command": "authorize",
            "task_id": None,
            "task_limit": None,
            "run_limit": 7.00,
            "actor": None,
            "after": None,
            "reserve": None,
            "reason": reason,
        }
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_a_reserve_in_prose_is_called_out(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)

        assert cmd_budget(self._args("raising it, leaving a $2 reserve for review"), cfg) == 0
        out = capsys.readouterr().out

        assert "not enforced" in out
        assert "--reserve" in out

    def test_a_real_reserve_is_not(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)

        assert cmd_budget(self._args(REASON, reserve="review=2.00"), cfg) == 0
        out = capsys.readouterr().out

        assert "not enforced" not in out
        assert "reserved for review: $2.00" in out

    def test_an_ordinary_reason_says_nothing(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)

        assert cmd_budget(self._args("the pilot needs more room"), cfg) == 0

        assert "not enforced" not in capsys.readouterr().out


class TestTheStageMatch:
    @pytest.mark.parametrize(
        ("stage", "provenance", "expected"),
        [
            ("review", "review", True),
            ("review", "review:security", True),
            ("review", "green", False),
            ("review", "red_authoring", False),
            ("review", "reviewer", False),
            ("green", "green", True),
        ],
    )
    def test_the_table(self, stage, provenance, expected):
        assert is_reserved_for(stage, provenance) is expected


class TestHalfAReserveIsRefused:
    """A reserve is a `(stage, amount)` pair or nothing: half of one withholds
    an unnamed amount, or names a stage that withholds nothing.

    A fresh database refuses it with a `CHECK`. A database **upgraded** to this
    version has the columns without the constraint — SQLite cannot add one to
    an existing table with `ALTER TABLE ... ADD COLUMN` (Copilot, PR #271) — so
    the writer refuses it too, which is what makes the invariant the same on
    both. Measured, not assumed: on a migrated database SQLite accepts the
    half-set row happily.
    """

    def _old_schema_db(self, tmp_path: Path, **overrides) -> ExecutorConfig:
        """A state file written before #267: the columns do not exist yet."""
        import sqlite3

        cfg = _cfg(tmp_path, **overrides)
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cfg.state_file)
        conn.execute(
            "CREATE TABLE budget_authorizations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id TEXT NOT NULL, "
            "scope TEXT NOT NULL, task_id TEXT, namespace TEXT, previous_limit_usd REAL, "
            "new_limit_usd REAL NOT NULL, recorded_spend_usd REAL NOT NULL, "
            "unmeasured_calls INTEGER NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, CHECK (scope IN ('task', 'run')))"
        )
        conn.commit()
        conn.close()
        return cfg

    @pytest.mark.parametrize(("stage", "amount"), [("review", None), (None, 2.00)])
    def test_the_writer_refuses_half_of_one(self, tmp_path, stage, amount):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(ValueError, match="both a stage and"):
            state.record_budget_authorization(
                scope="run",
                new_limit_usd=7.00,
                recorded_spend_usd=0.0,
                unmeasured_calls=0,
                actor="o@e.c",
                reason="r",
                reserve_stage=stage,
                reserve_usd=amount,
            )

    def test_it_refuses_on_a_migrated_database_too(self, tmp_path):
        """Where the CHECK is absent, so this is the only thing standing."""
        cfg = self._old_schema_db(tmp_path)

        with ExecutorState(cfg) as state, pytest.raises(ValueError, match="both a stage and"):
            state.record_budget_authorization(
                scope="run",
                new_limit_usd=7.00,
                recorded_spend_usd=0.0,
                unmeasured_calls=0,
                actor="o@e.c",
                reason="r",
                reserve_stage="review",
                reserve_usd=None,
            )

    def test_a_migrated_database_carries_a_whole_reserve(self, tmp_path):
        """The migration itself works: the columns arrive, and a reserve
        written through them is enforced by the guard like any other."""
        cfg = self._old_schema_db(tmp_path, task_budget_usd=None)
        _spend(cfg, 5.10)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=7.00, reserve=("review", 2.00))

            assert check_before_call(cfg, state, "TASK-104", "green") is not None
            assert check_before_call(cfg, state, "TASK-104", "review") is None


class TestOlderDomains:
    def test_authorizations_written_before_this_carry_no_reserve(self, tmp_path):
        """The columns are added by migration and read as NULL, which is what
        those authorizations meant."""
        import sqlite3

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason="older", run_budget_usd=7.00)
        conn = sqlite3.connect(cfg.state_file)
        try:
            row = conn.execute(
                "SELECT reserve_stage, reserve_usd FROM budget_authorizations"
            ).fetchone()
        finally:
            conn.close()

        assert row == (None, None)
        with ExecutorState(cfg) as state:
            assert check_before_call(cfg, state, "TASK-104", "green") is None
