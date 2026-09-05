"""Acceptance test for BEH-28 (spec-runner#341, TASK-018).

`Given` a project that DECLARED a linter, a RED pass that recorded a candidate
commit, a declared fix that ran and did not clear every finding, so no
checkpoint was recorded and the attempt ended in a refusal.
`When` the same task is retried.
`Then` the remainder left on the branch is adopted via #261's
`_unregistered_red` mechanism — but *before* a fresh authoring call, not after
one runs and reproduces the identical rejection — so the retry pays for
neither a new authoring call nor a repeat of the lint-fix agent round.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-28
`checked_by`: kind=integration, owner=qa,
target=tests/test_rejected_red_remainder_is_adopted.py

None of `test_rejected_red_is_adopted.py`'s existing adoption tests cover this
path: they all set `lint_command: ""`, so the lint gate never fires and their
rejection is a claim violation, not a lint one. This test configures a
DECLARED linter whose fix never clears its finding — deterministic, so a
retry against the same tree is a foregone conclusion — and checks that the
retry makes no paid agent call at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedOutcome, run_red_phase

FAILING = "def test_thing():\n    assert False\n"
SELECTOR = "tests/test_thing.py::test_thing"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
        # A declared linter that always finds something, and a declared fix
        # that runs but never clears it (no semicolons: `is_composite_shell_command`
        # would otherwise treat the inline script as a chained command and
        # skip the fix narrowing).
        "lint_command": 'python3 -c "raise SystemExit(1)"',
        "lint_command_declared": True,
        "lint_fix_command": 'python3 -c "pass"',
        "lint_fix_command_declared": True,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _write_the_test(config, prompt, **kw):
    from spec_runner import tdd

    path = Path(config.project_root) / "tests" / "test_thing.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAILING)
    return tdd.AgentCall(text=f"TDD_SELECTOR: {SELECTOR}")


def _repo(tmp_path: Path, **overrides) -> tuple[Path, ExecutorConfig]:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _cfg(root, **overrides)


def _repo_with_a_rejected_red(tmp_path: Path, **overrides) -> tuple[Path, ExecutorConfig]:
    """The residue seeded directly, without paying for the attempt that would
    ordinarily produce it — same shape #261's own fixture uses, so the two
    adoption routes (pre- and post-authoring) are compared on equal footing.
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_thing.py").write_text(FAILING)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"TASK-104: red for {SELECTOR}")
    return root, _cfg(root, **overrides)


@pytest.mark.slow
class TestRejectedRedRemainderIsAdopted:
    def test_the_retry_does_not_pay_for_a_new_authoring_call(self, tmp_path, monkeypatch):
        from spec_runner import tdd

        root, cfg = _repo(tmp_path)

        # Attempt 1: the declared linter rejects the committed red, and the
        # declared fix does not clear it — exactly BEH-28's Given.
        monkeypatch.setattr(tdd, "_run_agent", _write_the_test)
        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)

        assert first.outcome is RedOutcome.UNVERIFIABLE
        assert first.checkpoint is None
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        subject = _git(root, "log", "-1", "--format=%s", head).stdout.strip()
        assert subject == f"TASK-104: red for {SELECTOR}", "the rejected red stayed committed"

        # Attempt 2 (the retry): nothing about the project changed, so an
        # agent asked to author the same red again would reproduce the exact
        # same rejection. The remainder already on the branch is adopted
        # instead — no authoring call, and no repeat of the lint-fix agent
        # round, is paid for.
        calls: list[str] = []

        def _record_call(config, prompt, **kw):
            calls.append(prompt)
            return tdd.AgentCall(text=f"TDD_SELECTOR: {SELECTOR}")

        monkeypatch.setattr(tdd, "_run_agent", _record_call)
        with ExecutorState(cfg) as state:
            second = run_red_phase(_task(), cfg, state)

        assert calls == [], (
            "the retry paid for a new authoring call instead of adopting the unregistered "
            "remainder left by the declared linter's rejection"
        )
        assert second.outcome is RedOutcome.UNVERIFIABLE
        assert second.checkpoint is None
        head_after = _git(root, "rev-parse", "HEAD").stdout.strip()
        assert head_after == head, "the retry adopted the existing commit, not a new one"

    def test_it_says_it_adopted_rather_than_authored(self, tmp_path, monkeypatch):
        from spec_runner import tdd

        root, cfg = _repo(tmp_path)
        monkeypatch.setattr(tdd, "_run_agent", _write_the_test)
        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        said: list[str] = []

        def _fail_if_called(config, prompt, **kw):
            raise AssertionError("the retry should not call the agent at all")

        monkeypatch.setattr(tdd, "_run_agent", _fail_if_called)
        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state, log_progress=said.append)

        assert any("adopting" in line for line in said), said

    def test_without_a_declared_linter_authoring_still_pays(self, tmp_path, monkeypatch):
        """FR-05: a project that never declared a linter keeps today's (#261)
        behavior unchanged — the same residue is still adopted eventually, but
        only *after* an authoring call runs and reproduces it, never before.
        This is what actually discriminates the `lint_command_declared` gate:
        without it, a residue with no declared linter would be adopted for
        free too, same as the declared case above.
        """
        from spec_runner import tdd

        root, cfg = _repo_with_a_rejected_red(
            tmp_path,
            lint_command="",
            lint_command_declared=False,
            lint_fix_command="",
            lint_fix_command_declared=False,
        )
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        calls: list[str] = []

        def _record_call(config, prompt, **kw):
            calls.append(prompt)
            return tdd.AgentCall(text=f"TDD_SELECTOR: {SELECTOR}")

        monkeypatch.setattr(tdd, "_run_agent", _record_call)
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert len(calls) == 1, "an undeclared linter must not skip the authoring call"
        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert result.checkpoint is not None
        assert result.checkpoint.commit_sha == head, "the commit that was already there"
