"""TASK-005 RED (#341 BEH-29): the python-shaped default fix invocation must
never be run for a project that declared a linter but no fix command — and
the refusal must say so, not stay silent about which of the several ways a
fix can fail to run this one is.

`_lint_claimed` (tdd.py) already skips *running* an undeclared fix — that part
predates this task. What it does not do yet: when `lint_fix_command_declared`
is False, the refusal it returns is byte-identical to "no fix ran" for every
other reason (composite command, unnarrowable paths), so an operator reading
it cannot tell "you never told me how to fix this" from "I tried and gave
up". BEH-29 asks for the cause to be named explicitly.
"""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, RedOutcome, run_red_phase


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


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_failing_test(monkeypatch):
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():\n    assert False\n")
        return AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestUndeclaredFixNamesItsReason:
    def test_a_declared_linter_without_a_declared_fix_names_the_reason(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            lint_command="false",
            lint_command_declared=True,
            # lint_fix_command_declared defaults to False: the field still
            # carries the python-shaped default (`uv run ruff check . --fix`),
            # but the project never declared `commands.lint_fix`.
        )
        _agent_writing_a_failing_test(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        detail = result.detail or ""
        assert "fix invocation" in detail
        assert "not declared" in detail
