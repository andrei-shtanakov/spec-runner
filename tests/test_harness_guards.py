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

from pathlib import Path

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig


def _cfg(tmp_path: Path, cmd: str) -> ExecutorConfig:
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / ".state.db",
        logs_dir=tmp_path / ".logs",
        claude_command=cmd,
    )


class TestTheGuard:
    @pytest.mark.parametrize("cmd", ["claude", "codex", "opencode", "pi", "qwen"])
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
