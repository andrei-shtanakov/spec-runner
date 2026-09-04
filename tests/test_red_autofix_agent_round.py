"""Acceptance tests for BEH-07 (spec-runner#341, TASK-006).

`Given` a mechanical lint fix that clears some findings in a red-file but
leaves at least one behind — trivially fixable in the test's own text.
`When` the RED phase reaches the point where it would otherwise refuse
(BEH-04's shape).
`Then` the system makes exactly one additional, cold paid call of the same
RED phase — there is no session to continue, so the prompt itself carries the
remaining findings, the current red-file and the selector — and the attempt
reaches a confirmed-red checkpoint once that call clears the remainder.
`And` the call is a paid agent call subject to #213: gated by
`check_before_call` before it starts, recorded by `record_agent_call` after —
a budget already exhausted by the authoring call means the round never
starts and the attempt reduces to the BEH-04 refusal, not a silent success.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-07
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

# Clears BADWORD only — AGENTWORD is left for the agent round.
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


def _cfg(root: Path, lint_command: str, lint_fix_command: str, **overrides) -> ExecutorConfig:
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
        **overrides,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _scripted_agent(monkeypatch, calls: list[str], prompts: list[str], *, cost: float = 0.0):
    """First call authors a red with two findings; second — the agent round —
    clears the one the mechanical fix could not."""
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        prompts.append(prompt)
        path = Path(config.project_root) / "tests/test_x.py"
        if not calls:
            calls.append("red")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_y():  # BADWORD AGENTWORD\n    assert False\n")
            return tdd.AgentCall(
                text="TDD_SELECTOR: tests/test_x.py::test_y", cost_usd=cost or None
            )
        calls.append("agent_round")
        path.write_text(path.read_text().replace("AGENTWORD", ""))
        return tdd.AgentCall(text="ok")

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestTheRemainderIsClearedInOneColdCall:
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
            claims = state.active_claims(resolve_namespace(cfg))
            recorded = state.agent_calls("TASK-001")

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        assert calls == ["red", "agent_round"]

        # And: the frozen file carries neither finding, and is claimed.
        assert [c.path for c in claims] == ["tests/test_x.py"]
        frozen = (root / "tests/test_x.py").read_text()
        assert "BADWORD" not in frozen
        assert "AGENTWORD" not in frozen

        # And: the checkpoint's own bytes are the ones the agent round wrote
        # — the amend absorbed both the mechanical fix and the round.
        committed = subprocess.run(
            ["git", "show", f"{result.checkpoint.commit_sha}:tests/test_x.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert committed == frozen

        # And: the ledger shows two separately paid calls (#213).
        assert [row["provenance"] for row in recorded] == [
            "red_authoring",
            "red_autofix_agent_round",
        ]

    def test_the_prompt_is_a_cold_call_carrying_findings_file_and_selector(
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
            run_red_phase(_task(), cfg, state)

        assert len(prompts) == 2
        agent_round_prompt = prompts[1]
        assert "AGENTWORD" in agent_round_prompt
        assert "tests/test_x.py::test_y" in agent_round_prompt
        assert "def test_y" in agent_round_prompt


class TestABudgetAlreadySpentByTheAuthoringCallStopsTheRound:
    """FR-07's budget clause: a round the ceiling cannot afford never starts
    — gated, not counted after the fact — and the attempt reduces to BEH-04's
    refusal rather than a silent success or a second overshoot call."""

    def test_the_round_does_not_start_and_the_attempt_refuses_as_beh_04(
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
            task_budget_usd=1.0,
        )
        calls: list[str] = []
        prompts: list[str] = []
        # The authoring call alone spends the whole task ceiling.
        _scripted_agent(monkeypatch, calls, prompts, cost=1.0)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))
            recorded = state.agent_calls("TASK-001")

        # Then: no agent round happened — one paid call only, the authoring
        # one — and the attempt ends in the same shape as an unfixable
        # finding (BEH-04): no checkpoint, no claim, UNVERIFIABLE.
        assert calls == ["red"]
        assert [row["provenance"] for row in recorded] == ["red_authoring"]
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.checkpoint is None
        assert claims == []
        assert "AGENTWORD" in (root / "tests/test_x.py").read_text()

        detail = result.detail or ""
        assert "lint failed on the file about to be frozen" in detail
        assert "a fix ran and did not clear the finding" in detail
        assert "not started" in detail


_MESSY_FIX_SCRIPT = """
import sys
from pathlib import Path

# Clears BADWORD but also leaves a side file behind — the exception path
# must sweep it away.
for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", ""))
Path("tests/leftover.bak").write_text("junk\\n")
"""


class TestAnAgentRoundExceptionStillRollsTheRepairBack:
    """#352 review blocker: an exception thrown by the agent round (timeout,
    unlaunchable CLI) must not bypass `_rollback_fix` — otherwise the repair
    bytes and its side files survive on the branch and the next attempt's
    `git add -A` sweeps them into a fresh red commit (FR-02/NFR-08)."""

    def test_a_timeout_in_the_round_leaves_the_authored_tree(self, tmp_path_factory, monkeypatch):
        import pytest

        from spec_runner import tdd

        root = _repo(tmp_path_factory.mktemp("round-timeout"))
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_MESSY_FIX_SCRIPT)

        cfg = _cfg(
            root,
            lint_command=_shell_command(check_script),
            lint_fix_command=_shell_command(fix_script),
        )

        calls: list[str] = []

        def fake(config, prompt, **kwargs):
            path = Path(config.project_root) / "tests/test_x.py"
            if not calls:
                calls.append("red")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def test_y():  # BADWORD AGENTWORD\n    assert False\n")
                return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")
            calls.append("agent_round")
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        monkeypatch.setattr(tdd, "_run_agent", fake)

        with ExecutorState(cfg) as state, pytest.raises(subprocess.TimeoutExpired):
            run_red_phase(_task(), cfg, state)

        # The repair is rolled back: the fix's side file is gone and the
        # authored bytes (BADWORD intact) are what the branch holds.
        assert not (root / "tests/leftover.bak").exists()
        assert "BADWORD" in (root / "tests/test_x.py").read_text()
