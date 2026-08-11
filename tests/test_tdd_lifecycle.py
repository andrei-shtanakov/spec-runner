"""#141 slice 4a: the TDD lifecycle as a recorded state machine.

Slices 1–3 built the parts — a verified red, a byte-lock, typed remedies — but
where a task *is* lived in inference: read the checkpoints, read the claims,
guess. This materialises it.

Scope is the owner's, deliberately narrow:

    GREEN_IMPLEMENTING → GREEN_VERIFYING → REFACTORING: SKIPPED
    → pre-terminal gates → merge → DONE

`REFACTORING` exists as a phase whose outcome is `skipped` — the vocabulary
already has the word, and it is honest: the stage was deliberately not
executed. **No refactor agent runs.** Whether one ever should is a separate
decision on battle-test evidence (3b), and the reason it is separate is that
under the word "REFACTORING" a new expensive and ill-defined agent stage could
otherwise arrive without anyone choosing it.

Contract: `docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md` §3a
"""

import contextlib
import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import (
    ILLEGAL,
    TddPhase,
    advance,
    current_phase,
    is_terminal,
    next_phase,
)
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestTheShapeOfTheMachine:
    def test_the_order_is_the_owners(self):
        assert next_phase(TddPhase.READY) is TddPhase.RED_AUTHORING
        assert next_phase(TddPhase.RED_AUTHORING) is TddPhase.RED_VERIFYING
        assert next_phase(TddPhase.RED_VERIFYING) is TddPhase.GREEN_IMPLEMENTING
        assert next_phase(TddPhase.GREEN_IMPLEMENTING) is TddPhase.GREEN_VERIFYING
        assert next_phase(TddPhase.GREEN_VERIFYING) is TddPhase.REFACTORING
        assert next_phase(TddPhase.REFACTORING) is TddPhase.DONE
        assert next_phase(TddPhase.DONE) is None

    def test_done_is_the_only_terminal_phase(self):
        assert is_terminal(TddPhase.DONE)
        assert not any(is_terminal(p) for p in TddPhase if p is not TddPhase.DONE)

    def test_skipping_the_red_is_not_a_legal_transition(self):
        """The one transition that carries the contract. Everything else in
        this slice is bookkeeping; this is the rule."""
        assert (TddPhase.READY, TddPhase.GREEN_IMPLEMENTING) in ILLEGAL
        assert (TddPhase.RED_AUTHORING, TddPhase.GREEN_IMPLEMENTING) in ILLEGAL

    def test_going_backwards_is_legal(self):
        """A remedy sends a task back to authoring, and a retry re-enters
        implementation. A machine that only moves forward would make both
        into errors."""
        assert (TddPhase.GREEN_VERIFYING, TddPhase.GREEN_IMPLEMENTING) not in ILLEGAL
        assert (TddPhase.RED_VERIFYING, TddPhase.RED_AUTHORING) not in ILLEGAL


