"""Acceptance tests for BEH-08 and BEH-09 (spec-runner#341, TASK-007).

`Given` a fix ran against the authored red before the freeze.
`Then` (BEH-08) a fix that made the test pass yields `NOT_RED`, not a
confirmed checkpoint; (BEH-09) a fix that broke the build yields
`UNVERIFIABLE`; in both cases no claim is recorded and the gate cannot
answer "confirmed red".

Delivered under a tdd-waiver (spec/.tdd-evidence/waivers/…/TASK-007.json):
the behaviour is an emergent property of TASK-001–006 — the fix's bytes are
absorbed into the candidate before the replay, the replay judges those very
bytes, and only `EXPECTED_FAIL` records a claim — so an honest failing red
for this task cannot be written. These greens lock the behaviour in place.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-08
`checked_by`: kind=integration, owner=qa, target=tests/test_autofix_does_not_whiten_red.py
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

#: BEH-08: clears the finding AND flips the assertion — the "fix" whitens the
#: red. The replay must catch this on the absorbed bytes.
_WHITENING_FIX_SCRIPT = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    text = path.read_text().replace("BADWORD", "")
    path.write_text(text.replace("assert False", "assert True"))
"""

#: BEH-09: clears the finding AND corrupts the file into invalid Python — the
#: "fix" breaks the build. The replay cannot even collect the test.
_BREAKING_FIX_SCRIPT = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    text = path.read_text().replace("BADWORD", "")
    path.write_text(text + "\\ndef broken(:\\n")
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
    return Task(id="TASK-007", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_fixable_red(monkeypatch) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


def _run(tmp_path_factory, monkeypatch, fix_script_body: str, slug: str):
    root = _repo(tmp_path_factory.mktemp(slug))
    scripts = tmp_path_factory.mktemp(f"{slug}-scripts")
    check_script = scripts / "check_lint.py"
    check_script.write_text(_CHECK_SCRIPT)
    fix_script = scripts / "fix_lint.py"
    fix_script.write_text(fix_script_body)

    cfg = _cfg(
        root,
        lint_command=_shell_command(check_script),
        lint_fix_command=_shell_command(fix_script),
    )
    _agent_writing_a_fixable_red(monkeypatch)

    with ExecutorState(cfg) as state:
        result = run_red_phase(_task(), cfg, state)
        claims = state.active_claims(resolve_namespace(cfg))
    return result, claims


class TestAWhiteningFixDoesNotProduceAConfirmedRed:
    """BEH-08: the replay runs on the absorbed (fixed) bytes; a test the fix
    made pass answers `NOT_RED` — no claim, no confirmed checkpoint."""

    def test_the_outcome_is_not_red_and_nothing_is_claimed(self, tmp_path_factory, monkeypatch):
        result, claims = _run(tmp_path_factory, monkeypatch, _WHITENING_FIX_SCRIPT, "whiten")
        assert result.outcome is RedOutcome.NOT_RED, result.detail
        assert claims == []
        assert (
            result.checkpoint is None or result.checkpoint.outcome is not RedOutcome.EXPECTED_FAIL
        )


class TestABuildBreakingFixDoesNotProduceAConfirmedRed:
    """BEH-09: a fix that corrupted the file demonstrates nothing — the
    replay answers `UNVERIFIABLE`, and nothing is claimed."""

    def test_the_outcome_is_unverifiable_and_nothing_is_claimed(
        self, tmp_path_factory, monkeypatch
    ):
        result, claims = _run(tmp_path_factory, monkeypatch, _BREAKING_FIX_SCRIPT, "break")
        assert result.outcome is RedOutcome.UNVERIFIABLE, result.detail
        assert claims == []
        assert (
            result.checkpoint is None or result.checkpoint.outcome is not RedOutcome.EXPECTED_FAIL
        )
