"""Acceptance tests for BEH-03 (spec-runner#341, TASK-003).

BEH-03 (integration): a fix-run of the declared linter that rewrites files
beyond the red-file — a neighbour of the working tree, or a file it creates —
must not be allowed to ride into a checkpoint, whether or not the fix cured
the finding it was invoked for. The attempt refuses, no checkpoint or claim
is recorded, the out-of-scope bytes are rolled back, and — the part that was
missing — the refusal names the concrete paths that went beyond the boundary
of the claimed file, on both the "cured" and the "did not cure" branch.

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

# Strays the same way, but also cures the claimed file's finding — the fix
# and the cure are two different questions; both branches must name strays.
_STRAY_AND_CURE_FIX_SCRIPT = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", ""))
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


def _run(tmp_path_factory, monkeypatch, fix_script_source: str):
    root = _repo(tmp_path_factory.mktemp("proj"))
    scripts = tmp_path_factory.mktemp("scripts")
    check_script = scripts / "check_lint.py"
    check_script.write_text(_CHECK_SCRIPT)
    fix_script = scripts / "fix_lint.py"
    fix_script.write_text(fix_script_source)

    cfg = _cfg(
        root,
        lint_command=_shell_command(check_script),
        lint_fix_command=_shell_command(fix_script),
    )
    _agent_writing_a_fixable_red(monkeypatch)

    with ExecutorState(cfg) as state:
        result = run_red_phase(_task(), cfg, state)
        claims = state.active_claims(resolve_namespace(cfg))

    return root, cfg, result, claims


class TestAStrayThatDoesNotCureStillNamesTheOutOfBoundsPaths:
    """BEH-03: a fix that never cures the finding must still name the paths
    it strayed onto — not just "lint failed on the file about to be frozen".
    """

    def test_the_refusal_names_the_stray_paths_even_when_the_fix_did_not_cure(
        self, tmp_path_factory, monkeypatch
    ):
        root, cfg, result, claims = _run(
            tmp_path_factory, monkeypatch, _STRAY_NO_CURE_FIX_SCRIPT
        )

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert claims == []
        assert "strayed" not in (root / "README.md").read_text()
        assert not (root / "tests/leftover.bak").exists()

        detail = result.detail or ""
        assert "outside the claim" in detail
        assert "README.md" in detail
        assert "tests/leftover.bak" in detail


class TestAStrayThatCuresStillRefusesAndNamesTheOutOfBoundsPaths:
    """BEH-03, the other branch: a fix that DOES cure the claimed file's
    finding but also strays must still refuse and name the strayed paths —
    curing the finding is not a pass on scope.
    """

    def test_the_refusal_names_the_stray_paths_even_when_the_fix_cured(
        self, tmp_path_factory, monkeypatch
    ):
        root, cfg, result, claims = _run(
            tmp_path_factory, monkeypatch, _STRAY_AND_CURE_FIX_SCRIPT
        )

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert claims == []
        assert "strayed" not in (root / "README.md").read_text()
        assert not (root / "tests/leftover.bak").exists()

        detail = result.detail or ""
        assert "outside the claim" in detail
        assert "README.md" in detail
        assert "tests/leftover.bak" in detail
