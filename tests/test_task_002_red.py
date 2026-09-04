"""RED for BEH-25 (spec-runner#341, TASK-002).

`Given` a lint fix changed bytes in the red-file after the authored diff was
committed as the candidate.
`When` the RED pass absorbs the fix into the checkpoint commit (amend,
subject-preserving per Q-04).
`Then` an operator reading the commit history around the checkpoint can see
which bytes the fix contributed, distinct from what the agent authored — not
only the merged end state.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-25
`checked_by`: kind=integration, owner=qa, target=tests/test_frozen_bytes_are_the_replayed_bytes.py
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, run_red_phase

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
        lint_fix_command_declared=True,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-002", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_fixable_red(monkeypatch) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestFixDiffIsPresentableInHistory:
    def test_the_checkpoint_commit_message_shows_the_fixs_own_bytes(
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
        _agent_writing_a_fixable_red(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None

        # Then: the subject stays the authored one (Q-04, #261 adoptability) —
        # BEH-02's own guarantee, already established. What's missing is the
        # body: an operator reading `git log` around the checkpoint must be
        # able to tell which bytes the fix contributed, not just the merged
        # end state the amend leaves behind.
        message = _git(root, "log", "-1", "--format=%B", result.checkpoint.commit_sha).stdout
        subject = message.splitlines()[0]
        assert subject == "TASK-002: red for tests/test_x.py::test_y"

        # And: the removed (authored) bytes and the added (fixed) bytes are
        # both legible in the message body, as a unified-diff-shaped trailer —
        # not merely the fact that a fix ran.
        body = message[len(subject) :]
        assert "-def test_y():  # BADWORD" in body
        assert "+def test_y():  #" in body
