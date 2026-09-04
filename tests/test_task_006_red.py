"""RED checkpoint for TASK-006 (BEH-07, spec-runner#341).

`Given` a RED-authoring pass whose failing test carries two findings: one the
declared mechanical fix clears (`BADWORD`), one it cannot (`AGENTWORD`) — the
fix command in this fixture only ever touches `BADWORD`, the same way a real
linter's `--fix` clears import order but leaves a naming violation behind.
`When` the pre-freeze lint still fails after the mechanical fix.
`Then` the system does not refuse the attempt outright (that is BEH-04's
shape): it makes exactly one additional paid call of the same RED phase,
whose prompt carries the text of the remaining finding, the current red-file
and the selector — a cold call, since spec-runner has no session-continuation
mechanism — and the attempt reaches a confirmed-red checkpoint once that call
clears the remainder.
`And` the ledger records two separate paid calls for the task, not one
(#213's `check_before_call`/`record_agent_call` pair applies to the agent
round the same way it applies to the authoring call).

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-07
`checked_by`: kind=integration, owner=qa, target=tests/test_red_autofix_agent_round.py
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

findings = []
for p in sys.argv[1:]:
    text = Path(p).read_text()
    if "BADWORD" in text:
        findings.append(f"{p}: BADWORD")
    if "AGENTWORD" in text:
        findings.append(f"{p}: AGENTWORD")
if findings:
    print("\\n".join(findings))
    sys.exit(1)
sys.exit(0)
"""

_FIX_SCRIPT = """
import sys
from pathlib import Path

# Clears BADWORD only — AGENTWORD is the finding no mechanical fix can reach,
# same as a linter's --fix clearing import order but not a naming violation.
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
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _scripted_agent(monkeypatch, calls: list[str], prompts: list[str]) -> None:
    """First call authors a red with both findings; second — the agent round
    the harness owes once the mechanical fix leaves one behind — clears the
    remainder. Whether that second call happens at all is exactly what BEH-07
    is about; today nothing makes it."""
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        prompts.append(prompt)
        path = Path(config.project_root) / "tests/test_x.py"
        if not calls:
            calls.append("red")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_y():  # BADWORD AGENTWORD\n    assert False\n")
            return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")
        calls.append("agent_round")
        path.write_text(path.read_text().replace("AGENTWORD", ""))
        return tdd.AgentCall(text="ok")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestRemainingFindingsReturnToTheAgentInOneRound:
    def test_the_attempt_reaches_a_checkpoint_after_one_additional_agent_call(
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
        calls: list[str] = []
        prompts: list[str] = []
        _scripted_agent(monkeypatch, calls, prompts)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            recorded = state.agent_calls("TASK-001")

        # Then: the mechanical fix cleared BADWORD but left AGENTWORD behind —
        # BEH-07 says this is not the BEH-04 refusal shape. Exactly one
        # additional paid call happens, and the attempt reaches a confirmed
        # checkpoint.
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        assert calls == ["red", "agent_round"]

        # And: the agent round's prompt is a cold call carrying the remaining
        # finding, the current red-file and the selector — there is no
        # session to continue.
        agent_round_prompt = prompts[1]
        assert "AGENTWORD" in agent_round_prompt
        assert "tests/test_x.py::test_y" in agent_round_prompt
        assert "def test_y" in agent_round_prompt

        # And: the file that ends up frozen carries neither finding.
        frozen = (root / "tests/test_x.py").read_text()
        assert "BADWORD" not in frozen
        assert "AGENTWORD" not in frozen

        # And: the ledger shows two separately paid calls, not one — the
        # agent round is billed under #213 the same way the authoring call is.
        assert len(recorded) == 2
