"""#182 (F-7): a config that mixes the flat and `executor:` shapes is refused.

Found by the battle test of published v2.25.0. One stray `executor:` key made
`load_config_from_yaml` read *only* that section and discard every top-level
key — silently, with `validate` reporting zero errors.

Why that is a safety bug rather than a usability wart: `claude_command` is
among the discarded keys, so the tool falls back to its default and invokes a
paid external model with write access to the working tree. The battle config
named a scripted stand-in agent; the run went to the real `claude` CLI and
spent real tokens. Every safety knob set that way — `skip_permissions`,
`run_review`, `execution_mode`, a sandboxed command — is silently off.

So the refusal is an **error**, at load time and in `validate`. A warning is
exactly wrong here: the failure mode is "your settings did nothing and money
was spent elsewhere", which is the class of thing that scrolls past.

Either shape alone stays valid — the legacy `executor:`-only config is the
documented v1.x layout and must keep working untouched.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from spec_runner.config import ConfigError, load_config_from_yaml
from spec_runner.validate import validate_config

#: The config from the battle report, near enough. Everything outside
#: `executor:` was discarded, including the stand-in agent.
BATTLE_CONFIG = """\
execution_mode: tdd
claude_command: /tmp/agent.sh
command_template: "{cmd} -p {prompt}"
executor:
  create_git_branch: false
"""

LEGACY_CONFIG = """\
executor:
  max_retries: 5
  claude_model: opus
  commands:
    test: pytest -x
"""

FLAT_CONFIG = """\
max_retries: 5
claude_model: opus
commands:
  test: pytest -x
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spec-runner.config.yaml"
    path.write_text(text)
    return path


class TestTheLoaderRefuses:
    def test_mixing_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config_from_yaml(_write(tmp_path, BATTLE_CONFIG))

    def test_the_message_names_every_discarded_key(self, tmp_path):
        """Naming them is the whole point — the operator has to be able to see
        which of their settings did nothing."""
        with pytest.raises(ConfigError) as exc:
            load_config_from_yaml(_write(tmp_path, BATTLE_CONFIG))
        message = str(exc.value)
        for key in ("execution_mode", "claude_command", "command_template"):
            assert key in message, message

    def test_it_is_not_swallowed_into_defaults(self, tmp_path):
        """`load_config_from_yaml` catches broad exceptions and returns `{}`.
        Falling into that path would be the original bug wearing a new hat:
        the settings still do nothing, and the defaults still spend money."""
        try:
            result = load_config_from_yaml(_write(tmp_path, BATTLE_CONFIG))
        except ConfigError:
            return
        pytest.fail(f"the mixed config loaded as {result!r} instead of raising")

    def test_a_config_that_cannot_be_read_is_refused_too(self, tmp_path):
        """Raised by review of this PR, and the same fail-open: a config the
        loader cannot parse used to log a warning and return `{}`, which sends
        the run to the defaults — a paid model with write access — while the
        operator believes their file is in force."""
        with pytest.raises(ConfigError) as exc:
            load_config_from_yaml(_write(tmp_path, ": invalid: yaml: ["))
        assert "spec-runner.config.yaml" in str(exc.value)

    def test_an_executor_key_that_is_not_a_mapping_is_refused(self, tmp_path):
        """`executor:` with nothing under it discards everything too, and used
        to crash into the same silent `{}`."""
        with pytest.raises(ConfigError) as exc:
            load_config_from_yaml(_write(tmp_path, "executor:\nmax_retries: 5\n"))
        assert "executor" in str(exc.value)


class TestEitherShapeAloneStillWorks:
    """The regression that would hurt most: refusing a config that was always
    legal."""

    def test_the_legacy_wrapped_config_loads(self, tmp_path):
        loaded = load_config_from_yaml(_write(tmp_path, LEGACY_CONFIG))
        assert loaded["max_retries"] == 5
        assert loaded["test_command"] == "pytest -x"

    def test_the_flat_v2_config_loads(self, tmp_path):
        loaded = load_config_from_yaml(_write(tmp_path, FLAT_CONFIG))
        assert loaded["max_retries"] == 5
        assert loaded["test_command"] == "pytest -x"

    def test_the_dead_top_level_sections_are_still_only_warnings(self, tmp_path):
        """`execution_order`, `skip_tasks` and `environment` sit at the top
        level of every bundled legacy template. They are unread, they are
        already warned about, and they are not settings that got discarded —
        so they must not become errors."""
        path = _write(
            tmp_path,
            "execution_order: [TASK-001]\nskip_tasks: []\nenvironment: {}\n" + LEGACY_CONFIG,
        )
        assert load_config_from_yaml(path)["max_retries"] == 5
        result = validate_config(path)
        assert result.ok, result.errors
        assert result.warnings

    def test_an_unrecognised_top_level_key_is_not_a_discarded_setting(self, tmp_path):
        """Detection intersects with the known keys on purpose. A comment
        anchor or a third-party section is noise, not a setting that silently
        stopped working."""
        path = _write(tmp_path, "x-editor-hint: whatever\n" + LEGACY_CONFIG)
        assert load_config_from_yaml(path)["max_retries"] == 5


class TestValidateReportsIt:
    def test_it_is_an_error_not_a_warning(self, tmp_path):
        result = validate_config(_write(tmp_path, BATTLE_CONFIG))
        assert not result.ok
        assert any("claude_command" in e for e in result.errors), result.errors
        assert not any("claude_command" in w for w in result.warnings), result.warnings

    def test_validate_used_to_pass_this_file(self, tmp_path):
        """Pinning the actual F-7 observation: `validate` reported 0 errors on
        the config that sent a run to a paid model."""
        assert validate_config(_write(tmp_path, BATTLE_CONFIG)).errors

    def test_it_keeps_reporting_the_rest_of_the_file(self, tmp_path):
        """`validate` answers "what is wrong with my setup", not "what is the
        first thing wrong with it" — stopping at the mixing would hand the
        operator their problems one run at a time."""
        result = validate_config(
            _write(tmp_path, BATTLE_CONFIG + "  max_retriez: 3\n"),
        )
        assert any("claude_command" in e for e in result.errors), result.errors
        assert any("max_retriez" in e for e in result.errors), result.errors


@pytest.mark.slow
class TestTheCliSaysItWithoutATraceback:
    """A handler test is not a surface test — twice this month a fix was
    correct in the function and wrong at the command line."""

    def _run(self, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")
        _write(tmp_path, BATTLE_CONFIG)
        return subprocess.run(
            [sys.executable, "-m", "spec_runner.cli", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize("command", ["status", "validate", "run", "costs"])
    def test_it_refuses_and_explains(self, tmp_path, command):
        proc = self._run(tmp_path, command)
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, combined
        assert "Traceback" not in combined, combined
        assert "claude_command" in combined, combined

    def test_a_run_never_starts(self, tmp_path):
        """The one that matters: this config is how a run reached a paid model
        with the operator believing it was pointed at a stand-in."""
        proc = self._run(tmp_path, "run")
        assert proc.returncode != 0
        assert "executor" in (proc.stdout + proc.stderr)

    def test_validate_still_validates(self, tmp_path):
        """Every other command stops at the loader; `validate` runs, because
        listing the setup's problems is what it is for."""
        proc = self._run(tmp_path, "validate")
        combined = proc.stdout + proc.stderr
        assert "tasks.md" in combined, combined
