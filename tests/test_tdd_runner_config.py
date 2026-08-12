"""#198 build order §2: `tdd_runner`, and the mismatch that must block.

The owner's rule, and the reason it is a refusal rather than a logged note:

> The declaration chooses the semantics; it cannot prove the command can carry
> them. Otherwise a typo makes one runner's exit codes be read as another's —
> #198 returning through an explicit config key.

So `tdd_runner: pytest` with `mix test` is a `ConfigError` at load and an error
in `validate`, not a warning and not "the declaration wins".

`tdd_runner` also joins `gates.POLICY_KEYS`: changing the adapter changes what
"confirmed" meant, so an earlier verdict must not be inherited across it.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.validate import validate_config


def _cfg(**overrides) -> ExecutorConfig:
    defaults: dict = {"test_command": "pytest"}
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestResolvingTheRunner:
    def test_absent_falls_back_to_inference(self):
        assert _cfg(test_command="uv run pytest").resolve_tdd_runner() == "pytest"

    def test_absent_and_unrecognised_is_no_runner(self):
        """Not an error: it is the ordinary state of a project that has not
        opted into TDD mode. The refusal happens at the replay, which is where
        it can name the selector too."""
        assert _cfg(test_command="mix test").resolve_tdd_runner() is None

    def test_a_declared_runner_is_used(self):
        assert _cfg(tdd_runner="pytest", test_command="pytest -x").resolve_tdd_runner() == "pytest"

    def test_an_unknown_name_is_refused_and_lists_what_exists(self):
        with pytest.raises(ConfigError) as exc:
            _cfg(tdd_runner="pytset").resolve_tdd_runner()
        assert "pytset" in str(exc.value) and "pytest" in str(exc.value)

    def test_a_runner_the_command_cannot_carry_is_refused(self):
        """The heart of it. A typo here would read ExUnit's exit codes as
        pytest's, which is exactly how a test that never ran became a red."""
        with pytest.raises(ConfigError) as exc:
            _cfg(tdd_runner="pytest", test_command="mix test").resolve_tdd_runner()
        assert "mix test" in str(exc.value)

    def test_the_declaration_does_not_win_over_the_command(self):
        """Explicitly pinned, because the first draft of the design said the
        opposite and the owner overruled it."""
        with pytest.raises(ConfigError):
            _cfg(tdd_runner="pytest", test_command="go test ./...").resolve_tdd_runner()


class TestValidateSaysItToo:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "spec-runner.config.yaml"
        path.write_text(body)
        return path

    def test_an_unknown_runner_is_an_error(self, tmp_path):
        result = validate_config(self._write(tmp_path, "tdd_runner: exunit\n"))
        assert any("tdd_runner" in e for e in result.errors), result.errors

    def test_a_mismatch_is_an_error_not_a_warning(self, tmp_path):
        body = "tdd_runner: pytest\ncommands:\n  test: mix test\n"
        result = validate_config(self._write(tmp_path, body))
        assert any("tdd_runner" in e for e in result.errors), result.errors
        assert not [w for w in result.warnings if "tdd_runner" in w]

    def test_a_matching_pair_passes(self, tmp_path):
        body = "tdd_runner: pytest\ncommands:\n  test: uv run pytest\n"
        assert validate_config(self._write(tmp_path, body)).ok

    @pytest.mark.parametrize("value", ['""', "0"])
    def test_a_present_but_empty_command_is_still_checked(self, tmp_path, value):
        """Raised in review: skipping every falsy value let `validate` pass a
        config that `run` then refuses at startup — the same file judged
        differently by two surfaces, which is worse than either verdict."""
        body = f"tdd_runner: pytest\ncommands:\n  test: {value}\n"
        result = validate_config(self._write(tmp_path, body))
        assert any("tdd_runner" in e for e in result.errors), result.errors

    def test_validate_and_startup_agree(self, tmp_path):
        """Pinning the agreement itself, not just each side of it."""
        from spec_runner.config import ExecutorConfig

        body = 'tdd_runner: pytest\ncommands:\n  test: ""\n'
        assert not validate_config(self._write(tmp_path, body)).ok
        with pytest.raises(ConfigError):
            ExecutorConfig(tdd_runner="pytest", test_command="").resolve_tdd_runner()

    def test_no_command_is_not_an_error(self, tmp_path):
        """A config that names a runner and no test command is incomplete, not
        contradictory — `preflight` is where missing commands are reported."""
        assert validate_config(self._write(tmp_path, "tdd_runner: pytest\n")).ok


class TestItIsAPolicyKey:
    def test_changing_the_runner_changes_the_config_hash(self, tmp_path):
        """A verdict is bound to `(sha, config_hash)`. Changing which adapter
        judged the replay changes what "confirmed" meant, so the old verdict
        must not be inherited."""
        from spec_runner.gates import POLICY_KEYS, GateContext

        assert "tdd_runner" in POLICY_KEYS

        def _hash(runner: str) -> str:
            cfg = ExecutorConfig(
                project_root=tmp_path,
                state_file=tmp_path / ".s.db",
                logs_dir=tmp_path / ".l",
                tdd_runner=runner,
            )
            return GateContext(
                task_id="TASK-001", checkpoint_sha="abc", config=cfg, state=None
            ).config_hash

        assert _hash("pytest") != _hash("")


@pytest.mark.slow
class TestTheCliRefusesAtStartup:
    """A handler test is not a surface test — the lesson of #185."""

    def test_a_contradictory_config_stops_the_command(self, tmp_path):
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")
        (tmp_path / "spec-runner.config.yaml").write_text(
            "tdd_runner: pytest\ncommands:\n  test: mix test\n"
        )
        proc = subprocess.run(
            ["python", "-m", "spec_runner.cli", "status"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, combined
        assert "Traceback" not in combined, combined
        assert "tdd_runner" in combined, combined
