"""#261 (F-36): a rejected red commit starved every later authoring pass.

The sequence, from the pilot:

1. Attempt 1 authored a red that also touched a file frozen by a completed
   task's stale claim (#260), so the gate rejected it — `the red commit
   violates an active claim` → `HOOK_FAILURE`. The rejected commit **stayed on
   the task branch**, with no checkpoint.
2. The stale claim was released.
3. `retry` re-entered red authoring on the same branch. The agent found the
   failing test already sitting in the tree, quite reasonably changed nothing,
   and the phase refused:

   ```
   ⛔ RED not confirmed: the authoring pass changed nothing, so there is no
      red commit to replay
   ```

Each cycle burned a paid authoring call and ended in the same place.

The residue is not the problem — discarding it would be. It is the agent's
work, and #231 is the standing lesson about a tool that deletes work it did not
like. What was missing is that nothing ever *looked* at it: the phase demanded
a fresh diff and would not consider a commit that was already there.

Adoption is deliberately narrow. A checkpoint recorded against the wrong commit
would be a claim about a tree nobody proposed, so all four conditions hold: the
tree is clean, HEAD's subject is exactly what this task's red commit writes for
the selector the agent just reported, and no checkpoint was ever recorded for
it in any status.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import (
    RedCheckpoint,
    RedOutcome,
    _config_hash,
    _unregistered_red,
    resolve_namespace,
    run_red_phase,
)

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
        "lint_command": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _repo_with_a_rejected_red(tmp_path: Path) -> tuple[Path, ExecutorConfig]:
    """The state attempt 1 left behind: the red is committed, on the branch,
    and no checkpoint records it."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    cfg = _cfg(root)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_thing.py").write_text(FAILING)
    _git(root, "add", "-A")
    # The subject `_commit_red` writes — this is what makes the commit
    # identifiable as this task's red rather than someone's ordinary work.
    _git(root, "commit", "-qm", f"TASK-104: red for {SELECTOR}")
    return root, cfg


def _agent_that_changes_nothing(monkeypatch, selector: str = SELECTOR):
    """What the pilot's agent did, and reasonably: the failing test it was
    asked for is already in the tree."""
    from spec_runner import tdd

    monkeypatch.setattr(
        tdd,
        "_run_agent",
        lambda config, prompt, **kw: tdd.AgentCall(text=f"TDD_SELECTOR: {selector}"),
    )


@pytest.mark.slow
class TestTheStarvation:
    def test_the_next_pass_now_reaches_a_verdict(self, tmp_path, monkeypatch):
        """#261 at the level it was reported: the retry after a rejected red."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        _agent_that_changes_nothing(monkeypatch)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert result.checkpoint is not None
        assert result.checkpoint.commit_sha == head, "the commit that was already there"

    def test_the_checkpoint_records_the_tree_it_was_authored_against(self, tmp_path, monkeypatch):
        """The baseline of an adopted red is the commit's own parent. Taken
        from HEAD, as the ordinary path does, it would be the red itself."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        _agent_that_changes_nothing(monkeypatch)
        parent = _git(root, "rev-parse", "HEAD^").stdout.strip()

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.checkpoint is not None
        assert result.checkpoint.baseline_sha == parent

    def test_it_says_it_adopted_rather_than_authored(self, tmp_path, monkeypatch):
        """An operator reading the log must not think a new red was written."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        _agent_that_changes_nothing(monkeypatch)
        said: list[str] = []

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state, log_progress=said.append)

        assert any("adopting" in line for line in said), said

    def test_the_claimed_file_is_frozen_as_usual(self, tmp_path, monkeypatch):
        """An adopted red is a red like any other: it locks its file, or the
        adoption would be a way to get a checkpoint without a byte-lock."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        _agent_that_changes_nothing(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        assert [c.path for c in claims] == ["tests/test_thing.py"]


