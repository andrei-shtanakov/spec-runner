"""#141 slice 1a: the `execution_mode` surface — recorded, not yet enforced.

A project default with an optional per-task override (owner amendment 4). This
slice deliberately changes **no behaviour**: it establishes how the mode is
declared and resolved so the RED checkpoint (1b) and its gate (1c) have
something to read. Nothing branches on the mode yet, and `standard` is the
default, so a project that says nothing is untouched.

The `standard` guarantee, stated precisely (design §3.1): *execution, terminal
state and external contracts do not change* — deliberately not "byte
identical", which Part A's append-only rows already preclude.

Design: `docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md` §3.1
"""

from pathlib import Path

import pytest

from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.task import Task, parse_tasks


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-001",
        "name": "t",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestTheDefaultIsStandard:
    def test_config_defaults_to_standard(self, tmp_path):
        assert _cfg(tmp_path).execution_mode == "standard"

    def test_a_task_declares_nothing_by_default(self):
        assert _task().execution_mode is None

    def test_resolving_a_silent_task_under_a_silent_project_is_standard(self, tmp_path):
        assert _cfg(tmp_path).resolve_execution_mode(_task()) == "standard"


class TestThePerTaskOverride:
    """Owner amendment 4: a project default *with* an optional per-task
    override — so a single task can be brought under the contract without
    converting a whole repo, and vice versa."""

    def test_a_task_can_opt_in_while_the_project_is_standard(self, tmp_path):
        cfg = _cfg(tmp_path, execution_mode="standard")
        assert cfg.resolve_execution_mode(_task(execution_mode="tdd")) == "tdd"

    def test_a_task_can_opt_out_while_the_project_is_tdd(self, tmp_path):
        """Opting out must be possible, or the override is only half a knob and
        one unsuitable task forces the whole project back to standard."""
        cfg = _cfg(tmp_path, execution_mode="tdd")
        assert cfg.resolve_execution_mode(_task(execution_mode="standard")) == "standard"

    def test_a_silent_task_inherits_the_project(self, tmp_path):
        cfg = _cfg(tmp_path, execution_mode="tdd")
        assert cfg.resolve_execution_mode(_task()) == "tdd"

    def test_no_task_at_all_resolves_the_project_default(self, tmp_path):
        assert _cfg(tmp_path, execution_mode="tdd").resolve_execution_mode(None) == "tdd"


class TestAnUnknownModeIsRefused:
    """Fail closed and loudly. A typo silently meaning `standard` is how a
    project believes it is under the contract when it is not."""

    def test_an_unknown_project_mode_raises(self, tmp_path):
        with pytest.raises(ConfigError) as exc:
            _cfg(tmp_path, execution_mode="TDD!").resolve_execution_mode(_task())
        assert "standard" in str(exc.value) and "tdd" in str(exc.value)

    def test_an_unknown_task_mode_raises(self, tmp_path):
        with pytest.raises(ConfigError) as exc:
            _cfg(tmp_path).resolve_execution_mode(_task(execution_mode="red-green"))
        assert "TASK-001" in str(exc.value), "the message must name the task carrying the typo"

    def test_the_config_error_is_not_a_bare_valueerror(self, tmp_path):
        """`ConfigError` is what the CLI turns into a clean `⛔` line instead of
        a traceback."""
        with pytest.raises(ConfigError):
            _cfg(tmp_path, execution_mode="nope").resolve_execution_mode(None)


class TestTheTasksFileDeclaration:
    """`**Mode:** tdd` alongside the existing `**Traces to:**` / `**Depends
    on:**` metadata, so the declaration lives with the task it governs."""

    def _tasks(self, tmp_path: Path, body: str) -> list[Task]:
        path = tmp_path / "tasks.md"
        path.write_text(body)
        return parse_tasks(path)

    def test_a_mode_line_is_parsed(self, tmp_path):
        tasks = self._tasks(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:** tdd\nEst: 1d\n\n- [ ] x\n",
        )
        assert tasks[0].execution_mode == "tdd"

    def test_the_mode_line_is_case_insensitive_and_trimmed(self, tmp_path):
        tasks = self._tasks(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:**  TDD \nEst: 1d\n",
        )
        assert tasks[0].execution_mode == "tdd"

    def test_a_task_without_the_line_declares_nothing(self, tmp_path):
        tasks = self._tasks(tmp_path, "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\nEst: 1d\n")
        assert tasks[0].execution_mode is None

    def test_an_unknown_word_survives_parsing_unmapped(self, tmp_path):
        """Case and whitespace are folded; meaning is not. An unknown word
        survives to `resolve_execution_mode`, which refuses it — mapping it to
        `standard` at parse time would hide the typo."""
        tasks = self._tasks(
            tmp_path, "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:** rgr\nEst: 1d\n"
        )
        assert tasks[0].execution_mode == "rgr"


class TestNothingBranchesOnItYet:
    """Slice 1a's whole claim. The mode is declarable and resolvable; no code
    path reads it to do anything differently."""

    #: Modules that decide what a run actually *does*. Declaring the mode
    #: (config, task) and refusing a bad value (validate) are not that; those
    #: are allowed to name it. This list is the claim: as long as none of these
    #: mentions `execution_mode`, no execution path can branch on it.
    EXECUTION_PATH = (
        "execution.py",
        "hooks.py",
        "runner.py",
        "review.py",
        "gates.py",
        "cli.py",
    )

    def test_no_execution_path_consults_the_mode(self):
        import subprocess

        repo_root = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            ["git", "grep", "-l", "execution_mode", "--", "src/spec_runner"],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        # `git grep` exits 1 when it matches nothing; anything else means the
        # search itself failed, and an empty stdout would let this guard pass
        # while checking nothing.
        assert out.returncode in (0, 1), f"git grep failed ({out.returncode}): {out.stderr}"
        touched = {Path(line.strip()).name for line in out.stdout.splitlines() if line.strip()}
        assert {"config.py", "task.py"} <= touched, (
            "the guard found no declaration sites — it is not searching what it thinks it is"
        )
        branching = touched & set(self.EXECUTION_PATH)
        assert not branching, (
            f"{sorted(branching)} reads execution_mode — 1a declares the mode, "
            "it does not enforce it. Enforcement arrives with the RED checkpoint (1b/1c), "
            "and this guard should be deleted there rather than widened here."
        )


class TestValidateRefusesItToo:
    """`resolve_execution_mode` raises at run time; `validate` says so first,
    which is the difference between a failed run and a message."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "spec-runner.config.yaml"
        path.write_text(body)
        return path

    def test_an_unknown_mode_is_a_config_error(self, tmp_path):
        from spec_runner.validate import validate_config

        result = validate_config(self._write(tmp_path, "execution_mode: red-green\n"))
        assert not result.ok
        assert any("execution_mode" in e for e in result.errors)

    @pytest.mark.parametrize("mode", ["standard", "tdd"])
    def test_the_known_modes_pass(self, tmp_path, mode):
        from spec_runner.validate import validate_config

        assert validate_config(self._write(tmp_path, f"execution_mode: {mode}\n")).ok

    def test_saying_nothing_passes(self, tmp_path):
        from spec_runner.validate import validate_config

        assert validate_config(self._write(tmp_path, "auto_commit: true\n")).ok
