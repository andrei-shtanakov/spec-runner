"""#576: `tdd complete` — the door for a tdd task delivered by hand.

The terminal gate refused TASK-001 of the #480 workstream; the work was then
finished by hand, reviewed in a PR and merged. Its lifecycle stayed at
`green_verifying` and its claims stayed active, and no door fitted: `release`
demands DONE, which only `post_done_hook` writes; `abandon` would record the
red as no good, which is false; `resume` and `repair` answer other states.

`complete` is a door with **checks**, not with trust. Before anything is
written: the red is an ancestor of the named commit, the named commit is an
ancestor of HEAD, the red's own selector **passes** when replayed against that
commit, and this task's claims are intact in it. Only then, in one
transaction: lifecycle DONE, claims released, remedy recorded.

What it deliberately does not check is the review verdict and the other
pre-terminal gates — the review happened outside, in the PR. The actor and the
reason record who answers for that (owner decision, 2026-09-23).

Not marked `slow`, unlike the replay-driving classes of
`test_post_green_resume.py`, and deliberately: this file is the only
coverage of an authority door that writes DONE and unlocks files, the `slow`
lane never runs in CI, and the whole file costs about ten seconds.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import ClaimStatus, record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import TddPhase, advance, current_phase
from spec_runner.remedy import (
    AGENT_MARKER,
    RemedyError,
    RemedyOperation,
    complete,
    release,
)
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace

TASK = "TASK-001"
REASON = "terminal gate refused on review; finished by hand, reviewed and merged in PR #556"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        # Outside the repo, so `git add -A` never commits the live state DB.
        "state_file": root.parent / ".state.db",
        "logs_dir": root.parent / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _checkpoint(cfg: ExecutorConfig, sha: str, selector: str, task_id: str = TASK):
    return RedCheckpoint(
        task_id=task_id,
        namespace=resolve_namespace(cfg),
        commit_sha=sha,
        baseline_sha=sha,
        selector=selector,
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-09-22T10:00:00",
    )


def _wedged(tmp_path: Path, *, green: str = "def value():\n    return 2\n"):
    """The #576 shape: a confirmed red, a paid run stopped at `green_verifying`
    by the terminal gate, and the green finished by hand on top of it.

    Returns the root, the config, the checkpoint and the SHA of the hand-made
    green. HEAD sits one commit past it — "merged, and master moved on".
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "README.md").write_text("base\n")
    _commit(root, "base")
    (root / "app.py").write_text("def value():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_x.py").write_text(
        "from app import value\n\n\ndef test_y():\n    assert value() == 2\n"
    )
    red_sha = _commit(root, "red")
    cfg = _cfg(root)
    checkpoint = _checkpoint(cfg, red_sha, "tests/test_x.py::test_y")
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(checkpoint)
        record_claims(cfg, state, checkpoint)
        for phase in (
            TddPhase.RED_AUTHORING,
            TddPhase.RED_VERIFYING,
            TddPhase.GREEN_IMPLEMENTING,
            TddPhase.GREEN_VERIFYING,
        ):
            advance(state, resolve_namespace(cfg), TASK, phase)

    (root / "app.py").write_text(green)
    green_sha = _commit(root, "green, by hand")
    (root / "CHANGES.md").write_text("later work\n")
    _commit(root, "master moves on")
    return root, cfg, checkpoint, green_sha


def _recorded(cfg: ExecutorConfig) -> tuple:
    """Everything `complete` may write, read back — to prove a refusal wrote none of it."""
    namespace = resolve_namespace(cfg)
    with ExecutorState(cfg) as state:
        return (
            state.tdd_phase_history(TASK, namespace),
            sorted(state.claims_for(namespace)),
            state.remedies(TASK, namespace),
        )