@pytest.mark.slow
class TestTheConditionsAreNarrow:
    """Adopting the wrong commit would put a checkpoint on a tree nobody
    proposed — worse than the wedge it cures."""

    def test_work_that_could_not_be_committed_is_not_stepped_over(self, tmp_path, monkeypatch):
        """`_commit_red` returns "" for two different events, and only one of
        them may be adopted over. Here the agent *did* write a test and the
        commit failed — a hook, a lock, a bad identity — so the work exists,
        staged, and reaching back to an older commit would step over it.

        The hook is real rather than mocked: what is being checked is that a
        failed commit and an empty one are told apart, and mocking the commit
        would be assuming exactly that.
        """
        from spec_runner import tdd

        root, cfg = _repo_with_a_rejected_red(tmp_path)
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        def _write_more(config, prompt, **kw):
            path = Path(config.project_root) / "tests" / "test_thing.py"
            path.write_text(FAILING + "\n\ndef test_more():\n    assert False\n")
            return tdd.AgentCall(text=f"TDD_SELECTOR: {SELECTOR}")

        monkeypatch.setattr(tdd, "_run_agent", _write_more)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "could not be committed" in (result.detail or "")
        assert result.checkpoint is None

    def test_a_tree_with_something_to_commit_takes_the_ordinary_path(self, tmp_path, monkeypatch):
        """The complementary half: when there *is* a diff, the phase commits it
        and adoption never happens — so the residue can only be reached when
        nothing else could be.

        The new red goes to a **new** file, because since #252 D a red written
        into a file that existed at its baseline is refused — and the rejected
        red's file is exactly that.
        """
        from spec_runner import tdd

        root, cfg = _repo_with_a_rejected_red(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        def _write_more(config, prompt, **kw):
            path = Path(config.project_root) / "tests" / "test_task_104_red.py"
            path.write_text("def test_more():\n    assert False\n")
            return tdd.AgentCall(text="TDD_SELECTOR: tests/test_task_104_red.py::test_more")

        monkeypatch.setattr(tdd, "_run_agent", _write_more)
        said: list[str] = []

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state, log_progress=said.append)

        assert result.checkpoint is not None
        assert result.checkpoint.commit_sha != head
        assert not any("adopting" in line for line in said)

    def test_another_commit_is_not_adopted(self, tmp_path):
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        (root / "notes.md").write_text("ordinary work\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "docs: notes")

        with ExecutorState(cfg) as state:
            assert _unregistered_red(cfg, state, _task(), SELECTOR) == ""

    def test_another_tasks_red_is_not_adopted(self, tmp_path):
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        from spec_runner.task import Task

        other = Task(id="TASK-105", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            assert _unregistered_red(cfg, state, other, SELECTOR) == ""

    def test_a_different_selector_is_not_adopted(self, tmp_path):
        """An agent naming another test is not talking about this commit."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)

        with ExecutorState(cfg) as state:
            assert _unregistered_red(cfg, state, _task(), "tests/test_other.py::test_x") == ""

    def test_a_registered_commit_is_not_adopted(self, tmp_path):
        """A commit that already had a checkpoint is registered, whatever
        became of it. Re-adopting would re-litigate whatever retired it — an
        `abandon` undone by the next retry, silently."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        from spec_runner.remedy import CheckpointStatus

        checkpoint = RedCheckpoint(
            task_id="TASK-104",
            namespace=resolve_namespace(cfg),
            commit_sha=head,
            baseline_sha=head,
            selector=SELECTOR,
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(cfg),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-13T00:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(checkpoint)
            state.set_checkpoint_status(
                resolve_namespace(cfg), checkpoint.checkpoint_id, CheckpointStatus.ABANDONED
            )

            assert _unregistered_red(cfg, state, _task(), SELECTOR) == ""

    def test_an_abandoned_red_still_refuses_the_pass(self, tmp_path, monkeypatch):
        """The same statement one level up: after an `abandon`, an authoring
        pass that changes nothing is refused, exactly as before."""
        root, cfg = _repo_with_a_rejected_red(tmp_path)
        _agent_that_changes_nothing(monkeypatch)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        from spec_runner.remedy import CheckpointStatus

        checkpoint = RedCheckpoint(
            task_id="TASK-104",
            namespace=resolve_namespace(cfg),
            commit_sha=head,
            baseline_sha=head,
            selector=SELECTOR,
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(cfg),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-13T00:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(checkpoint)
            state.set_checkpoint_status(
                resolve_namespace(cfg), checkpoint.checkpoint_id, CheckpointStatus.ABANDONED
            )
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "no unregistered red commit" in (result.detail or "")


@pytest.mark.slow
class TestTheOrdinaryPathIsUnchanged:
    def test_an_authored_red_still_commits_and_replays(self, tmp_path, monkeypatch):
        """The adoption must not become the normal route. Here the agent
        writes the test, so there *is* a diff — and the commit it produces is
        the one that gets replayed."""
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
        before = _git(root, "rev-parse", "HEAD").stdout.strip()

        def _write(config, prompt, **kw):
            path = Path(config.project_root) / "tests" / "test_thing.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAILING)
            return tdd.AgentCall(text=f"TDD_SELECTOR: {SELECTOR}")

        monkeypatch.setattr(tdd, "_run_agent", _write)
        said: list[str] = []
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state, log_progress=said.append)

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert result.checkpoint is not None
        assert result.checkpoint.commit_sha != before
        assert result.checkpoint.baseline_sha == before, "the tree it was authored against"
        assert not any("adopting" in line for line in said)

    def test_a_confirmed_red_is_still_reused_without_a_call(self, tmp_path, monkeypatch):
        """The neighbouring shortcut (F-4) must keep taking precedence: a
        registered confirmed red costs no call at all, and adoption is for the
        case where there is no checkpoint to reuse."""
        from spec_runner import tdd

        root, cfg = _repo_with_a_rejected_red(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        checkpoint = RedCheckpoint(
            task_id="TASK-104",
            namespace=resolve_namespace(cfg),
            commit_sha=head,
            baseline_sha=head,
            selector=SELECTOR,
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(cfg),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-13T00:00:00",
        )
        called: list[str] = []
        monkeypatch.setattr(
            tdd,
            "_run_agent",
            lambda *a, **k: called.append("paid") or tdd.AgentCall(text=""),
        )

        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(checkpoint)
            record_claims(cfg, state, checkpoint)
            result = run_red_phase(_task(), cfg, state)

        assert called == [], "a reusable checkpoint must not pay for an authoring call"
        assert result.checkpoint is not None
        assert result.checkpoint.checkpoint_id == checkpoint.checkpoint_id


@pytest.mark.slow
class TestPreAuthoringAdoptionGuards:
    """#366 review: the pre-authoring adoption (#341 BEH-28) must not
    deadlock a residue that never reached lint, must not adopt over
    un-committed work, and must fall back to authoring on an unparseable
    selector."""

    def _lint_scripts(self, tmp_path):
        import shlex
        import sys

        scripts = tmp_path / "scripts"
        scripts.mkdir(exist_ok=True)
        check = scripts / "check.py"
        check.write_text(
            "import sys\nfrom pathlib import Path\n"
            "bad = any('AGENTWORD' in Path(p).read_text() for p in sys.argv[1:])\n"
            "sys.exit(1 if bad else 0)\n"
        )
        fix = scripts / "fix.py"
        fix.write_text("import sys\nsys.exit(0)\n")  # cures nothing
        q = shlex.quote
        return f"{q(sys.executable)} {q(str(check))}", f"{q(sys.executable)} {q(str(fix))}"

    def test_the_agent_round_still_runs_for_an_adopted_residue(self, tmp_path, monkeypatch):
        """Fix 1: a residue may have been rejected BEFORE lint ever ran; the
        BEH-07 round must stay reachable, or the first fix-proof finding
        deadlocks the task forever."""
        from spec_runner import tdd
        from spec_runner.tdd import resolve_namespace as _rns
        from spec_runner.tdd_runners import ADAPTERS

        root, _ = _repo_with_a_rejected_red(tmp_path)
        check_cmd, fix_cmd = self._lint_scripts(tmp_path)
        cfg = _cfg(
            root,
            lint_command=check_cmd,
            lint_command_declared=True,
            lint_fix_command=fix_cmd,
            lint_fix_command_declared=True,
        )
        # The residue must sit at THIS workstream's evidential path — the
        # ownership gate (#366 r3) sends anything else to the authoring path.
        evidential = str(ADAPTERS["pytest"].evidential_file("TASK-104", namespace=_rns(cfg)))
        red = root / evidential
        red.parent.mkdir(parents=True, exist_ok=True)
        red.write_text("def test_thing():  # AGENTWORD\n    assert False\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"TASK-104: red for {evidential}::test_thing", "--amend")

        rounds: list[str] = []

        def curing_round(config, prompt, **kwargs):
            rounds.append("round")
            red.write_text(red.read_text().replace("AGENTWORD", ""))
            return tdd.AgentCall(text="done")

        monkeypatch.setattr(tdd, "_run_agent", curing_round)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert rounds == ["round"], "the BEH-07 round must run for the adoption"
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail

    def test_a_dirty_tree_falls_back_to_authoring(self, tmp_path, monkeypatch):
        """Fix 2: newer un-committed bytes over the residue mean 'the commit
        fell over with work in flight' — the authoring path commits them;
        adopting HEAD would byte-lock bytes no commit holds."""
        from spec_runner import tdd

        root, _ = _repo_with_a_rejected_red(tmp_path)
        (root / "tests" / "test_thing.py").write_text(
            "def test_thing():\n    assert False  # newer authored bytes\n"
        )
        cfg = _cfg(root, lint_command="true", lint_command_declared=True)

        authored: list[str] = []

        def authoring(config, prompt, **kwargs):
            authored.append("author")
            return tdd.AgentCall(text=f"TDD_SELECTOR: {SELECTOR}")

        monkeypatch.setattr(tdd, "_run_agent", authoring)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert authored == ["author"], "a dirty tree must go the authoring path"
        # Downstream this synthetic setup then honestly refuses on #252 D
        # (the file predates the task in the baseline) — the pin here is that
        # adoption did NOT happen and the hanging bytes were committed by the
        # authoring path, not byte-locked from the working tree.
        assert result.checkpoint is None
        assert "already existed" in (result.detail or "")
        assert "newer authored bytes" in (
            subprocess.run(
                ["git", "show", "HEAD:tests/test_thing.py"],
                cwd=root,
                capture_output=True,
                text=True,
            ).stdout
        )

    def test_an_unparseable_residue_selector_falls_back_to_authoring(self, tmp_path, monkeypatch):
        """Fix 3: a residue whose subject selector the current adapter
        refuses is not adoptable — refusing outright would block the task on
        every retry, since HEAD never changes."""
        from spec_runner import tdd

        root, _ = _repo_with_a_rejected_red(tmp_path)
        _git(root, "commit", "-qm", "TASK-104: red for -k thing", "--amend")
        cfg = _cfg(root, lint_command="true", lint_command_declared=True)

        authored: list[str] = []
        fresh = "tests/test_fresh.py"

        def authoring(config, prompt, **kwargs):
            authored.append("author")
            (root / fresh).write_text("def test_fresh():\n    assert False\n")
            return tdd.AgentCall(text=f"TDD_SELECTOR: {fresh}::test_fresh")

        monkeypatch.setattr(tdd, "_run_agent", authoring)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert authored == ["author"], "an unparseable residue must not deadlock"
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