class TestItIsRecordedAndSurvives:
    def test_a_task_with_no_history_is_ready(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        with ExecutorState(cfg) as state:
            assert current_phase(state, resolve_namespace(cfg), "TASK-001") is TddPhase.READY

    def test_a_transition_is_readable_afterwards(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-001", TddPhase.RED_AUTHORING, detail="authoring")
            assert current_phase(state, ns, "TASK-001") is TddPhase.RED_AUTHORING

    def test_it_survives_a_reopen(self, tmp_path):
        """Recovery is the point: a run that dies must not lose where it was."""
        cfg = _cfg(_repo(tmp_path))
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-001", TddPhase.RED_AUTHORING)
            advance(state, ns, "TASK-001", TddPhase.RED_VERIFYING)
        with ExecutorState(cfg) as state:
            assert current_phase(state, ns, "TASK-001") is TddPhase.RED_VERIFYING

    def test_the_history_is_append_only(self, tmp_path):
        """Where a task has been is evidence, not noise — the same posture as
        every other record in this contract."""
        cfg = _cfg(_repo(tmp_path))
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-001", TddPhase.RED_AUTHORING)
            advance(state, ns, "TASK-001", TddPhase.RED_VERIFYING)
            advance(state, ns, "TASK-001", TddPhase.RED_AUTHORING, detail="abandoned, start again")
            history = state.tdd_phase_history("TASK-001", ns)
        assert [h["phase"] for h in history] == [
            "red_authoring",
            "red_verifying",
            "red_authoring",
        ]
        assert history[-1]["detail"] == "abandoned, start again"

    def test_another_workstream_is_a_separate_machine(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        with ExecutorState(cfg) as state:
            advance(state, resolve_namespace(cfg), "TASK-001", TddPhase.RED_AUTHORING)
            assert current_phase(state, "someone-else", "TASK-001") is TddPhase.READY


class TestAnIllegalTransitionIsRefused:
    def test_green_without_a_red_is_refused(self, tmp_path):
        from spec_runner.lifecycle import IllegalTransition

        cfg = _cfg(_repo(tmp_path))
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state, pytest.raises(IllegalTransition) as exc:
            advance(state, ns, "TASK-001", TddPhase.GREEN_IMPLEMENTING)
        assert "red" in str(exc.value).lower()

    def test_the_refusal_is_recorded_too(self, tmp_path):
        """A refused transition is a thing that happened; losing it would make
        the history a record of successes only."""
        from spec_runner.lifecycle import IllegalTransition

        cfg = _cfg(_repo(tmp_path))
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            with pytest.raises(IllegalTransition):
                advance(state, ns, "TASK-001", TddPhase.GREEN_IMPLEMENTING)
            history = state.tdd_phase_history("TASK-001", ns)
        assert history and history[-1]["phase"] == "refused:green_implementing"


class TestRefactoringIsSkipped:
    """3a's whole point. The phase exists so the machine is complete; nothing
    runs, so no expensive stage arrives under a word nobody agreed to."""

    def test_entering_it_records_skipped(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            for phase in (
                TddPhase.RED_AUTHORING,
                TddPhase.RED_VERIFYING,
                TddPhase.GREEN_IMPLEMENTING,
                TddPhase.GREEN_VERIFYING,
                TddPhase.REFACTORING,
            ):
                advance(state, ns, "TASK-001", phase)
            history = state.tdd_phase_history("TASK-001", ns)
        refactoring = [h for h in history if h["phase"] == "refactoring"]
        assert refactoring and refactoring[-1]["detail"] == "skipped"

    def test_no_refactor_agent_exists_to_be_called(self):
        """Checkable, unlike a promise. When 3b arrives this guard is deleted,
        not widened."""
        repo_root = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            [
                "git",
                "grep",
                "-l",
                "--untracked",
                "-iE",
                "refactor_(agent|pass|prompt)",
                "--",
                "src/spec_runner",
            ],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        assert out.returncode in (0, 1), f"git grep failed: {out.stderr}"
        assert not out.stdout.strip(), (
            "something that looks like an automatic refactor pass exists: "
            f"{out.stdout.strip()} — 3b was not approved"
        )


@pytest.mark.slow
class TestARealRunWalksTheMachine:
    """The transitions have to be recorded by the run, not only by a module
    nobody calls."""

    def _run(self, tmp_path, monkeypatch, *, green_fails=False):
        from spec_runner import execution, hooks, tdd

        root = _repo(tmp_path)
        (root / "spec").mkdir(exist_ok=True)
        (root / "spec" / "tasks.md").write_text(
            "# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO\nEst: 1d\n\n- [ ] x\n"
        )
        (root / "spec" / ".gitignore").write_text(".executor-*\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "spec")
        cfg = _cfg(
            root,
            test_command="python -m pytest",
            lint_command="",
            auto_commit=True,
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
        )

        def _red(config, prompt, **kwargs):
            path = Path(config.project_root) / "tests" / "test_thing.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_thing():\n    assert False\n")
            return tdd.AgentCall(text="TDD_SELECTOR: tests/test_thing.py::test_thing")

        monkeypatch.setattr(tdd, "_run_agent", _red)
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(execution, "update_task_status", lambda *a, **k: True)

        class _Stop(Exception):
            pass

        if green_fails:
            monkeypatch.setattr(
                execution, "build_task_prompt", lambda *a, **k: (_ for _ in ()).throw(_Stop())
            )
        else:
            monkeypatch.setattr(execution, "build_task_prompt", lambda *a, **k: "prompt")
            monkeypatch.setattr(
                execution,
                "build_cli_invocation",
                lambda **k: type("I", (), {"argv": ["true"], "result_format": "text"})(),
            )
            monkeypatch.setattr(hooks, "post_done_hook", hooks.post_done_hook)

        from spec_runner.task import Task

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            with contextlib.suppress(_Stop):
                execution.execute_task(task, cfg, state)
            history = [
                h["phase"] for h in state.tdd_phase_history("TASK-001", resolve_namespace(cfg))
            ]
        return history

    def test_the_red_phases_are_recorded_by_the_run(self, tmp_path, monkeypatch):
        history = self._run(tmp_path, monkeypatch, green_fails=True)
        assert history[:2] == ["red_authoring", "red_verifying"]

    def test_green_implementing_follows_a_confirmed_red(self, tmp_path, monkeypatch):
        history = self._run(tmp_path, monkeypatch, green_fails=True)
        assert "green_implementing" in history
        assert history.index("red_verifying") < history.index("green_implementing")

    def test_no_refused_transition_on_the_ordinary_path(self, tmp_path, monkeypatch):
        """If the machine's rules and the real order disagreed, this is where
        it would show."""
        history = self._run(tmp_path, monkeypatch, green_fails=True)
        assert not [h for h in history if h.startswith("refused:")], history
