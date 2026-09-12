"""The suite must not be able to spend money by accident.

Written after it did. A new test drove the RED phase with the default
`claude_command`, the checkpoint it planted turned out not to be reusable, and
the phase called `claude` and was billed **$0.55** — on a project where paid
runs are explicitly unauthorised. Nothing was wrong with the product: the test
was missing one `monkeypatch.setattr`, and the only signal was a minute of
silence before it failed for an unrelated reason.

`conftest._no_real_agent_calls` closes that. These tests pin the guard itself,
because a guard nothing checks is a guard that quietly stops working — and its
failure mode is a bill.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.execution import RealAgentCallRefused
from spec_runner.executor import execute_task
from spec_runner.lifecycle import TddPhase
from spec_runner.preset_cmd import list_presets, load_fragment
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace
from tests import conftest
from tests.conftest import (
    _NEVER_EXECUTE,
    BELT_PROBE_COMMAND,
    PAID_AGENT_COMMANDS,
    PaidBinaryReached,
)


def _cfg(tmp_path: Path, cmd: str) -> ExecutorConfig:
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / ".state.db",
        logs_dir=tmp_path / ".logs",
        claude_command=cmd,
    )


class TestTheGuard:
    @pytest.mark.parametrize("cmd", sorted(PAID_AGENT_COMMANDS))
    def test_a_bare_agent_name_is_refused(self, tmp_path, monkeypatch, cmd):
        """Nothing may execute even if the guard is gone.

        The first version of this test called the seam and relied on the guard
        to stop it — so deleting the guard to check the test made a **real
        call**, which is the failure it exists to prevent, performed by its own
        verification. A test about not spending money must not be able to spend
        money when it fails.
        """

        def _explode(*_a, **_k):
            raise RuntimeError("nothing may be executed by this test")

        monkeypatch.setattr(tdd.subprocess, "run", _explode)

        with pytest.raises(AssertionError, match="would call the real agent"):
            tdd._run_agent(_cfg(tmp_path, cmd), "any prompt")

    def test_a_fake_script_still_runs(self, tmp_path):
        """Fake-CLI tests are the established way to exercise this path, and
        they cost nothing — the guard must not break them.

        The one place this file lets a subprocess run: a script the test wrote
        itself.
        """
        fake = tmp_path / "fake-agent"
        fake.write_text('#!/bin/bash\necho "TDD_SELECTOR: tests/test_x.py::test_y"\n')
        fake.chmod(0o755)

        call = tdd._run_agent(_cfg(tmp_path, str(fake)), "any prompt")

        assert "TDD_SELECTOR" in call.text

    def test_a_test_that_stubs_the_seam_is_unaffected(self, monkeypatch, tmp_path):
        """The guard patches the same attribute a test would; a test that
        stubs it afterwards wins, which is what every existing test does."""
        monkeypatch.setattr(tdd, "_run_agent", lambda *a, **k: tdd.AgentCall(text="stubbed"))

        assert tdd._run_agent(_cfg(tmp_path, "claude"), "any prompt").text == "stubbed"

    def test_the_guard_knows_every_cli_this_project_ships_a_preset_for(self):
        """The envelope, pinned against the product rather than against a
        second hand-kept list (Copilot, #246).

        Parametrizing over `PAID_AGENT_COMMANDS` proves every listed name is
        refused, but it cannot notice a name going *missing* — the cases would
        vanish with it. What cannot vanish quietly is a bundled preset: adding
        `spec-runner config --preset <new-cli>` means the suite can now invoke
        that CLI, so the guard has to learn it in the same change.
        """
        shipped = {load_fragment(name).command for name in list_presets()}

        assert shipped <= PAID_AGENT_COMMANDS, (
            f"these CLIs have presets but are not guarded: {sorted(shipped - PAID_AGENT_COMMANDS)}"
        )


class TestTheGuardCoversVerifyFirst:
    """spec-runner#402/TASK-013 (BEH-32): a `verify_first` task whose live
    entry run observes a genuine `test_failure` walks the same RED-authoring
    seam a `tdd` task does (`execution.py`'s `verify_first_red` branch) —
    a path that did not exist when `TestTheGuard` above was written and that
    it never exercises (it calls `tdd._run_agent` directly, never through
    `execute_task`). The guard must fire there too, not only on the seam in
    isolation.
    """

    def _repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        for args in (
            ("init", "-q"),
            ("config", "user.email", "t@example.com"),
            ("config", "user.name", "t"),
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        tests_dir = root / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_probe.py").write_text("def test_it():\n    assert False\n")
        spec = root / "spec"
        spec.mkdir()
        (spec / "tasks.md").write_text(
            "# Tasks\n\n### TASK-900: Probe\n🟠 P1 | ⬜ TODO | Est: 1d\n\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
        return root

    def test_a_verify_first_red_authoring_call_is_refused(self, tmp_path):
        """The refusal must come from the RED-authoring seam, and nothing may
        run even if the guard is gone.

        Two defects this test had, both of which let it pass while covering
        nothing:

        * `pytest.raises(AssertionError, match="would call the real agent")`
          also catches `RealAgentCallRefused` — a subclass of `AssertionError`
          carrying the SAME message, raised by the guard on the *green* seam
          (`execution._run_agent_process`) when the flow goes around
          `_run_red_phase_gate` entirely. So deleting the `verify_first_red`
          branch this class exists to cover left the test green: the standard
          path refused instead, with an identical message. The seam is now
          named — the refusal must NOT be the green one — and the recorded
          phase is asserted, so the RED path must genuinely have been entered.
        * a test about not spending money relied wholly on the autouse guard.
          Drop `"claude"` from `PAID_AGENT_COMMANDS` and `_refuse_tdd` falls
          through to the real `tdd._run_agent` and executes the CLI. That is
          not hypothetical: it happened while measuring this very test
          (spec-runner#455, 2026-09-12) and executed the real agent **seven
          times across three runs** — RED authoring, task execution, and once
          the review seam, which no guard covered. The belt that prevents it
          now lives in `conftest._belt_never_executes_a_paid_binary`, one
          level below every seam, so it protects the whole suite rather than
          this test alone.
        """

        root = self._repo(tmp_path)
        cfg = ExecutorConfig(
            project_root=root,
            state_file=root / ".state.db",
            logs_dir=root / ".logs",
            test_command="pytest",
            tdd_runner="pytest",
            run_tests_on_done=False,
            create_git_branch=False,
            auto_commit=True,
            claude_command="claude",
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id="TASK-900",
            name="Probe",
            priority="p1",
            status="todo",
            estimate="1d",
            execution_mode="verify_first",
            verifies=["tests/test_probe.py"],
        )

        with (
            ExecutorState(cfg) as state,
            pytest.raises(AssertionError, match="would call the real agent") as refused,
        ):
            execute_task(task, cfg, state)

        assert not isinstance(refused.value, RealAgentCallRefused), (
            "the refusal came from the green seam (`_run_agent_process`), not "
            "from RED authoring — the `verify_first_red` branch this test "
            "exists to cover was not entered at all"
        )

        with ExecutorState(cfg) as state:
            phases = [
                row["phase"] for row in state.tdd_phase_history(task.id, resolve_namespace(cfg))
            ]
        assert TddPhase.RED_AUTHORING.value in phases, (
            f"the RED-authoring phase must be recorded before the paid call is "
            f"refused; observed {phases}"
        )


class TestTheBeltCoversEverySeamEvenWithTheGuardGone:
    """spec-runner#455: the belt underneath the name-based guard.

    `_no_real_agent_calls` refuses by CLI name at two seams. Its failure
    modes are known and were all observed for real: a seam it does not cover
    (the review seam had none), a test that replaces the patched seam itself,
    and — the one that cost money on 2026-09-12 — `PAID_AGENT_COMMANDS`
    losing a name, which is both how the guard decays in production and what
    a reviewer does deliberately to check it.

    So each test here empties `PAID_AGENT_COMMANDS` entirely, drives the seam
    all the way to its subprocess call, and asserts that
    `conftest.PaidBinaryReached` — the belt, not the guard — is what stopped
    it.

    **Nothing here may execute a paid binary even if the belt is broken.**
    The command under test is `BELT_PROBE_COMMAND`, a name that exists in no
    PATH: a belt that fails to fire produces `FileNotFoundError`, not a bill.

    Real CLI names do appear elsewhere in this file — `TestTheGuard`
    parametrizes over `PAID_AGENT_COMMANDS`, and the verify-first test drives
    `execute_task` with `claude_command="claude"` — but always with something
    ahead of the process: an exploded `subprocess.run`, or the guard and the
    belt together. In THIS class, where the guard is deliberately emptied,
    only the sentinel is ever passed to a seam.
    """

    def _unguarded(self, monkeypatch) -> None:
        """The guard at its worst: every name gone from its list."""
        monkeypatch.setattr(conftest, "PAID_AGENT_COMMANDS", frozenset())

    def test_the_tdd_red_authoring_seam_is_belted(self, tmp_path, monkeypatch):
        self._unguarded(monkeypatch)

        with pytest.raises(PaidBinaryReached) as belted:
            tdd._run_agent(_cfg(tmp_path, BELT_PROBE_COMMAND), "any prompt")

        assert BELT_PROBE_COMMAND in str(belted.value)

    def test_the_standard_execution_seam_is_belted(self, tmp_path, monkeypatch):
        self._unguarded(monkeypatch)
        from spec_runner.execution import _run_agent_process
        from spec_runner.runner import build_cli_invocation

        invocation = build_cli_invocation(
            cmd=BELT_PROBE_COMMAND,
            prompt="any prompt",
            model=None,
            template=None,
            skip_permissions=False,
            json_output=True,
        )

        with pytest.raises(PaidBinaryReached) as belted:
            _run_agent_process(_cfg(tmp_path, BELT_PROBE_COMMAND), invocation)

        assert BELT_PROBE_COMMAND in str(belted.value)

    def test_the_review_seam_is_belted(self, tmp_path, monkeypatch):
        """The seam the name-based guard never covered at all — and the one
        that produced the seventh session of the 2026-09-12 incident."""
        self._unguarded(monkeypatch)
        from spec_runner.review import _run_reviewer

        with pytest.raises(PaidBinaryReached) as belted:
            _run_reviewer(
                _cfg(tmp_path, BELT_PROBE_COMMAND),
                task_id="TASK-901",
                provenance="review",
                prompt="any prompt",
                review_cmd=BELT_PROBE_COMMAND,
                review_model="",
                review_template="",
            )

        assert BELT_PROBE_COMMAND in str(belted.value)

    def test_the_belt_knows_every_name_the_guard_and_the_presets_know(self):
        """Drift, caught statically — nothing is executed to check this.

        The belt is allowed to be broader than the guard (an extra name costs
        nothing), but never narrower: a name the guard refuses, or a CLI this
        project ships a preset for, must also be one the belt would stop if
        the guard ever let it through.
        """
        shipped = {load_fragment(name).command for name in list_presets()}

        assert PAID_AGENT_COMMANDS <= _NEVER_EXECUTE, (
            "the guard refuses names the belt would execute: "
            f"{sorted(PAID_AGENT_COMMANDS - _NEVER_EXECUTE)}"
        )
        assert shipped <= _NEVER_EXECUTE, (
            f"these CLIs have presets but the belt does not know them: "
            f"{sorted(shipped - _NEVER_EXECUTE)}"
        )

    def test_every_process_creation_primitive_is_belted(self, monkeypatch):
        """All three doors, not just the one the seams happen to use today.

        The seams above reach `subprocess.run`. `runner.py` also streams a CLI
        through `asyncio.create_subprocess_exec`, and `Popen` is one
        refactoring away from being the path a seam takes — a belt that
        covered only `run` would be silently bypassed the day that happens.
        """
        self._unguarded(monkeypatch)

        with pytest.raises(PaidBinaryReached):
            subprocess.Popen([BELT_PROBE_COMMAND])

        async def _spawn():
            await asyncio.create_subprocess_exec(BELT_PROBE_COMMAND)

        with pytest.raises(PaidBinaryReached):
            asyncio.run(_spawn())

    def test_a_wrapped_template_hides_the_name_inside_one_element(self, monkeypatch):
        """`command_template: bash -lc '{cmd} …'` is the case the docstring
        always claimed to cover and did not (spec-runner#459 review).

        `build_cli_invocation` runs the formatted template through
        `shlex.split`, so the agent name ends up INSIDE the third element:
        `["bash", "-lc", "<cmd> -p '…'"]`. Taking `PurePath(part).name` per
        element yields the basename of that whole string and misses it — and
        on the review seam, which no name-based guard covers, that is a real
        paid call.
        """
        self._unguarded(monkeypatch)
        from spec_runner.runner import build_cli_invocation

        invocation = build_cli_invocation(
            cmd=BELT_PROBE_COMMAND,
            prompt="any prompt",
            model=None,
            template="bash -lc '{cmd} -p {prompt}'",
            skip_permissions=False,
            json_output=True,
        )
        assert invocation.argv[0] == "bash", (
            f"fixture must produce a wrapped invocation, got {invocation.argv!r}"
        )
        assert BELT_PROBE_COMMAND not in invocation.argv, (
            "the name must be hidden INSIDE an element, or this test proves nothing"
        )

        with pytest.raises(PaidBinaryReached) as belted:
            subprocess.run(invocation.argv, capture_output=True)

        assert BELT_PROBE_COMMAND in str(belted.value)

    def test_a_shell_string_is_one_element_too(self, monkeypatch):
        """`subprocess.run("<cmd> --flag", shell=True)` passes a single
        string; its basename is the whole command line."""
        self._unguarded(monkeypatch)

        with pytest.raises(PaidBinaryReached) as belted:
            subprocess.run(f"{BELT_PROBE_COMMAND} --version", shell=True, capture_output=True)

        assert BELT_PROBE_COMMAND in str(belted.value)
