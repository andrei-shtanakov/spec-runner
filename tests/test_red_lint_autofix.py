"""Acceptance test for BEH-01 (spec-runner#341, TASK-001).

`Given` a project that declared a linter via `commands.lint`, a task running
in `execution_mode: tdd`, and a RED-authoring pass whose failing test carries
only lint findings the declared linter marks as fixable.
`When` the RED pass reaches the pre-freeze lint.
`Then` the attempt does not end in refusal: the system attempts a fix and
brings the task to a recorded checkpoint.
`And` the claimed file lints clean with the declared linter afterwards.
`And` the `tests`-phase gate answers "confirmed red" for that checkpoint —
the task is free to continue on to the GREEN pass.
`And` no second RED-authoring call happens for the same test.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-01
`checked_by`: kind=integration, owner=qa, target=tests/test_red_lint_autofix.py
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.gates import GateContext, GateStatus, _red_gate
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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
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

    def test_the_red_gate_reads_it_as_confirmed_red(self, tmp_path_factory, monkeypatch):
        """The RED gate (`gates._red_gate`) — the part of the `tests`-phase
        gate chain that answers BEH-01's "confirmed red" clause. The claims
        gate, evaluated alongside it in the pre-terminal chain, is a separate
        guarantee about the checkpoint commit's own bytes (BEH-02, TASK-002) —
        out of scope here, same reasoning as the prior review of this task.
        """
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
        _agent_writing_a_fixable_red(monkeypatch, [])

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            assert result.checkpoint is not None

            ctx = GateContext(
                task_id="TASK-001",
                checkpoint_sha=result.checkpoint.commit_sha,
                config=cfg,
                state=state,
                facts={"execution_mode": "tdd"},
            )
            outcome = _red_gate(ctx)

        # And: the gate for phase `tests` answers "confirmed red" — nothing
        # stops the task from continuing on to the GREEN pass.
        assert outcome.status is GateStatus.SATISFIED


class TestACompositeLintFixCommandIsNeverNarrowedToPaths:
    """FR-09's exclusion applies to the fix command too, not only the check.

    Appending claim paths to a *composite* `lint_fix_command` (one that chains
    several programs) glues the paths onto whichever component happens to sit
    last in the chain — not necessarily the one meant to receive them. Here
    the fix script sits last, so a naive implementation would still land the
    path correctly and clean the file by luck of ordering; the guard must
    still refuse to run it, because the next project's chain order is not
    ours to guess right. The attempt then falls through to the same refusal a
    project with no fix command at all would get.
    """

    def test_the_fix_is_skipped_even_though_it_would_have_worked(
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
            lint_fix_command=f"true && {_shell_command(fix_script)}",
        )
        _agent_writing_a_fixable_red(monkeypatch, [])

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        # Then: no fix was attempted against the composite command — the
        # attempt refuses instead of continuing, even though appending the
        # path here would have happened to land on the fix script and clean
        # the file.
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "lint failed on the file about to be frozen" in (result.detail or "")
        assert claims == []

        # And: the file is left exactly as authored — nothing ran against it.
        frozen = (root / "tests/test_x.py").read_text()
        assert "BADWORD" in frozen
