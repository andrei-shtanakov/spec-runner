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

import subprocess
from pathlib import Path

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.preset_cmd import list_presets, load_fragment
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from tests.conftest import PAID_AGENT_COMMANDS


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
            pytest.raises(AssertionError, match="would call the real agent"),
        ):
            execute_task(task, cfg, state)