class TestTheReportedWedge:
    def test_release_refuses_and_complete_finishes_the_task(self, tmp_path):
        root, cfg, checkpoint, green_sha = _wedged(tmp_path)
        namespace = resolve_namespace(cfg)

        with ExecutorState(cfg) as state:
            with pytest.raises(RemedyError, match="has not reached DONE"):
                release(cfg, state, TASK, reason="the task is done")

            result = complete(cfg, state, TASK, green_sha, reason=REASON)

            assert result.operation is RemedyOperation.COMPLETE
            assert result.checkpoint_id == checkpoint.checkpoint_id
            assert result.outcome is RedOutcome.NOT_RED
            assert result.released == 1
            assert current_phase(state, namespace, TASK) is TddPhase.DONE
            assert state.active_claims(namespace) == []
            [claim] = state.claims_for(namespace, TASK)
            assert claim[3] == ClaimStatus.RELEASED.value
            [remedy] = state.remedies(TASK, namespace)
            assert remedy.operation is RemedyOperation.COMPLETE
            assert remedy.actor == "operator@example.com"
            assert remedy.reason == REASON
            done = state.tdd_phase_history(TASK, namespace)[-1]
            assert green_sha in (done["detail"] or "")

    def test_release_afterwards_sees_done(self, tmp_path):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        with ExecutorState(cfg) as state:
            complete(cfg, state, TASK, green_sha, reason=REASON)
            after = release(cfg, state, TASK, reason="nothing left to unlock")
        assert after.released == 0

    def test_the_repeat_is_already_applied_not_refused_as_done(self, tmp_path):
        """Owner decision: the already-applied check comes before the DONE
        refusal, or the second run of a successful command would be an error."""
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        with ExecutorState(cfg) as state:
            complete(cfg, state, TASK, green_sha, reason=REASON)
            again = complete(cfg, state, TASK, green_sha, reason=REASON)
            assert again.already_applied
            assert len(state.remedies(TASK, resolve_namespace(cfg))) == 1

    def test_head_itself_is_an_admissible_commit(self, tmp_path):
        root, cfg, _cp, _green = _wedged(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state:
            assert complete(cfg, state, TASK, head, reason=REASON).released == 1


class TestRefusalsWriteNothing:
    def test_an_ordinary_done_is_sent_to_release(self, tmp_path):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        with ExecutorState(cfg) as state:
            advance(state, resolve_namespace(cfg), TASK, TddPhase.DONE)
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="tdd release"):
            complete(cfg, state, TASK, green_sha, reason=REASON)
        assert _recorded(cfg) == before

    def test_still_red_at_the_commit_is_refused(self, tmp_path):
        root, cfg, _cp, _green = _wedged(tmp_path, green="def value():\n    return 3\n")
        before = _recorded(cfg)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="still fails"):
            complete(cfg, state, TASK, head, reason=REASON)
        assert _recorded(cfg) == before

    def test_a_commit_not_built_on_the_red_is_refused(self, tmp_path):
        root, cfg, checkpoint, _green = _wedged(tmp_path)
        base = _git(root, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", "-b", "side", base)
        (root / "app.py").write_text("def value():\n    return 2\n")
        side = _commit(root, "side")
        _git(root, "checkout", "-q", "-")
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="not an ancestor"):
            complete(cfg, state, TASK, side, reason=REASON)
        assert _recorded(cfg) == before

    def test_a_commit_head_does_not_contain_is_refused(self, tmp_path):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        _git(root, "checkout", "-q", "-b", "unmerged", green_sha)
        (root / "extra.py").write_text("x = 1\n")
        unmerged = _commit(root, "never merged")
        _git(root, "checkout", "-q", "-")
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="not in HEAD"):
            complete(cfg, state, TASK, unmerged, reason=REASON)
        assert _recorded(cfg) == before

    def test_an_unknown_commit_is_refused(self, tmp_path):
        root, cfg, _cp, _green = _wedged(tmp_path)
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="does not resolve"):
            complete(cfg, state, TASK, "0" * 40, reason=REASON)
        assert _recorded(cfg) == before

    def test_an_edited_frozen_test_is_refused(self, tmp_path):
        """The green passes because it weakened the evidence — the laundering
        the byte-lock exists to catch."""
        root, cfg, _cp, _green = _wedged(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_y():\n    assert True\n")
        weakened = _commit(root, "weaken the test")
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="test_x.py"):
            complete(cfg, state, TASK, weakened, reason=REASON)
        assert _recorded(cfg) == before

    def test_an_unverifiable_replay_writes_nothing_and_says_so(self, tmp_path):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        # A composite command cannot be narrowed to one selector — the replay
        # refuses to guess, which is "cannot tell", not "passed".
        composite = _cfg(root, test_command="python -m pytest && true")
        before = _recorded(cfg)
        with ExecutorState(composite) as state:
            result = complete(composite, state, TASK, green_sha, reason=REASON)
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.note
        assert _recorded(cfg) == before

    def test_a_skipped_test_is_not_a_green(self, tmp_path):
        """Acceptance review of this PR: `verify_red` reads a skipped test as
        `not_red` — safe for a red, where "not red" refuses, and exactly wrong
        here, where "not red" is the success. A `conftest.py` is not claimed,
        so an autouse skip leaves the frozen file untouched while the broken
        implementation stays broken. Passing is not enough; the selected test
        has to be shown to have run."""
        root, cfg, _cp, _green = _wedged(tmp_path, green="def value():\n    return 3\n")
        (root / "tests" / "conftest.py").write_text(
            "import pytest\n\n\n@pytest.fixture(autouse=True)\n"
            "def _skip():\n    pytest.skip('not today')\n"
        )
        skipped = _commit(root, "skip it instead")
        # `-v` prints the node id beside SKIPPED, which is what makes the
        # selection "proven" and the skip otherwise indistinguishable.
        verbose = _cfg(root, test_command="python -m pytest -v")
        before = _recorded(cfg)
        with ExecutorState(verbose) as state:
            result = complete(verbose, state, TASK, skipped, reason=REASON)
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert _recorded(cfg) == before

    def test_no_confirmed_red_is_refused(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init", "-q")
        (root / "a.txt").write_text("a\n")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=o@e", "-c", "user.name=o", "commit", "-qm", "base")
        cfg = _cfg(root)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="no confirmed red"):
            complete(cfg, state, TASK, "HEAD", reason=REASON)

    def test_a_reason_is_required(self, tmp_path):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="reason"):
            complete(cfg, state, TASK, green_sha, reason="  ")

    def test_refused_inside_an_agent(self, tmp_path, monkeypatch):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        monkeypatch.setenv(AGENT_MARKER, "1")
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="agent"):
            complete(cfg, state, TASK, green_sha, reason=REASON)

    def test_refused_while_the_executor_lock_is_held(self, tmp_path):
        from spec_runner.config import ExecutorLock

        root, cfg, _cp, green_sha = _wedged(tmp_path)
        lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
        assert lock.acquire()
        try:
            with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="running"):
                complete(cfg, state, TASK, green_sha, reason=REASON)
        finally:
            lock.release()


