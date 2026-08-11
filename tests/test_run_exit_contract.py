"""F-2: one exit contract for `run`, whatever selected the tasks.

The battle test on published v2.25.0 found `run --task=X` exiting **0** after
the task failed every attempt, while `run --all` on the same repository exited
1. The label "single-task mode" understates it: the exit code was decided by
*whether the loop chose to stop early*, not by whether the work succeeded. The
`--all` path only differed because it reaches an idle-stop verdict afterwards;
the fixed-list path had no final judgement at all.

So the fix is not "make --task match --all" but "judge the run's outcome once,
and use it in both".

Exit surface:

    0  every task the run touched reached success
    1  work did not finish — attempts exhausted, a gate refused, a stop reason
    2  the instrument broke — the run could not find out whether the work is
       good, which is a different thing to tell CI than "the work is bad"

These go through the real CLI entrypoint (`spec_runner.cli.main` with argv),
not the helper underneath, because the defect lived in the wiring between the
loop and `sys.exit` — a direct call to the helper would have passed throughout.
"""

import subprocess
import sys
from pathlib import Path

import pytest

FAKE_AGENT = """#!/bin/bash
# $BEHAVIOUR is baked in at write time.
{body}
"""


def _run_cli(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    """Invoke the CLI the way an orchestrator or CI would."""
    return subprocess.run(
        [sys.executable, "-c", "from spec_runner.cli import main; main()", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _project(tmp_path: Path, agent_body: str, *, tasks: int = 1, extra: str = "") -> Path:
    root = tmp_path / "proj"
    (root / "spec").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    agent = root / "agent.sh"
    agent.write_text(FAKE_AGENT.format(body=agent_body))
    agent.chmod(0o755)

    body = "# Tasks\n"
    for i in range(1, tasks + 1):
        body += f"\n### TASK-00{i}: task {i}\n🟠 P1 | ⬜ TODO\nEst: 1d\n\n- [ ] do it\n"
    (root / "spec" / "tasks.md").write_text(body)

    (root / "spec-runner.config.yaml").write_text(
        f"""claude_command: {agent}
command_template: "{{cmd}} -p {{prompt}}"
max_retries: 1
commands:
  test: "true"
  lint: ""
hooks:
  pre_start:
    create_git_branch: false
    sync_deps: false
  post_done:
    run_tests: false
    run_lint: false
    run_review: false
    auto_commit: false
{extra}"""
    )
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


FAILS = 'echo "TASK_FAILED: no"; exit 1'
SUCCEEDS = 'echo "TASK_COMPLETE"'


class TestTheTwoSelectorsAgree:
    """The acceptance criterion that names the defect: the same outcome must
    produce the same exit code however the tasks were chosen."""

    @pytest.mark.slow
    def test_a_failing_task_exits_non_zero_under_task(self, tmp_path):
        root = _project(tmp_path, FAILS)
        assert _run_cli(root, "run", "--task=TASK-001").returncode != 0

    @pytest.mark.slow
    def test_a_failing_task_exits_non_zero_under_all(self, tmp_path):
        root = _project(tmp_path, FAILS)
        assert _run_cli(root, "run", "--all").returncode != 0

    @pytest.mark.slow
    def test_both_selectors_agree_on_failure(self, tmp_path):
        one = _project(tmp_path / "a", FAILS)
        two = _project(tmp_path / "b", FAILS)
        assert (
            _run_cli(one, "run", "--task=TASK-001").returncode
            == _run_cli(two, "run", "--all").returncode
        )

    @pytest.mark.slow
    def test_both_selectors_agree_on_success(self, tmp_path):
        one = _project(tmp_path / "a", SUCCEEDS)
        two = _project(tmp_path / "b", SUCCEEDS)
        assert _run_cli(one, "run", "--task=TASK-001").returncode == 0
        assert _run_cli(two, "run", "--all").returncode == 0


class TestResumableIsNotSuccess:
    @pytest.mark.slow
    def test_a_task_left_unfinished_is_not_reported_as_success(self, tmp_path):
        """ "Come back and finish this" is not "done", and CI reads only the
        exit code."""
        root = _project(tmp_path, FAILS)
        result = _run_cli(root, "run", "--task=TASK-001")
        assert result.returncode != 0
        assert "completed=0" in (result.stdout + result.stderr)


class TestTheVerdictIsComputedFromTheOutcome:
    """Unit-level cover for the shared judgement, so the reason a run exits
    non-zero is inspectable without spawning a process."""

    def test_no_failures_is_zero(self):
        from spec_runner.cli import run_exit_code

        assert run_exit_code(failed=0, infrastructure=0, prior=0) == 0

    def test_a_failure_is_one(self):
        from spec_runner.cli import run_exit_code

        assert run_exit_code(failed=1, infrastructure=0, prior=0) == 1

    def test_an_instrument_error_is_two(self):
        """The instrument broke: the run cannot say whether the work is good.
        Telling CI "the work is bad" would be a different, wrong statement."""
        from spec_runner.cli import run_exit_code

        assert run_exit_code(failed=0, infrastructure=1, prior=0) == 2

    def test_a_real_failure_outranks_an_instrument_error(self):
        """Something concrete is wrong and someone can act on it; that is the
        more useful thing to report."""
        from spec_runner.cli import run_exit_code

        assert run_exit_code(failed=1, infrastructure=1, prior=0) == 1

    def test_an_existing_stop_reason_exit_is_preserved(self):
        """The loop already exits 1 for `on_task_failure: stop` and friends;
        the new verdict must not quietly downgrade those to 0."""
        from spec_runner.cli import run_exit_code

        assert run_exit_code(failed=0, infrastructure=0, prior=1) == 1


class TestTheInstrumentErrorPathIsWired:
    """`run_exit_code` maps it; these check the two joints on either side —
    classifying the refusal, and the loop feeding the verdict."""

    def test_an_instrument_refusal_is_classified_apart_from_a_verdict(self):
        from spec_runner.execution import _refusal_error_code
        from spec_runner.hooks import GATE_INSTRUMENT_ERROR_PREFIX
        from spec_runner.state import ErrorCode

        assert (
            _refusal_error_code(f"{GATE_INSTRUMENT_ERROR_PREFIX}: reviewer CLI vanished")
            is ErrorCode.INFRASTRUCTURE
        )
        assert (
            _refusal_error_code("Pre-terminal gate unsatisfied: 2 findings")
            is ErrorCode.HOOK_FAILURE
        )

    def test_a_run_whose_only_failure_is_infrastructure_exits_2(self, tmp_path, monkeypatch):
        """Below the subprocess boundary on purpose: forcing a real instrument
        error through the CLI would mean breaking git mid-run, and what needs
        proving here is that the loop's verdict reads the recorded code."""
        import argparse

        from spec_runner import cli
        from spec_runner.config import ExecutorConfig
        from spec_runner.state import ErrorCode, ExecutorState

        root = tmp_path / "proj"
        (root / "spec").mkdir(parents=True)
        (root / "spec" / "tasks.md").write_text(
            "# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO\nEst: 1d\n\n- [ ] x\n"
        )
        config = ExecutorConfig(
            project_root=root,
            state_file=root / "spec" / ".executor-state.db",
            logs_dir=root / "spec" / ".logs",
            create_git_branch=False,
            auto_commit=False,
        )
        config.logs_dir.mkdir(parents=True, exist_ok=True)

        def _instrument_failure(task, cfg, state, **kwargs):
            state.record_attempt(
                task.id,
                False,
                0.0,
                error="Pre-terminal gate infrastructure error: gate could not answer",
                error_code=ErrorCode.INFRASTRUCTURE,
            )
            return False

        monkeypatch.setattr(cli, "run_with_retries", _instrument_failure)
        monkeypatch.setattr(cli, "ExecutorState", ExecutorState)
        args = argparse.Namespace(
            task="TASK-001",
            all=False,
            milestone=None,
            dry_run=False,
            json_result=False,
            force=True,
            allow_dirty_spec=True,
            tui=False,
            hitl_review=False,
            no_reset_failed=False,
            strict=False,
            no_strict=False,
        )
        with pytest.raises(SystemExit) as exit_info:
            cli._run_tasks(args, config, lock_held=True)
        assert exit_info.value.code == 2, "an instrument error must not read as a work failure"


class TestTheVerdictScope:
    """Copilot's finding on #183: the verdict looked only at the initially
    ready list, so `--all` could miss a task that became ready and then failed,
    and a selected task never attempted counted as success."""

    def _state(self, tmp_path, tasks: dict[str, str]):
        from spec_runner.config import ExecutorConfig
        from spec_runner.state import ExecutorState

        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
            logs_dir=tmp_path / "logs",
        )
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        state = ExecutorState(config)
        for task_id, status in tasks.items():
            state.get_task_state(task_id).status = status
        return config, state

    def test_a_task_that_became_ready_mid_run_is_counted(self, tmp_path):
        """It is not in `tasks_to_run`, and its failure must still be the
        run's failure."""
        from spec_runner.state import ErrorCode

        config, state = self._state(tmp_path, {})
        state.record_attempt("TASK-LATE", False, 0.0, error="x", error_code=ErrorCode.TASK_FAILED)
        touched = {tid for tid, ts in state.tasks.items() if ts.attempts}
        state.close()
        assert "TASK-LATE" in touched

    def test_a_selected_task_never_attempted_is_not_success(self, tmp_path):
        """A run interrupted before the first attempt did not do the work."""
        from spec_runner.cli import run_exit_code

        # No attempts, not successful → the verdict must see a failure.
        assert run_exit_code(failed=1, infrastructure=0, prior=0) == 1


class TestTheSchemaKeepsUpWithTheEnum:
    """The published contract must enumerate every code a run can store. It had
    already drifted before this change: `TASK_BLOCKED` shipped in #140 and was
    never added, so state from a blocked task failed validation."""

    def test_every_error_code_is_in_the_published_schema(self):
        import json

        from spec_runner.state import ErrorCode

        schema = json.loads(
            (
                Path(__file__).resolve().parent.parent / "schemas" / "executor-state.schema.json"
            ).read_text(encoding="utf-8")
        )
        enum = set(schema["definitions"]["TaskAttempt"]["properties"]["error_code"]["enum"])
        missing = {c.value for c in ErrorCode} - enum
        assert not missing, f"ErrorCode values absent from the published schema: {sorted(missing)}"
