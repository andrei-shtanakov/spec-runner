"""#256: one ceiling, one reader.

`budget authorize` (#243) raised the run ceiling to $9.00, on the record, with
CAS and an actor. The next `run` refused anyway:

    ⛔ Refusing to run: budget_exceeded (total cost $5.92 > budget $1.82)
       Raise budget_usd, or `spec-runner reset` to clear recorded costs.

`$1.82` is the **config file's** number — the one the authorization exists to
supersede. So the ceiling had three readers that disagreed: `retry` honoured
authorizations (via `budget.effective_limits`), `run`'s preflight read raw
config, and the success path enforced nothing at all (#255, separate).

The remediation advice made it worse. "Raise `budget_usd`" points at the
boundary the authorization mechanism replaced with an audited one, and
"`spec-runner reset` to clear recorded costs" tells an operator to **erase the
spend history** in order to fit under a ceiling — the exact opposite of the
config's own words, *enforced rather than trusted*.

What this file pins: the enforcement reads the effective ceiling wherever it is
asked, and what a refusal tells you to do next is honest.
"""

from __future__ import annotations

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
        "budget_usd": 1.82,
        "task_budget_usd": 1.82,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _spend(cfg: ExecutorConfig, amount: float) -> None:
    with ExecutorState(cfg) as state:
        state.record_agent_call("TASK-101", "green", cost_usd=amount)


class TestThePreflightReadsTheEffectiveCeiling:
    def test_it_stops_when_the_configured_ceiling_stands(self, tmp_path):
        """Unchanged for a project that never authorises anything."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)

        with ExecutorState(cfg) as state:
            cause = state.stop_cause()

        assert cause is not None
        assert cause[0] == "budget_exceeded"
        assert "$1.82" in cause[1]

    def test_it_proceeds_after_an_authorization(self, tmp_path):
        """The pilot's exact numbers: $5.92 spent, ceiling raised to $9.00."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=9.00)
            assert state.stop_cause() is None

    def test_it_stops_again_past_the_authorized_ceiling(self, tmp_path):
        """Raising a ceiling is not removing it — and the number it quotes is
        the authorised one, which is how you can tell which reader answered."""
        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=2.00)
            cause = state.stop_cause()

        assert cause is not None
        assert "$2.00" in cause[1]
        assert "$1.82" not in cause[1], "the config value is no longer what is enforced"

    def test_the_readers_agree(self, tmp_path):
        """The defect in one assertion: the preflight and the pre-call guard
        must answer the same question the same way. They did not — `retry` ran
        to completion under an authorization the next `run` refused to honour.
        """
        from spec_runner.budget import check_before_call, effective_limits

        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)

        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=9.00)
            authorize(cfg, state, reason=REASON, task_id="TASK-101", task_budget_usd=9.00)

            _task_limit, run_limit = effective_limits(cfg, state, "TASK-101")
            assert run_limit == 9.00
            assert state.stop_cause() is None
            assert check_before_call(cfg, state, "TASK-101", "green") is None

    def test_max_consecutive_failures_still_outranks_it(self, tmp_path):
        """The other stop reason must keep working — the fix touches which
        number the budget check reads, nothing else."""
        cfg = _cfg(tmp_path, max_consecutive_failures=1)
        with ExecutorState(cfg) as state:
            state.consecutive_failures = 1
            cause = state.stop_cause()

        assert cause is not None
        assert cause[0] == "max_consecutive_failures"


class TestTheRefusalTellsYouSomethingHonest:
    def test_it_no_longer_suggests_erasing_the_history(self, tmp_path, capsys, monkeypatch):
        """`spec-runner reset` clears recorded costs. Suggesting it as the way
        under a ceiling is advice to destroy the evidence the ceiling is
        measured against."""
        import argparse

        from spec_runner import cli

        cfg = _cfg(tmp_path)
        _spend(cfg, 5.92)
        (tmp_path / "spec").mkdir(exist_ok=True)
        cfg.tasks_file.write_text(
            "### TASK-101: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)

        with pytest.raises(SystemExit):
            cli.cmd_run(argparse.Namespace(task=None, all=False, dry_run=False), cfg)

        out = capsys.readouterr().out
        assert "budget_exceeded" in out
        assert "reset" not in out, "never advise erasing the cost history"
        assert "budget authorize" in out, "point at the audited way to raise it"


class TestTheCeilingIsDisplayedAsAuthorised:
    def test_costs_names_the_authorization(self, tmp_path, capsys):
        """#230 §4: an authorised limit is always displayed *as such*. The
        pinned `budget_usd` key keeps its documented meaning (the configured
        value); the line beside it says which ceiling is actually in force."""
        import argparse

        from spec_runner.cli_info import cmd_costs

        cfg = _cfg(tmp_path)
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-101: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        _spend(cfg, 5.92)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=9.00)

        cmd_costs(argparse.Namespace(json=False, sort="id"), cfg)
        out = capsys.readouterr().out

        assert "of $1.82 (configured)" in out
        assert "Ceiling in force:" in out and "$9.00" in out
        assert "authorization #1" in out

    def test_the_pinned_json_key_keeps_its_documented_meaning(self, tmp_path, capsys):
        """`schemas/costs.schema.json` documents `budget_usd` as *configured*.
        Changing what it carries would be a silent semantic change on a surface
        spec-runner-vscode vendors — a scheduled additive key, not a quiet
        redefinition."""
        import argparse
        import json

        from spec_runner.cli_info import cmd_costs

        cfg = _cfg(tmp_path)
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-101: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        _spend(cfg, 5.92)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=9.00)

        cmd_costs(argparse.Namespace(json=True, sort="id"), cfg)
        payload = json.loads(capsys.readouterr().out)

        assert payload["summary"]["budget_usd"] == 1.82

    def test_nothing_is_printed_without_an_authorization(self, tmp_path, capsys):
        import argparse

        from spec_runner.cli_info import cmd_costs

        cfg = _cfg(tmp_path)
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-101: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")

        cmd_costs(argparse.Namespace(json=False, sort="id"), cfg)

        assert "Ceiling in force" not in capsys.readouterr().out