class TestOnlyAStandingRedIsEvidence:
    """Local review of this PR: `confirmed_reds` returns reds of any status
    (deliberately — `resume` needs the retired ones). Proving against a red
    the operator gave up, whose claims `abandon` already retired, made the
    claims check vacuous: nothing active, so nothing violated, so "intact" —
    and a test weakened after the abandon went through."""

    def test_an_abandoned_red_is_refused_even_with_a_weakened_test(self, tmp_path):
        from spec_runner.remedy import abandon

        root, cfg, checkpoint, _green = _wedged(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, TASK, checkpoint.checkpoint_id, reason="gave up on this red")
        (root / "tests" / "test_x.py").write_text("def test_y():\n    assert True\n")
        weakened = _commit(root, "weaken the test after the abandon")
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="no standing"):
            complete(cfg, state, TASK, weakened, reason=REASON)
        assert _recorded(cfg) == before

    def test_a_superseded_red_points_to_resume_and_works_after_it(self, tmp_path):
        from spec_runner.remedy import CheckpointStatus, resume

        root, cfg, checkpoint, green_sha = _wedged(tmp_path)
        namespace = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            state.set_checkpoint_status(
                namespace, checkpoint.checkpoint_id, CheckpointStatus.SUPERSEDED
            )
            state.supersede_claims(
                namespace, TASK, ClaimStatus.SUPERSEDED, checkpoint_id=checkpoint.checkpoint_id
            )
            with pytest.raises(RemedyError, match="tdd resume"):
                complete(cfg, state, TASK, green_sha, reason=REASON)
            resume(cfg, state, TASK, reason="green was established on this red")
            assert complete(cfg, state, TASK, green_sha, reason=REASON).released == 1

    def test_a_standing_red_with_no_active_claim_is_refused(self, tmp_path):
        """No lock means no byte evidence to prove anything against — an empty
        check must not read as an intact one."""
        root, cfg, checkpoint, green_sha = _wedged(tmp_path)
        with ExecutorState(cfg) as state:
            state.supersede_claims(
                resolve_namespace(cfg),
                TASK,
                ClaimStatus.SUPERSEDED,
                checkpoint_id=checkpoint.checkpoint_id,
            )
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="no active claim"):
            complete(cfg, state, TASK, green_sha, reason=REASON)
        assert _recorded(cfg) == before


class TestWhichClaimsCount:
    def test_another_tasks_broken_claim_does_not_block(self, tmp_path):
        """Owner decision: only this task's claims are checked. A neighbour's
        violated lock is the neighbour's problem, not a reason this task
        cannot close."""
        root, cfg, _cp, _green = _wedged(tmp_path)
        (root / "tests" / "test_other.py").write_text("def test_z():\n    assert False\n")
        other_red = _commit(root, "other red")
        other = _checkpoint(cfg, other_red, "tests/test_other.py::test_z", task_id="TASK-002")
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(other)
            record_claims(cfg, state, other)
        (root / "tests" / "test_other.py").write_text("def test_z():\n    assert True\n")
        head = _commit(root, "neighbour weakens its own test")

        with ExecutorState(cfg) as state:
            result = complete(cfg, state, TASK, head, reason=REASON)
            remaining = state.active_claims(resolve_namespace(cfg))
        assert result.released == 1
        assert [c.task_id for c in remaining] == ["TASK-002"]


