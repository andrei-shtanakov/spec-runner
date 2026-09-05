"""BEH-28 (#341, TASK-018): a red rejected by a DECLARED linter's unclearable
finding does not starve the next attempt into paying for authorship again.

`_unregistered_red` (#261) already lets a retry adopt a rejected-but-committed
red instead of dead-ending on "the authoring pass changed nothing" — but every
existing adoption test (`tests/test_rejected_red_is_adopted.py`) configures
`lint_command: ""`, so the lint gate never fires and the rejection those tests
replay is a claim violation, not a lint one. None of them cover the path this
workstream's own charter describes: a project that DECLARED a linter (and a
fix invocation) whose fix does not clear every finding, leaving the red
committed and unregistered.

On that path, retrying with an unchanged project (same declared lint command,
same fix command, same file) is a foregone conclusion: the agent, asked again,
reproduces byte-identical content, the lint check fails identically, and the
declared fix clears nothing it did not clear the first time. Paying for a
fresh authoring call (and the BEH-07 lint-agent round that follows it) to
relearn what the first attempt already proved is exactly the "authorship from
scratch" the remainder is supposed to make unnecessary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedOutcome, run_red_phase

FAILING = "def test_thing():\n    assert False\n"


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
        # that runs but never clears it — "the fix did not clear all
        # findings" from BEH-28's Given, made deterministic and fast (no
        # semicolons: `is_composite_shell_command` would otherwise treat the
        # inline script as a chained command and skip the fix narrowing).
        "lint_command": 'python3 -c "raise SystemExit(1)"',
        "lint_command_declared": True,
        "lint_fix_command": 'python3 -c "pass"',
        "lint_fix_command_declared": True,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _selector_for(cfg: ExecutorConfig) -> str:
    """The ws-scoped evidential selector adoption now demands (#366 r3):
    ownership is proven by the namespace segment in the file name."""
    from spec_runner.tdd import resolve_namespace
    from spec_runner.tdd_runners import ADAPTERS

    path = ADAPTERS["pytest"].evidential_file("TASK-104", namespace=resolve_namespace(cfg))
    return f"{path}::test_thing"


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _write_the_test(config, prompt, **kw):
    from spec_runner import tdd

    selector = _selector_for(config)
    path = Path(config.project_root) / selector.split("::")[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAILING)
    return tdd.AgentCall(text=f"TDD_SELECTOR: {selector}")


class TestRejectedRedRemainderIsAdopted:
    def test_the_retry_does_not_pay_for_a_new_authoring_call(self, tmp_path, monkeypatch):
        from spec_runner import tdd

        root = tmp_path / "repo"
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "o@e.c")
        _git(root, "config", "user.name", "O")
        (root / "README.md").write_text("x\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        cfg = _cfg(root)

        # Attempt 1: the declared linter rejects the committed red, and the
        # declared fix does not clear it — exactly BEH-28's Given.
        monkeypatch.setattr(tdd, "_run_agent", _write_the_test)
        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)

        assert first.outcome is RedOutcome.UNVERIFIABLE
        assert first.checkpoint is None
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        subject = _git(root, "log", "-1", "--format=%s", head).stdout.strip()
        assert subject == f"TASK-104: red for {_selector_for(cfg)}", (
            "the rejected red stayed committed"
        )

        # Attempt 2 (the retry): nothing about the project changed, so an
        # agent asked to author the same red again would reproduce the exact
        # same rejection. The remainder already on the branch is adopted
        # instead — no authoring call is paid for to relearn that.
        calls: list[str] = []

        def _record_call(config, prompt, **kw):
            calls.append(prompt)
            return tdd.AgentCall(text=f"TDD_SELECTOR: {_selector_for(config)}")

        monkeypatch.setattr(tdd, "_run_agent", _record_call)
        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        # BEH-28's contract is «не платит за новое АВТОРСТВО»: the adopted
        # residue may still receive the BEH-07 follow-up round (a different,
        # budget-gated paid call — #366 review fix 1), so only authoring
        # prompts are forbidden here.
        authoring_calls = [c for c in calls if not c.startswith("# RED phase follow-up")]
        assert authoring_calls == [], (
            "the retry paid for a new authoring call instead of adopting the unregistered "
            "remainder left by the declared linter's rejection"
        )
