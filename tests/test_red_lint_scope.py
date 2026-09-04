"""#220: the RED-phase lint runs only for a linter the project declared.

`lint_command` defaults to `uv run ruff check .`, and the RED phase lints the
file it is about to freeze — reasonably, since a claim makes that file
byte-immutable. On an Elixir project that declared `commands.test` and no
`commands.lint`, that default had ruff read a `.exs` file, report 251 errors,
and turn every red into `unverifiable`. The RED gate then refused to implement,
so `execution_mode: tdd` could not run **at all** — and the message an operator
saw named a linter they never configured.

Found by the free claims rehearsal for #214, whose first run died here before
reaching what it was testing.

The four boundaries the owner drew (issue #220), each with a test below:

1. No declared `commands.lint` → the pre-freeze lint is skipped.
2. A declared lint runs, and its failure is `unverifiable` — unchanged.
3. `hooks.post_done.run_lint: false` must **not** control this lint. They are
   different guarantees: "do not gate finished work on lint" is not "do not
   freeze a file that does not lint".
4. A deterministic lint failure must not be retried for money. Measured rather
   than assumed — see `TestItIsNotPaidTwice`, which also documents that the
   premise held in the issue ("`unverifiable` is recovered/retried") is not
   what the code does today.
"""

import os
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig, build_config, load_config_from_yaml
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.c"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str = "TASK-001") -> Task:
    return Task(id=task_id, name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_failing_test(monkeypatch, calls: list | None = None):
    """Replace the paid RED authoring call with a scripted one."""
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        if calls is not None:
            calls.append("red")
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


def _build_args(**overrides) -> Namespace:
    defaults = {
        "max_retries": None,
        "timeout": None,
        "no_tests": False,
        "no_branch": False,
        "no_commit": False,
        "no_review": False,
        "callback_url": "",
        "spec_prefix": "",
        "project_root": None,
        "max_concurrent": 0,
        "budget": None,
        "task_budget": None,
        "hitl_review": False,
        "log_level": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestTheLoaderKnowsWhatWasDeclared:
    """`lint_command_declared` is the whole mechanism: an inferred default and
    a declared linter are indistinguishable in `lint_command` alone."""

    def _config_from(self, tmp_path: Path, body: str) -> ExecutorConfig:
        path = tmp_path / "spec-runner.config.yaml"
        path.write_text(body)
        return build_config(load_config_from_yaml(path), _build_args())

    def test_a_project_that_declares_only_a_test_command_declares_no_linter(self, tmp_path):
        cfg = self._config_from(tmp_path, "executor:\n  commands:\n    test: mix test\n")
        assert cfg.lint_command_declared is False
        assert cfg.lint_command == "uv run ruff check ."  # the default is still there

    def test_a_declared_linter_is_declared(self, tmp_path):
        cfg = self._config_from(
            tmp_path,
            "executor:\n  commands:\n    test: mix test\n    lint: mix format --check-formatted\n",
        )
        assert cfg.lint_command_declared is True
        assert cfg.lint_command == "mix format --check-formatted"

    def test_an_empty_declaration_declares_nothing(self, tmp_path):
        cfg = self._config_from(tmp_path, 'executor:\n  commands:\n    lint: ""\n')
        assert cfg.lint_command_declared is False

    def test_no_config_file_at_all_leaves_the_default_untouched(self, tmp_path):
        """`load_config_from_yaml` returns {} for a missing file, so nothing
        overrides the dataclass default. Documented, not asserted as desirable:
        a project with no config file has also declared no `execution_mode`."""
        cfg = build_config(load_config_from_yaml(tmp_path / "nope.yaml"), _build_args())
        assert cfg.lint_command_declared is True


@pytest.mark.slow
class TestTheRedPhaseLint:
    def test_an_undeclared_linter_does_not_run(self, tmp_path, monkeypatch):
        """Boundary 1. The command here would fail if it ran at all — the red
        is confirmed because the lint was skipped, not because it passed."""
        root = _repo(tmp_path)
        cfg = _cfg(root, lint_command="false", lint_command_declared=False)
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert [c.path for c in claims] == ["tests/test_x.py"]

    def test_a_declared_linter_still_runs_and_still_refuses(self, tmp_path, monkeypatch):
        """Boundary 2 — unchanged behaviour, pinned next to the change that
        could have swallowed it."""
        root = _repo(tmp_path)
        cfg = _cfg(root, lint_command="false", lint_command_declared=True)
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            assert state.active_claims(resolve_namespace(cfg)) == []

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "lint failed on the file about to be frozen" in (result.detail or "")

    def test_the_post_done_lint_switch_does_not_control_it(self, tmp_path, monkeypatch):
        """Boundary 3. `run_lint: false` says "do not gate finished work on
        lint"; it does not say "freeze a file that does not lint"."""
        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            lint_command="false",
            lint_command_declared=True,
            run_lint_on_done=False,
            lint_blocking=False,
        )
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE

    def test_an_undeclared_linter_is_skipped_even_when_lint_is_on(self, tmp_path, monkeypatch):
        """The mirror of the above: the two switches are independent in both
        directions, so neither can be quietly wired to the other."""
        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            lint_command="false",
            lint_command_declared=False,
            run_lint_on_done=True,
            lint_blocking=True,
        )
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL


