"""Acceptance tests for BEH-04, BEH-05, BEH-11 (spec-runner#341, TASK-004).

BEH-04 (integration): an unfixable lint finding keeps today's failure class —
no checkpoint, no claim, the gate answers "no confirmed red", the attempt
ends as a `HOOK_FAILURE`-shaped refusal rather than a success or an
infrastructure error.

BEH-05 (integration): the number of fix attempts is a declared, fixed cap —
known in advance and independent of what the finding says — not a loop that
could run forever or skip the lint silently.

BEH-11 (integration): an operator reading the refusal text alone (no logs)
can tell "a fix ran and failed" from "no fix ran", and the tried case also
names the findings still standing.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-04
(—BEH-11)
`checked_by`: kind=integration, owner=qa,
target=tests/test_red_lint_autofix_refusal.py
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.gates import GateContext, GateStatus, PhaseOutcome, _red_gate
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

# Prints the concrete finding instead of staying silent, and counts its own
# invocations in a side file — so a test can assert how many times it ran
# without instrumenting spec_runner itself.
_COUNTING_CHECK_SCRIPT = """
import sys
from pathlib import Path

counter = Path(__file__).parent / "check_calls.count"
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")

bad = [p for p in sys.argv[1:] if "BADWORD" in Path(p).read_text()]
for p in bad:
    print(f"{p}: found BADWORD")
sys.exit(1 if bad else 0)
"""

# Runs, leaves a side artefact, but never touches the claimed file — the
# finding reproduces on every check that follows it. Also counts its own
# invocations.
_UNCURING_COUNTING_FIX_SCRIPT = """
import sys
from pathlib import Path

counter = Path(__file__).parent / "fix_calls.count"
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")

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


def _cfg_with_declared_fix(root: Path, check_script: Path, fix_script: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command=_shell_command(check_script),
        lint_command_declared=True,
        lint_fix_command=_shell_command(fix_script),
        lint_fix_command_declared=True,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestUnfixableFindingKeepsTodaysFailureClass:
    """BEH-04: a finding no declared fix clears ends in the same refusal
    shape as before this feature existed — no checkpoint, no claim, the gate
    answers "no confirmed red", not a success and not an infrastructure
    error."""

    def test_no_checkpoint_no_claim_gate_says_no_confirmed_red(self, tmp_path_factory, monkeypatch):
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_COUNTING_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_UNCURING_COUNTING_FIX_SCRIPT)

        root = _repo(tmp_path_factory.mktemp("unfixable"))
        cfg = _cfg_with_declared_fix(root, check_script, fix_script)
        _agent_writing_a_fixable_red(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))
            checkpoint = state.red_checkpoint("TASK-004", resolve_namespace(cfg))

            head = _git(root, "rev-parse", "HEAD").stdout.strip()
            gate = _red_gate(
                GateContext(
                    task_id="TASK-004",
                    checkpoint_sha=head,
                    config=cfg,
                    state=state,
                    facts={"execution_mode": "tdd"},
                )
            )

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.checkpoint is None
        assert claims == []
        assert checkpoint is None

        # Neither a silent pass nor an infrastructure error — an honest,
        # unsatisfied verdict about the work.
        assert gate.status is GateStatus.UNSATISFIED
        assert gate.outcome is PhaseOutcome.NOT_RUN
        assert "no confirmed red" in gate.detail


class TestFixAttemptCapIsDeclaredAndRespected:
    """BEH-05: the machine-fix cap is a fixed, declared number — one attempt
    per RED pass — not a loop, and not something that grows with how many
    times the finding reproduces."""

    def test_the_fix_runs_exactly_once_and_the_check_runs_exactly_twice(
        self, tmp_path_factory, monkeypatch
    ):
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_COUNTING_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_UNCURING_COUNTING_FIX_SCRIPT)

        root = _repo(tmp_path_factory.mktemp("capped"))
        cfg = _cfg_with_declared_fix(root, check_script, fix_script)
        _agent_writing_a_fixable_red(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        # BEH-04 still holds — the cap is about how the refusal is reached,
        # not a change to what it is.
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert claims == []

        check_calls = int((scripts / "check_calls.count").read_text())
        fix_calls = int((scripts / "fix_calls.count").read_text())

        # One machine-fix attempt: the initial check that finds the problem,
        # one fix invocation, one recheck — never a second fix pass even
        # though the finding still reproduces after the first.
        assert fix_calls == 1
        assert check_calls == 2


class TestOperatorDistinguishesTriedFromUntriedRefusal:
    """BEH-11: the refusal text alone — no logs — tells a tried-and-failed
    fix apart from a fix that never ran, and names the class of failure
    (BEH-04) plus the remaining findings."""

    def test_tried_and_untried_refusals_are_textually_distinguishable(
        self, tmp_path_factory, monkeypatch
    ):
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_COUNTING_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_UNCURING_COUNTING_FIX_SCRIPT)

        tried_root = _repo(tmp_path_factory.mktemp("tried"))
        tried_cfg = _cfg_with_declared_fix(tried_root, check_script, fix_script)
        _agent_writing_a_fixable_red(monkeypatch)
        with ExecutorState(tried_cfg) as state:
            tried = run_red_phase(_task(), tried_cfg, state)

        untried_root = _repo(tmp_path_factory.mktemp("untried"))
        untried_cfg = ExecutorConfig(
            project_root=untried_root,
            state_file=untried_root / ".state.db",
            logs_dir=untried_root / ".logs",
            execution_mode="tdd",
            test_command="python -m pytest",
            lint_command=_shell_command(check_script),
            lint_command_declared=True,
            # No fix invocation declared at all.
        )
        untried_cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        _agent_writing_a_fixable_red(monkeypatch)
        with ExecutorState(untried_cfg) as state:
            untried = run_red_phase(_task(), untried_cfg, state)

        tried_detail = tried.detail or ""
        untried_detail = untried.detail or ""

        # Both keep the recognisable opening — the operator sees the same
        # class of failure either way.
        assert "lint failed on the file about to be frozen" in tried_detail
        assert "lint failed on the file about to be frozen" in untried_detail

        assert "a fix ran and did not clear the finding" in tried_detail
        assert "no fix ran" in untried_detail
        assert "a fix ran and did not clear the finding" not in untried_detail
        assert "no fix ran" not in tried_detail

        assert "remaining findings" in tried_detail.lower()
        assert "found BADWORD" in tried_detail
        assert "found BADWORD" in untried_detail
