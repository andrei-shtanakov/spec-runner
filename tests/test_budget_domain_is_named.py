"""#330: every sentence about a ceiling names the state file it came from.

The incident (kapelle, WS-kapelle-47, 2026-08-31). A run under
`--spec-prefix=WS-kapelle-47-` refused at `$2.55 > $1.82`; `budget authorize
--run-limit 20`, typed without the prefix, answered "the run ceiling is already
authorization #1 ($35.95)". Both sentences were true and they were about
different files: the state DB path carries the prefix (`config.py`), so the
authorization went to the default domain while the run read its own. The
operator held two contradictory ceilings with nothing to tell them apart, and
the ambiguity was resolved by reading `config.py`.

Nothing about the split is wrong — a `--budget` bounds one state file's
lifetime spend, and phases run under separate prefixes precisely to account
separately. What was wrong was that no message said which file it meant.

Acceptance, in the issue's words: from the error text the operator understands
that `authorize` needs `--spec-prefix`.
"""

from pathlib import Path

import pytest

from spec_runner.budget import check_before_call, domain_label, sibling_domains
from spec_runner.budget_cmd import AuthorizationError, authorize, cmd_budget
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

TASK = "TASK-101"
REASON = "the pilot continues"
PREFIX = "WS-kapelle-47-"


def _cfg(tmp_path: Path, prefix: str = "", **overrides) -> ExecutorConfig:
    """A project rooted at `tmp_path`, with the state path the prefix implies."""
    stem = f".executor-{prefix}state.db" if prefix else ".executor-state.db"
    defaults: dict = {
        "project_root": tmp_path,
        "spec_prefix": prefix,
        "state_file": tmp_path / "spec" / stem,
        "logs_dir": tmp_path / "spec" / ".logs",
        "budget_usd": 1.82,
        "task_budget_usd": 1.82,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestTheIncident:
    def test_both_halves_name_their_own_file(self, tmp_path, capsys):
        """The two sentences the operator held side by side. They still say
        different numbers — they must, they are different domains — but each
        now says which file its number came from."""
        default = _cfg(tmp_path)
        prefixed = _cfg(tmp_path, PREFIX)

        with ExecutorState(default) as state:
            authorize(default, state, reason=REASON, run_budget_usd=35.95)

        with ExecutorState(prefixed) as state:
            state.record_agent_call(TASK, "green", cost_usd=2.55)
            run_refusal = state.stop_cause()

        with ExecutorState(default) as state, pytest.raises(AuthorizationError) as exc:
            authorize(default, state, reason=REASON, run_budget_usd=20.0)

        assert run_refusal is not None
        assert domain_label(prefixed) in run_refusal[1]
        assert domain_label(default) in str(exc.value)
        assert domain_label(default) != domain_label(prefixed)

    def test_authorizing_without_the_prefix_says_the_prefixed_file_exists(self, tmp_path, capsys):
        """The warning the issue asked for: the divergence is visible *before*
        the paid run, not after it refuses."""
        prefixed = _cfg(tmp_path, PREFIX)
        with ExecutorState(prefixed):
            pass  # the prefixed domain now exists on disk

        default = _cfg(tmp_path)
        cmd_budget(_args(run_limit=20.0), default)
        out = capsys.readouterr().out

        assert "⚠️" in out
        assert domain_label(prefixed) in out
        assert "--spec-prefix" in out


class TestEverySentenceNamesIt:
    def test_the_cas_refusal(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, run_budget_usd=5.0)
            with pytest.raises(AuthorizationError, match="already authorization") as exc:
                authorize(cfg, state, reason=REASON, run_budget_usd=9.0)

        assert domain_label(cfg) in str(exc.value)

    def test_the_monotonic_refusal(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            row = authorize(cfg, state, reason=REASON, run_budget_usd=5.0)[0]
            with pytest.raises(AuthorizationError, match="only raises") as exc:
                authorize(cfg, state, reason=REASON, run_budget_usd=3.0, after=row["id"])

        assert domain_label(cfg) in str(exc.value)

    def test_the_pre_call_refusal_under_a_config_ceiling(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_agent_call(TASK, "green", cost_usd=2.55)
            refusal = check_before_call(cfg, state, TASK, "review")

        assert refusal is not None
        assert domain_label(cfg) in refusal.reason

    def test_the_pre_call_refusal_under_an_authorization(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason=REASON, task_id=TASK, task_budget_usd=2.0)
            state.record_agent_call(TASK, "green", cost_usd=2.55)
            refusal = check_before_call(cfg, state, TASK, "review")

        assert refusal is not None
        assert domain_label(cfg) in refusal.reason

    def test_the_decision_itself(self, tmp_path, capsys):
        """The success line used to say "this state file", which is the one
        phrase that cannot answer "which one?"."""
        cfg = _cfg(tmp_path)
        cmd_budget(_args(run_limit=20.0), cfg)
        out = capsys.readouterr().out

        assert domain_label(cfg) in out
        assert "this state file" not in out


class TestTheWarningIsQuietWhenItShouldBe:
    def test_silent_when_the_operator_named_the_prefix(self, tmp_path, capsys):
        """They have already said which domain they mean."""
        default = _cfg(tmp_path)
        with ExecutorState(default):
            pass

        cmd_budget(_args(run_limit=20.0), _cfg(tmp_path, PREFIX))

        assert "⚠️" not in capsys.readouterr().out

    def test_silent_when_there_is_only_one_domain(self, tmp_path, capsys):
        cmd_budget(_args(run_limit=20.0), _cfg(tmp_path))

        assert "⚠️" not in capsys.readouterr().out

    def test_siblings_exclude_the_active_file_and_its_journals(self, tmp_path):
        """A WAL file is not another domain."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg):
            pass
        (cfg.state_file.parent / ".executor-state.db-wal").write_text("")

        assert sibling_domains(cfg) == []


class TestOneResolver:
    def test_both_commands_spell_the_domain_the_same_way(self, tmp_path):
        """Two spellings would be two things to compare wrongly — the defect
        this fix is about, re-entered from the presentation side."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_agent_call(TASK, "green", cost_usd=2.55)
            run_refusal = state.stop_cause()
            authorize(cfg, state, reason=REASON, run_budget_usd=5.0)
            with pytest.raises(AuthorizationError) as exc:
                authorize(cfg, state, reason=REASON, run_budget_usd=9.0)

        label = domain_label(cfg)
        assert run_refusal is not None
        assert label in run_refusal[1] and label in str(exc.value)
        assert label == "spec/.executor-state.db"


def _args(**overrides):
    import argparse

    defaults: dict = {
        "budget_command": "authorize",
        "reason": REASON,
        "task_id": None,
        "task_limit": None,
        "run_limit": None,
        "actor": "andrei",
        "after": None,
        "reserve": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)
