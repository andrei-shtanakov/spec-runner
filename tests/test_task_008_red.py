"""RED test for BEH-10 (spec-runner#341, TASK-008).

BEH-10 (contract): for a composite `lint_command`, the system does not guess
which component would take a fix flag or a path. Fix mode is not applied at
all, and the refusal names the reason in text — "composite lint_command,
machine fix not applied" — distinct from every other reason a pre-freeze fix
can be skipped (FR-09; lesson #139 stays in force: a composite command is
never narrowed to a path either).

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-10
`checked_by`: kind=contract, owner=qa,
target=tests/test_red_lint_autofix_composite.py
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, RedOutcome, run_red_phase

# Fails unconditionally, ignoring argv — a composite `lint_command` is run
# un-narrowed (#139), so no per-file paths are ever appended to it. Models a
# project whose composite lint target (e.g. a Makefile chain) found a
# problem somewhere in the tree.
_ALWAYS_FAILING_CHECK_SCRIPT = """
import sys
print("composite lint check: found a problem")
sys.exit(1)
"""

# Would cure any finding it is pointed at — the test relies on it NEVER being
# invoked, tracked by a call counter written to a side file.
_CURING_COUNTING_FIX_SCRIPT = """
import sys
from pathlib import Path

counter = Path(__file__).parent / "fix_calls.count"
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")

for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", "fixed"))
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


def _task() -> Task:
    return Task(id="TASK-008", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_red(monkeypatch) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestCompositeLintCommandDeclaresFixSkipped:
    """BEH-10: fix mode never runs for a composite `lint_command`, and the
    refusal names that as the reason — not silence, and not the generic
    "no fix ran" wording used for every other skip cause."""

    def test_composite_check_command_skips_fix_and_names_the_reason(
        self, tmp_path_factory, monkeypatch
    ):
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_ALWAYS_FAILING_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_CURING_COUNTING_FIX_SCRIPT)

        root = _repo(tmp_path_factory.mktemp("composite"))
        composite_check = f"{_shell_command(check_script)} && true"

        cfg = ExecutorConfig(
            project_root=root,
            state_file=root / ".state.db",
            logs_dir=root / ".logs",
            execution_mode="tdd",
            test_command="python -m pytest",
            lint_command=composite_check,
            lint_command_declared=True,
            # A perfectly good, non-composite fix invocation IS declared —
            # BEH-10 is precisely that this does not matter when the check
            # command itself is composite.
            lint_fix_command=_shell_command(fix_script),
            lint_fix_command_declared=True,
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        _agent_writing_a_red(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE

        # The declared fix invocation was never invoked.
        assert not (scripts / "fix_calls.count").exists()

        detail = result.detail or ""
        assert (
            "composite lint_command, machine fix not applied" in detail
        ), f"refusal did not name the composite-command reason: {detail!r}"
