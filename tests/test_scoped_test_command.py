"""Scoping must not corrupt the gate, and must not hide that it narrowed it (#139).

`build_scoped_test_command` appended the mapped test paths to the end of the
whole string when it found no `tests/` token. A `test_command` is a *shell*
string and real configs are composite:

    python3 pin_check.py && uv run pytest -q && uv run pyrefly check

so the paths landed on `pyrefly check`. The `replace` branch was no safer: it
substituted the first `tests/` substring anywhere in the chain, which belongs
to whichever component happened to mention it. Ordering the commands so pytest
comes last works around it, but that is knowledge of this function's internals,
not a property of the config.

The second half is evidence: when scoping does fire, the gate runs a different
set than the config declares, and the only trace was one log line — for a
contract that demands the full suite (workstream acceptance, release gate),
that is a substituted proof.

Found by reading code during another incident: across five disputatio runs and
26 tasks, scoping never fired at all (it needs `changed_since`, i.e. parallel
mode). So these are the tests that observation could not provide.
"""

from pathlib import Path

import pytest

from spec_runner.git_ops import build_scoped_test_command, is_composite_shell_command

COMPOSITE = "python3 pin_check.py && uv run pytest -q && uv run pyrefly check"


@pytest.fixture
def test_files(tmp_path: Path) -> list[Path]:
    d = tmp_path / "tests"
    d.mkdir()
    files = [d / "test_a.py", d / "test_b.py"]
    for f in files:
        f.write_text("")
    return files


class TestCompositeCommandsAreNotScoped:
    def test_composite_command_is_left_alone(self, tmp_path, test_files):
        """Running the full declared gate is always safe; guessing is not."""
        assert build_scoped_test_command(COMPOSITE, test_files, tmp_path) == COMPOSITE

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest -q && ruff check",
            "pytest -q || echo failed",
            "pytest -q; ruff check",
            "pytest -q | tee out.txt",
            "pytest -q\nruff check",
        ],
    )
    def test_every_shell_operator_blocks_scoping(self, tmp_path, test_files, cmd):
        assert build_scoped_test_command(cmd, test_files, tmp_path) == cmd
        assert is_composite_shell_command(cmd)

    def test_paths_never_land_on_the_last_component(self, tmp_path, test_files):
        """The concrete disputatio failure: paths appended to `pyrefly check`."""
        out = build_scoped_test_command(COMPOSITE, test_files, tmp_path)
        assert not out.rstrip().endswith("test_b.py")
        assert out.split("&&")[-1].strip().removeprefix("uv run ") == "pyrefly check"

    def test_a_plain_command_is_not_composite(self):
        assert not is_composite_shell_command("uv run pytest tests/ -v -m 'not slow'")


class TestTokenReplacement:
    def test_replaces_the_whole_path_argument(self, tmp_path, test_files):
        """`pytest tests/unit` must not become `pytest <files>unit`.

        The old substring replace cut `tests/` out of `tests/unit` and glued
        the remainder onto the last injected path.
        """
        out = build_scoped_test_command("uv run pytest tests/unit -v", test_files, tmp_path)
        assert "unit" not in out
        assert "tests/test_a.py" in out and "tests/test_b.py" in out
        assert out.endswith("-v")

    def test_replaces_bare_tests_directory(self, tmp_path, test_files):
        out = build_scoped_test_command("uv run pytest tests/ -v", test_files, tmp_path)
        assert "tests/test_a.py" in out
        assert " tests/ " not in out

    def test_does_not_touch_a_substring_inside_another_word(self, tmp_path, test_files):
        """`--ignore=vendor/tests_helper` is not the test-path argument."""
        base = "uv run pytest --ignore=contests/x tests/ -v"
        out = build_scoped_test_command(base, test_files, tmp_path)
        assert "--ignore=contests/x" in out, out

    def test_appends_when_there_is_no_path_argument(self, tmp_path, test_files):
        out = build_scoped_test_command("uv run pytest", test_files, tmp_path)
        assert out == "uv run pytest tests/test_a.py tests/test_b.py"

    def test_no_files_returns_base_unchanged(self, tmp_path):
        assert build_scoped_test_command("uv run pytest tests/", [], tmp_path) == (
            "uv run pytest tests/"
        )


class TestScopingCanBeForbidden:
    """A contract that demands the full suite must be able to say so."""

    def test_config_defaults_to_scoping_enabled(self, tmp_path):
        from spec_runner.config import ExecutorConfig

        cfg = ExecutorConfig(project_root=tmp_path)
        assert cfg.scoped_tests is True

    def test_hook_runs_the_full_command_when_scoping_is_off(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        calls: list[str] = []
        monkeypatch.setattr(
            hooks, "find_changed_source_files", lambda *a, **k: [tmp_path / "src" / "x.py"]
        )
        monkeypatch.setattr(
            hooks, "map_source_to_test_files", lambda *a, **k: [tmp_path / "tests" / "test_x.py"]
        )
        monkeypatch.setattr(
            hooks,
            "build_scoped_test_command",
            lambda *a, **k: pytest.fail("scoping ran with scoped_tests=False"),
        )

        cfg = _hook_config(tmp_path, scoped_tests=False)
        _run_test_stage(monkeypatch, hooks, cfg, calls)
        # calls[0] is the test stage; the lint stage follows it.
        assert calls[0] == cfg.test_command

    def test_hook_scopes_by_default(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        calls: list[str] = []
        monkeypatch.setattr(
            hooks, "find_changed_source_files", lambda *a, **k: [tmp_path / "src" / "x.py"]
        )
        monkeypatch.setattr(
            hooks, "map_source_to_test_files", lambda *a, **k: [tmp_path / "tests" / "test_x.py"]
        )

        cfg = _hook_config(tmp_path, scoped_tests=True)
        _run_test_stage(monkeypatch, hooks, cfg, calls)
        assert calls and "test_x.py" in calls[0]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _hook_config(tmp_path: Path, **overrides):
    from spec_runner.config import ExecutorConfig

    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "run_tests_on_done": True,
        "run_review": False,
        "auto_commit": False,
        "create_git_branch": False,
        "test_command": "uv run pytest tests/ -v",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _run_test_stage(monkeypatch, hooks, cfg, calls: list[str]) -> None:
    """Drive post_done_hook far enough to observe the test command it runs."""
    import subprocess

    from spec_runner.task import Task

    def _fake_run(cmd, *a, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hooks.subprocess, "run", _fake_run)
    task = Task(id="TASK-001", name="x", priority="p0", status="todo", estimate="1d")
    hooks.post_done_hook(task, cfg, True, changed_since=1.0)
