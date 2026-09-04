"""RED for BEH-04, BEH-05, BEH-11 (spec-runner#341, TASK-004).

`Given` a lint finding that a fix invocation was run against (and did not
clear) and, in a second run, a lint finding for a project that declared no
fix invocation at all — two different reasons the pre-freeze lint still
refuses.
`When` an operator reads the refusal text without opening logs (BEH-11).
`Then` the two messages must be distinguishable: one says a fix was
attempted and names the outcome plus the findings that remain, the other
says no fix was attempted at all. Today `_lint_claimed` builds both refusals
from the same template — `"lint failed on the file about to be frozen
(...): {tail}."` — with no clause naming whether a fix ran, so the two cases
produce textually indistinguishable refusals whenever the check command's
own output happens to match (routinely true: a terse linter reports the
same finding before and after a fix that did not cure it). Both refusals
must also keep today's failure class (BEH-04): no checkpoint, no claim,
`RedOutcome.UNVERIFIABLE` — already true, and reasserted here as the
invariant the message clause must not disturb.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-04,
#BEH-05, #BEH-11
`checked_by`: kind=integration, owner=qa,
target=tests/test_red_lint_autofix_refusal.py
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

# Prints the concrete finding instead of staying silent, like a real linter
# would ("ruff: F401 unused import ...") — so the tail an operator reads
# names the finding, not just a bare non-zero exit.
_CHECK_SCRIPT = """
import sys
from pathlib import Path

bad = [p for p in sys.argv[1:] if "BADWORD" in Path(p).read_text()]
for p in bad:
    print(f"{p}: found BADWORD")
sys.exit(1 if bad else 0)
"""

# Runs, leaves a side artefact, but never touches the claimed file — the
# finding reproduces on every check that follows it.
_UNCURING_FIX_SCRIPT = """
import sys
from pathlib import Path

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


def _task() -> Task:
    return Task(id="TASK-004", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_fixable_red(monkeypatch) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestOperatorTellsAFailedFixFromAnUntriedOneByTheRefusalAlone:
    def test_the_two_refusals_name_whether_a_fix_ran_and_the_tried_one_lists_findings(
        self, tmp_path_factory, monkeypatch
    ):
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_UNCURING_FIX_SCRIPT)

        # --- Run 1: a declared fix ran and did not clear the finding. ---
        tried_root = _repo(tmp_path_factory.mktemp("tried"))
        tried_cfg = ExecutorConfig(
            project_root=tried_root,
            state_file=tried_root / ".state.db",
            logs_dir=tried_root / ".logs",
            execution_mode="tdd",
            test_command="python -m pytest",
            lint_command=_shell_command(check_script),
            lint_command_declared=True,
            lint_fix_command=_shell_command(fix_script),
            lint_fix_command_declared=True,
        )
        tried_cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        _agent_writing_a_fixable_red(monkeypatch)
        with ExecutorState(tried_cfg) as state:
            tried = run_red_phase(_task(), tried_cfg, state)
            tried_claims = state.active_claims(resolve_namespace(tried_cfg))

        # --- Run 2: no fix invocation was declared — none ran at all. ---
        untried_root = _repo(tmp_path_factory.mktemp("untried"))
        untried_cfg = ExecutorConfig(
            project_root=untried_root,
            state_file=untried_root / ".state.db",
            logs_dir=untried_root / ".logs",
            execution_mode="tdd",
            test_command="python -m pytest",
            lint_command=_shell_command(check_script),
            lint_command_declared=True,
            # lint_fix_command left undeclared on purpose.
        )
        untried_cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        _agent_writing_a_fixable_red(monkeypatch)
        with ExecutorState(untried_cfg) as state:
            untried = run_red_phase(_task(), untried_cfg, state)
            untried_claims = state.active_claims(resolve_namespace(untried_cfg))

        # BEH-04: both refusals keep today's failure class — no checkpoint,
        # no claim, the same outcome. Already true; the clause below must not
        # disturb it.
        for result, claims in ((tried, tried_claims), (untried, untried_claims)):
            assert result.outcome is RedOutcome.UNVERIFIABLE
            assert result.checkpoint is None
            assert claims == []

        tried_detail = tried.detail or ""
        untried_detail = untried.detail or ""

        # BEH-11: the operator can tell "a fix ran and failed" from "no fix
        # ran" from the message text alone.
        assert "a fix ran and did not clear the finding" in tried_detail
        assert "no fix ran" in untried_detail
        assert "a fix ran and did not clear the finding" not in untried_detail
        assert "no fix ran" not in tried_detail

        # BEH-11: the tried case also names the findings still standing, not
        # only the bare fact that a fix was attempted.
        assert "remaining findings" in tried_detail.lower()
        assert "found BADWORD" in tried_detail
