"""RED for TASK-001 (spec-runner#341, BEH-01).

`Given` a project that declared a linter via `commands.lint`, a task running
in `execution_mode: tdd`, and a RED-authoring pass whose failing test carries
only lint findings that declared linter marks as fixable.
`When` the RED pass reaches the pre-freeze lint.
`Then` the attempt does not end in refusal: the system attempts a fix and
brings the task to a recorded checkpoint, the claimed file lints clean
afterwards, and the agent is not invoked a second time to author the same
test again.

Today `tdd._lint_claimed` only *checks* the declared linter and turns any
non-zero exit straight into a refusal (`RedOutcome.UNVERIFIABLE`, no
checkpoint) — see `_lint_claimed` in `src/spec_runner/tdd.py`. There is no fix
attempt, so this red pins the missing behaviour.
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

_CHECK_SCRIPT = """
import sys
from pathlib import Path

bad = any("BADWORD" in Path(p).read_text() for p in sys.argv[1:])
sys.exit(1 if bad else 0)
"""

_FIX_SCRIPT = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", ""))
"""


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.c"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _shell_command(script_path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"


def _cfg(root: Path, lint_command: str, lint_fix_command: str) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command=lint_command,
        lint_command_declared=True,
        lint_fix_command=lint_fix_command,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_fixable_red(monkeypatch, calls: list) -> None:
    """Scripted RED-authoring call: one failing test with one fixable finding."""
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        calls.append("red")
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestAFixableLintFindingReachesAConfirmedCheckpoint:
    def test_the_attempt_is_not_refused_and_the_file_ends_up_clean(
        self, tmp_path_factory, monkeypatch
    ):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_FIX_SCRIPT)

        cfg = _cfg(
            root,
            lint_command=_shell_command(check_script),
            lint_fix_command=_shell_command(fix_script),
        )
        calls: list = []
        _agent_writing_a_fixable_red(monkeypatch, calls)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        # Then: the attempt is not refused — a fix was attempted and the task
        # reached a recorded, confirmed-red checkpoint.
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        assert [c.path for c in claims] == ["tests/test_x.py"]

        # And: the claimed file now lints clean with the declared linter.
        frozen = (root / "tests/test_x.py").read_text()
        assert "BADWORD" not in frozen

        # And: no second full RED-authoring call happened for the same test.
        assert calls == ["red"]