class TestUndeclaredFixIsNeverInferred:
    """#341 BEH-29: `lint_fix_command` always carries the python-shaped
    default (`uv run ruff check . --fix`), declared or not — unlike
    `lint_command`, which is None until the loader fills it in. A project
    that declared a linter but never `commands.lint_fix` must not have that
    default run against it, and the refusal must name the reason rather than
    reading identically to every other way a fix can fail to run."""

    def test_a_declared_linter_without_a_declared_fix_names_the_reason(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            lint_command="false",
            lint_command_declared=True,
            # lint_fix_command_declared defaults to False: the project never
            # declared commands.lint_fix.
        )
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        detail = result.detail or ""
        assert "fix invocation" in detail
        assert "not declared" in detail

    def test_the_default_fix_is_not_run_against_a_non_python_project(self, tmp_path, monkeypatch):
        """A non-python project (Elixir with `mix credo`) never declared
        `commands.lint_fix`. Running the python-shaped default `uv run ruff
        check . --fix` over such a tree in write mode would be the #220
        incident again, this time rewriting files instead of merely
        misreporting them — proven here by a fake `uv` on PATH that would
        leave a marker file behind if the harness ever invoked it."""
        root = _repo(tmp_path)
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir()
        marker = tmp_path / "uv-was-invoked"
        fake_uv = bin_dir / "uv"
        fake_uv.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        fake_uv.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        cfg = _cfg(
            root,
            lint_command="mix credo --strict",
            lint_command_declared=True,
        )
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        detail = result.detail or ""
        assert "not declared" in detail
        assert "no fix ran" in detail
        assert not marker.exists(), "the python-shaped default fix must never run"


@pytest.mark.slow
class TestItIsNotPaidTwice:
    """Boundary 4, measured.

    The issue expected `unverifiable` to reach the gate as an instrument error
    and be retried for money. It does not, and the reason is worth writing
    down: a lint failure records **no checkpoint**, so the gate answers "no
    confirmed red for this task" — `UNSATISFIED`, not `INSTRUMENT_ERROR` —
    which `_refusal_error_code` maps to `HOOK_FAILURE`, and that is in
    `_FATAL_ERRORS`. One paid RED call, no retry.

    Nobody decided that; it falls out of where the early return sits. These
    tests are what makes it a property instead of an accident.
    """

    def _run(self, tmp_path: Path, monkeypatch, calls: list) -> tuple:
        from spec_runner.execution import run_with_retries

        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            lint_command="false",
            lint_command_declared=True,
            max_retries=3,
            retry_delay_seconds=0,
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
        )
        (root / "spec").mkdir(exist_ok=True)
        (root / "spec" / "tasks.md").write_text(
            "### TASK-001: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )
        _agent_writing_a_failing_test(monkeypatch, calls)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)
            attempts = state.get_task_state("TASK-001").attempts
        return result, attempts

    def test_a_deterministic_lint_failure_costs_exactly_one_paid_call(self, tmp_path, monkeypatch):
        calls: list = []
        result, attempts = self._run(tmp_path, monkeypatch, calls)

        assert result is False
        assert len(calls) == 1, "a linter that fails identically every time must not be re-paid"
        assert len(attempts) == 1

    def test_the_refusal_names_the_linter_not_a_missing_red(self, tmp_path, monkeypatch):
        """The diagnosis used to land nowhere near the cause: the gate's
        generic "no confirmed red for this task" was the whole message, and the
        lint failure that produced it was dropped."""
        _result, attempts = self._run(tmp_path, monkeypatch, [])

        error = attempts[-1].error or ""
        assert "lint failed on the file about to be frozen" in error
        assert "tests/test_x.py" in error