class TestSeveralConfirmedReds:
    def _second_red(self, cfg: ExecutorConfig, root: Path) -> RedCheckpoint:
        (root / "tests" / "test_x2.py").write_text(
            "from app import value\n\n\ndef test_w():\n    assert value() == 2\n"
        )
        sha = _commit(root, "second red")
        second = _checkpoint(cfg, sha, "tests/test_x2.py::test_w")
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(second)
        return second

    def test_ambiguity_is_refused(self, tmp_path):
        root, cfg, _cp, _green = _wedged(tmp_path)
        self._second_red(cfg, root)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="--checkpoint"):
            complete(cfg, state, TASK, head, reason=REASON)

    def test_naming_one_resolves_it(self, tmp_path):
        root, cfg, checkpoint, _green = _wedged(tmp_path)
        self._second_red(cfg, root)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state:
            result = complete(
                cfg, state, TASK, head, reason=REASON, checkpoint_id=checkpoint.checkpoint_id
            )
        assert result.checkpoint_id == checkpoint.checkpoint_id


class TestAtomicity:
    def test_a_failed_write_rolls_all_three_back(self, tmp_path):
        """Owner decision: DONE, the release and the remedy row are one
        transaction. The fault is real SQLite, at the last of the three
        writes — so the two before it must be undone, not merely skipped."""
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        with ExecutorState(cfg) as state:
            assert state._conn is not None
            state._conn.execute(
                "CREATE TRIGGER no_remedy BEFORE INSERT ON tdd_remedies "
                "BEGIN SELECT RAISE(ABORT, 'disk on fire'); END"
            )
            state._conn.commit()
        before = _recorded(cfg)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="nothing was recorded"):
            complete(cfg, state, TASK, green_sha, reason=REASON)

        assert _recorded(cfg) == before
        with ExecutorState(cfg) as state:
            assert current_phase(state, resolve_namespace(cfg), TASK) is TddPhase.GREEN_VERIFYING


class TestTheCommand:
    def _run(self, cfg: ExecutorConfig, *argv: str) -> int:
        from spec_runner.cli import _build_parser
        from spec_runner.remedy import cmd_tdd

        args = _build_parser().parse_args(["tdd", "complete", *argv])
        return cmd_tdd(args, cfg)

    def test_success_exits_zero_and_says_what_it_did(self, tmp_path, capsys):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        code = self._run(cfg, TASK, "--commit", green_sha, "--reason", REASON)
        out = capsys.readouterr().out
        assert code == 0
        assert "Completed" in out and "1 claim" in out

    def test_a_refusal_exits_one_without_a_traceback(self, tmp_path, capsys):
        root, cfg, _cp, _green = _wedged(tmp_path)
        code = self._run(cfg, TASK, "--commit", "0" * 40, "--reason", REASON)
        assert code == 1
        assert "⛔" in capsys.readouterr().out

    def test_an_unverifiable_replay_exits_two(self, tmp_path, capsys):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        composite = _cfg(root, test_command="python -m pytest && true")
        code = self._run(composite, TASK, "--commit", green_sha, "--reason", REASON)
        assert code == 2
        assert "nothing was recorded" in capsys.readouterr().out.lower()

    def test_a_repeat_exits_zero(self, tmp_path, capsys):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        self._run(cfg, TASK, "--commit", green_sha, "--reason", REASON)
        assert self._run(cfg, TASK, "--commit", green_sha, "--reason", REASON) == 0
        assert "Already applied" in capsys.readouterr().out

    def test_tdd_status_stops_reporting_the_task_as_unfinished(self, tmp_path):
        """The symptom the issue opened with: `tdd status` printed `in green
        verifying` and a 🔒 about delivered work."""
        from spec_runner.tdd_status import collect, lifecycle_of, render

        root, cfg, _cp, green_sha = _wedged(tmp_path)
        assert "green verifying" in lifecycle_of(collect(cfg, TASK), TASK)

        assert self._run(cfg, TASK, "--commit", green_sha, "--reason", REASON) == 0
        data = collect(cfg, TASK)
        text = render(data, TASK)
        assert "done" in lifecycle_of(data, TASK)
        assert "🔒" not in text
        assert "complete" in text
