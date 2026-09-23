"""#388: the budget from the environment, and where the cap came from.

A cap written into an untracked `spec-runner.config.yaml` goes stale between
phases (the field case: $1.82 left over from an August battle test nearly
starved a ten-task workstream). An environment variable is a property of the
launch, not of the file. Precedence: CLI flag > environment > config file >
default, and the default stays "no cap" (owner's option (b): $30 is a
documented recommendation, not a default that would fail-close every config
without a budget).

The run prints which cap is in force and where it came from — on stderr,
because stdout carries `--json-result`, the Maestro contract.
"""

import argparse

import pytest

from spec_runner.config import ConfigError, build_config

RUN_ENV = "SPEC_RUNNER_BUDGET_USD"
TASK_ENV = "SPEC_RUNNER_TASK_BUDGET_USD"


def _args(**overrides) -> argparse.Namespace:
    values = {"budget": None, "task_budget": None}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(RUN_ENV, raising=False)
    monkeypatch.delenv(TASK_ENV, raising=False)


class TestPrecedence:
    def test_the_environment_overrides_the_config_file(self, monkeypatch):
        monkeypatch.setenv(RUN_ENV, "30")
        monkeypatch.setenv(TASK_ENV, "4.5")
        cfg = build_config(
            {"budget_usd": 1.82, "task_budget_usd": 1.0}, _args(), detect_subdir=False
        )
        assert cfg.budget_usd == 30.0
        assert cfg.task_budget_usd == 4.5
        assert cfg.budget_sources == {"budget_usd": RUN_ENV, "task_budget_usd": TASK_ENV}

    def test_a_cli_flag_overrides_the_environment(self, monkeypatch):
        monkeypatch.setenv(RUN_ENV, "30")
        cfg = build_config({}, _args(budget=7.0), detect_subdir=False)
        assert cfg.budget_usd == 7.0
        assert cfg.budget_sources["budget_usd"] == "--budget"

    def test_the_config_file_is_named_when_nothing_overrides_it(self):
        cfg = build_config({"task_budget_usd": 2.0}, _args(), detect_subdir=False)
        assert cfg.task_budget_usd == 2.0
        assert cfg.budget_sources == {"task_budget_usd": "config"}

    def test_the_default_is_still_no_cap(self):
        cfg = build_config({}, _args(), detect_subdir=False)
        assert cfg.budget_usd is None and cfg.task_budget_usd is None
        assert cfg.budget_sources == {}

    def test_an_empty_variable_is_unset(self, monkeypatch):
        monkeypatch.setenv(RUN_ENV, "  ")
        cfg = build_config({"budget_usd": 5.0}, _args(), detect_subdir=False)
        assert cfg.budget_usd == 5.0
        assert cfg.budget_sources == {"budget_usd": "config"}


class TestRefusals:
    @pytest.mark.parametrize("value", ["thirty", "0", "-5", "nan", "inf"])
    def test_an_unusable_value_is_refused_naming_the_variable(self, monkeypatch, value):
        monkeypatch.setenv(TASK_ENV, value)
        with pytest.raises(ConfigError, match=TASK_ENV):
            build_config({}, _args(), detect_subdir=False)

    @pytest.mark.parametrize("command", ["run", "retry", "watch"])
    def test_a_spending_command_refuses_without_a_traceback(self, monkeypatch, tmp_path, command):
        from spec_runner.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(RUN_ENV, "thirty")
        argv = ["spec-runner", command] + (["TASK-001"] if command == "retry" else [])
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as exc:
            main()
        assert RUN_ENV in str(exc.value)
        assert str(exc.value).startswith("⛔")

    @pytest.mark.parametrize("command", ["status", "stop", "costs"])
    def test_a_command_that_spends_nothing_warns_and_runs(
        self, monkeypatch, tmp_path, capsys, command
    ):
        """Local review: `stop` is the brake on a paid run in another shell;
        a typo in this shell's profile must not disable it, and the refusal's
        own reason ("would run unguarded") applies only to spending."""
        from spec_runner.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(RUN_ENV, "1,82")
        monkeypatch.setattr("sys.argv", ["spec-runner", command])
        try:
            main()
        except SystemExit as exc:
            assert RUN_ENV not in str(exc.code or "")
        assert f"{RUN_ENV}" in capsys.readouterr().err


