"""RED for BEH-03 (spec-runner#341, TASK-003).

`Given` a fix-run of the declared linter changes not only the red-file but
also at least one neighbouring file of the working tree — and, in this case,
the fix does not even cure the finding it was run for.
`When` the RED pass judges the fix's result before the freeze.
`Then` the attempt still ends in refusal (already true: an uncured lint
finding refuses today), the out-of-scope bytes are still rolled back
(already true), but the refusal message must name the concrete paths that
went beyond the boundary of the claimed file — and today it does not: the
"stray" detection and its path-naming message live only on the branch where
the fix *cured* the finding (`_absorb_lint_fix`). A fix that strays without
curing falls through to `_lint_claimed`'s generic "lint failed on the file
about to be frozen" text, silent about the stray.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-03
`checked_by`: kind=integration, owner=qa, target=tests/test_red_autofix_scope.py
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

# Strays onto a neighbouring file and a newly created one, but never touches
# the claimed file at all — so the finding it was invoked for is never cured.
_STRAY_NO_CURE_FIX_SCRIPT = """
import sys
from pathlib import Path

Path("README.md").write_text(Path("README.md").read_text() + "strayed\\n")
Path("tests/leftover.bak").write_text("junk\\n")
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
    return Task(id="TASK-003", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_fixable_red(monkeypatch) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestAStrayThatDoesNotCureStillNamesTheOutOfBoundsPaths:
    """BEH-03: the refusal must name the paths that went out of scope,
    whether or not the fix happened to cure the finding along the way — the
    scope violation and the cure are two different questions, and today's
    code only answers the first on the branch where the second is also yes.
    """

    def test_the_refusal_names_the_stray_paths_even_when_the_fix_did_not_cure(
        self, tmp_path_factory, monkeypatch
    ):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_STRAY_NO_CURE_FIX_SCRIPT)

        cfg = _cfg(
            root,
            lint_command=_shell_command(check_script),
            lint_fix_command=_shell_command(fix_script),
        )
        _agent_writing_a_fixable_red(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        # Already true today: the attempt refuses, and the stray bytes are
        # rolled back — this is not what is missing.
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert claims == []
        assert "strayed" not in (root / "README.md").read_text()
        assert not (root / "tests/leftover.bak").exists()

        # Missing today: the refusal text names the paths that went beyond
        # the boundary of the claimed file (BEH-03's own wording), not just
        # "lint failed on the file about to be frozen".
        detail = result.detail or ""
        assert "outside the claim" in detail
        assert "README.md" in detail
        assert "tests/leftover.bak" in detail
