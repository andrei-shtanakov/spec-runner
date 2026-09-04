"""RED for BEH-03 (spec-runner#341, TASK-003).

`Given` a fix run of the declared linter strays outside the claimed red-file
into a neighbour of the working tree, AND the fix does not cure the finding
it was run for — the check still fails after the fix runs.
`When` the RED pass judges the fix's result before the freeze.
`Then` the attempt is refused rather than continuing with a side effect.
`And` the refusal names the concrete paths that crossed the claim boundary —
not only the generic "lint failed" text `_lint_claimed` already returns for
an in-scope fix that fails to cure (BEH-04's shape). That generic text says
nothing about *why* the fix's own footprint mattered, and an operator reading
only the refusal cannot tell a boundary violation from an ordinary uncured
finding.
`And` the tree is rolled back to the authored candidate: no fix bytes, in or
out of scope, survive the refusal.

The straying-AND-curing case is already covered by
`tests/test_red_lint_autofix.py::TestAFixThatStraysOutsideTheClaimIsRolledBack`
— there the check passes after the fix, `_absorb_lint_fix` sees the stray
delta and names it. This RED targets the other fork: the fix strays AND still
does not cure, which falls into `_lint_claimed`'s own uncuring branch and
returns its plain "lint failed on the file about to be frozen" message,
verbatim, with no mention of the stray paths `_rollback_fix` is about to
discard.

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

#: Strays into a tracked neighbour and creates an untracked one, but leaves
#: the claimed file's finding untouched — the fix does not cure.
_STRAY_NONCURING_FIX_SCRIPT = """
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


class TestAStrayingFixThatDoesNotCureNamesItsStrayPaths:
    def test_the_refusal_names_the_paths_that_left_the_claim(
        self, tmp_path_factory, monkeypatch
    ):
        root = _repo(tmp_path_factory.mktemp("stray-noncure"))
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_STRAY_NONCURING_FIX_SCRIPT)

        cfg = _cfg(
            root,
            lint_command=_shell_command(check_script),
            lint_fix_command=_shell_command(fix_script),
        )
        _agent_writing_a_fixable_red(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.checkpoint is None
        assert claims == []

        # Then: the refusal names the paths that strayed outside the claim —
        # not just "lint failed", which is silent about the boundary
        # violation the operator needs to act on.
        assert "README.md" in (result.detail or "")
        assert "tests/leftover.bak" in (result.detail or "")

        # And: no fix bytes survive the refusal, in or out of scope — the
        # remainder is the authored candidate.
        assert (root / "README.md").read_text() == "x\n"
        assert not (root / "tests/leftover.bak").exists()
        assert "BADWORD" in (root / "tests/test_x.py").read_text()