class TestTheStartupLine:
    def test_it_names_each_cap_and_its_source_on_stderr(self, monkeypatch, capsys):
        from spec_runner.cli import _announce_budget

        monkeypatch.setenv(RUN_ENV, "30")
        cfg = build_config({"task_budget_usd": 2.0}, _args(), detect_subdir=False)
        _announce_budget(cfg)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "$30.00" in captured.err and RUN_ENV in captured.err
        assert "$2.00" in captured.err and "config" in captured.err

    def test_it_says_which_cap_is_absent(self, monkeypatch, capsys):
        from spec_runner.cli import _announce_budget

        monkeypatch.setenv(TASK_ENV, "3")
        cfg = build_config({}, _args(), detect_subdir=False)
        _announce_budget(cfg)
        err = capsys.readouterr().err
        assert "run: no cap" in err and "$3.00" in err

    def test_silent_when_no_cap_is_set(self, capsys):
        from spec_runner.cli import _announce_budget

        _announce_budget(build_config({}, _args(), detect_subdir=False))
        assert capsys.readouterr() == ("", "")

    def test_an_authorized_ceiling_is_the_one_shown_in_force(self, tmp_path, capsys):
        """Local review: the line read the config's $1.82 while enforcement
        and the overshoot announcement used the authorised $9.00 — the
        two-numbers defect #256 removed. One reader: the authorization wins
        and is displayed as one, with the configured value beside it."""
        from spec_runner.budget_cmd import authorize
        from spec_runner.cli import _announce_budget
        from spec_runner.config import ExecutorConfig
        from spec_runner.state import ExecutorState

        cfg = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "spec" / ".executor-state.db",
            logs_dir=tmp_path / "spec" / ".logs",
            budget_usd=1.82,
            budget_sources={"budget_usd": "config"},
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason="raised on the record", run_budget_usd=9.00)
        _announce_budget(cfg)
        err = capsys.readouterr().err
        assert "run: $9.00 in force (authorization #1" in err
        assert "configured $1.82 (config)" in err

    def test_an_authorization_alone_is_announced(self, tmp_path, capsys):
        from spec_runner.budget_cmd import authorize
        from spec_runner.cli import _announce_budget
        from spec_runner.config import ExecutorConfig
        from spec_runner.state import ExecutorState

        cfg = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "spec" / ".executor-state.db",
            logs_dir=tmp_path / "spec" / ".logs",
            budget_usd=1.0,
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        with ExecutorState(cfg) as state:
            authorize(cfg, state, reason="raised", run_budget_usd=5.00)
        cfg.budget_usd = None  # the config cap removed; the authorization stands
        _announce_budget(cfg)
        assert "run: $5.00 in force" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["cmd_run", "cmd_retry", "cmd_watch"])
    def test_every_command_that_spends_announces_it(self, command):
        """The line is printed where each spending command passes its
        pre-start guards — `run`, `retry` and `watch` alike."""
        import inspect

        import spec_runner.cli as cli

        source = inspect.getsource(
            cli._run_tasks_inner if command == "cmd_run" else getattr(cli, command)
        )
        assert "_announce_budget(config)" in source


class TestTheMcpChild:
    def test_a_cap_from_the_environment_reproduces_in_the_child(self, monkeypatch, tmp_path):
        """The MCP launcher proves the child rebuilds the parent's config
        (#485). The cap travels as `--budget`; its *source* is provenance,
        and the child rightly reports its own — not a policy difference."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.mcp_launch import (
            Irreproducible,
            LaunchScope,
            child_argv,
            simulate_child_config,
        )

        monkeypatch.setenv(RUN_ENV, "30")
        parent = build_config({"project_root": tmp_path}, _args(), detect_subdir=False)
        assert parent.budget_sources == {"budget_usd": RUN_ENV}
        result = simulate_child_config(LaunchScope.of(parent), child_argv(parent, "TASK-001"))
        assert not isinstance(result, Irreproducible), result
        assert isinstance(result, ExecutorConfig)
        assert result.budget_usd == 30.0
