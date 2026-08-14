"""#263 (F-37): repair's post-green guard read a phase row, not the evidence.

The agent implemented TASK-104's wiring, discovered empirically that the frozen
test asserts a shape the requirement contradicts, **reverted everything**, and
reported `TASK_BLOCKED` exactly as instructed ("Only an operator may abandon or
repair a claim"). The operator did what that message says — edited the frozen
test, committed, and ran `tdd repair`:

```
⛔ TASK-104 has reached green in this workstream: a red cannot be repaired once
   the implementation exists — the replay would pass … Use `tdd resume`
```

Every clause was false in that state. No implementation existed (reverted). The
replay would have *failed*, twice, at the selector. `resume` was the wrong door
— there was no green to resume past. All three doors closed around a working
tree that was honestly red, with an operator-authorized change to the evidence.

The guard asked `has_reached(GREEN_IMPLEMENTING)`, and "an implementation call
was started" is not "an implementation exists".

What the guard protected is real: a repair after a real green replays a test the
implementation now satisfies, and recording *that* retires the confirmed red the
task still needs — the #232 wedge. So the protection moves to where the answer
lives. The replay runs first, against the repaired commit, in a disposable
worktree; only `expected_fail` changes anything.

That reordering is also strictly safer than what it replaces. The old code
superseded the standing checkpoint and its claims **before** learning the
verdict, so a repair that failed to establish a red left the task with no
confirmed red at all. Now a repair that establishes nothing changes nothing.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import TddPhase, advance
from spec_runner.remedy import RemedyOperation, cmd_tdd, repair
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace

#: Fails without the implementation (ImportError *inside* the test, so a
#: failure and not a collection error), passes with it. A genuine
#: red/green pair, so "what makes the test pass" is the implementation itself
#: rather than a rewritten assertion.
EVIDENTIAL_TEST = "def test_y():\n    from impl import thing\n\n    assert thing() == 1\n"
IMPLEMENTATION = "def thing():\n    return 1\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="pytest",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _bed(tmp_path: Path) -> tuple[Path, ExecutorConfig, RedCheckpoint]:
    """A confirmed red on its own commit, claimed, lifecycle at RED."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    cfg = _cfg(root)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_x.py").write_text(EVIDENTIAL_TEST)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "red")
    red_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    checkpoint = RedCheckpoint(
        task_id="TASK-104",
        namespace=resolve_namespace(cfg),
        commit_sha=red_sha,
        baseline_sha=red_sha,
        selector="tests/test_x.py::test_y",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-08-13T00:00:00",
    )
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(checkpoint)
        record_claims(cfg, state, checkpoint)
        for phase in (TddPhase.RED_AUTHORING, TddPhase.RED_VERIFYING):
            advance(state, resolve_namespace(cfg), "TASK-104", phase)
    return root, cfg, checkpoint


def _attempted_and_reverted(root: Path, cfg: ExecutorConfig) -> str:
    """#263's state: the implementation was written, found impossible against
    the frozen test, and reverted. The phase row stays; the code does not."""
    with ExecutorState(cfg) as state:
        advance(state, resolve_namespace(cfg), "TASK-104", TddPhase.GREEN_IMPLEMENTING)
    # The operator's authorized change to the evidence — still red, and
    # honestly so: the implementation does not exist.
    (root / "tests" / "test_x.py").write_text(
        "def test_y():\n    from impl import thing\n\n    assert thing() == 2\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "operator: correct the asserted shape")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _a_real_green(root: Path, cfg: ExecutorConfig) -> str:
    """The other state: the implementation exists and the test passes on it."""
    with ExecutorState(cfg) as state:
        advance(state, resolve_namespace(cfg), "TASK-104", TddPhase.GREEN_IMPLEMENTING)
    (root / "tests" / "impl.py").write_text(IMPLEMENTATION)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "green")
    with ExecutorState(cfg) as state:
        advance(state, resolve_namespace(cfg), "TASK-104", TddPhase.GREEN_VERIFYING)
        # The wedge's own tail (#249): every retry re-enters red_authoring.
        advance(state, resolve_namespace(cfg), "TASK-104", TddPhase.RED_AUTHORING)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.slow
class TestARevertedAttemptKeepsTheDoorOpen:
    def test_the_repair_is_allowed_and_re_establishes_the_red(self, tmp_path):
        """#263's exact state. `green_implementing` is in the history and no
        implementation is in the tree, so the only honest judge is the replay —
        which fails, at the selector, as the operator said it would."""
        root, cfg, cp = _bed(tmp_path)
        repaired = _attempted_and_reverted(root, cfg)

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-104", cp.checkpoint_id, repaired, reason="shape")
            active = state.red_checkpoint("TASK-104", resolve_namespace(cfg))

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert result.new_checkpoint_id and result.new_checkpoint_id != cp.checkpoint_id
        assert active is not None and active.commit_sha == repaired

    def test_the_new_bytes_are_claimed(self, tmp_path):
        """The door reopening must not open a hole: the repaired evidence is
        locked exactly as the original was."""
        root, cfg, cp = _bed(tmp_path)
        repaired = _attempted_and_reverted(root, cfg)

        with ExecutorState(cfg) as state:
            repair(cfg, state, "TASK-104", cp.checkpoint_id, repaired, reason="shape")
            claims = state.active_claims(resolve_namespace(cfg))

        assert [c.checkpoint_sha for c in claims] == [repaired]


@pytest.mark.slow
class TestARealGreenIsStillRefused:
    def test_it_refuses_and_points_at_resume(self, tmp_path):
        """The protection #232 asked for, reached through the evidence: the
        test passes on that commit because the implementation makes it pass."""
        root, cfg, cp = _bed(tmp_path)
        head = _a_real_green(root, cfg)

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-104", cp.checkpoint_id, head, reason="fix it")

        assert result.outcome is RedOutcome.NOT_RED
        assert result.new_checkpoint_id is None
        assert result.note is not None and "tdd resume" in result.note

    def test_the_refusal_changes_nothing(self, tmp_path):
        """Strictly better than the order it replaces, which superseded the
        checkpoint and its claims *before* learning the verdict — and so left
        the task with no confirmed red at all. That was the wedge."""
        root, cfg, cp = _bed(tmp_path)
        head = _a_real_green(root, cfg)

        with ExecutorState(cfg) as state:
            before = [
                c.checkpoint_id
                for c in state.active_checkpoints(resolve_namespace(cfg), "TASK-104")
            ]
            repair(cfg, state, "TASK-104", cp.checkpoint_id, head, reason="fix it")

            after = [
                c.checkpoint_id
                for c in state.active_checkpoints(resolve_namespace(cfg), "TASK-104")
            ]
            claims = [c.path for c in state.active_claims(resolve_namespace(cfg))]
            remedies = state.remedies("TASK-104", resolve_namespace(cfg))

        assert after == before == [cp.checkpoint_id]
        assert claims == ["tests/test_x.py"]
        assert remedies == [], "a remedy that established nothing is not a remedy applied"

    def test_a_passing_test_without_a_green_says_something_else(self, tmp_path):
        """The phase history is advice, not verdict. With no green reached,
        `resume` would be the wrong door — and the note must not send an
        operator through it, which is the mirror of #263's own complaint."""
        root, cfg, cp = _bed(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_y():\n    assert True\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "operator: a test that passes")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-104", cp.checkpoint_id, head, reason="typo")

        assert result.outcome is RedOutcome.NOT_RED
        assert result.note is not None
        assert "tdd resume" not in result.note
        assert "untouched" in result.note

    def test_the_note_reports_what_was_observed_and_not_a_cause(self, tmp_path):
        """Copilot, PR #264. The code knows two things: the replay passed, and
        a verified green exists. It does **not** know that the implementation
        is what makes the test pass — an operator who repaired the test into a
        weaker one produces the same two observations. Naming the wrong cause
        with authority is precisely what the refusal this fixes did."""
        root, cfg, cp = _bed(tmp_path)
        head = _a_real_green(root, cfg)

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-104", cp.checkpoint_id, head, reason="fix it")

        assert result.note is not None
        assert "passes on the repaired commit" in result.note
        assert "verified green" in result.note
        assert "tdd resume" in result.note
        assert "makes it pass" not in result.note, "that is an inference, not an observation"

    def test_an_attempted_green_is_not_a_verified_one_in_the_advice_either(self, tmp_path):
        """The same distinction the verdict now makes, one level down. An
        operator whose repaired test passes after a *reverted* attempt has no
        green to resume past — `resume` would refuse them, correctly, and
        sending them there is exactly the closed-doors loop #263 reported."""
        root, cfg, cp = _bed(tmp_path)
        with ExecutorState(cfg) as state:
            advance(state, resolve_namespace(cfg), "TASK-104", TddPhase.GREEN_IMPLEMENTING)
        (root / "tests" / "test_x.py").write_text("def test_y():\n    assert True\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "operator: repaired it into a passing test")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-104", cp.checkpoint_id, head, reason="typo")

        assert result.note is not None and "tdd resume" not in result.note


@pytest.mark.slow
class TestTheCommandSaysWhatStillStands:
    def _args(self, **kw):
        return argparse.Namespace(tdd_command="repair", actor=None, **kw)

    def test_it_exits_2_and_names_the_standing_checkpoint(self, tmp_path, capsys):
        root, cfg, cp = _bed(tmp_path)
        head = _a_real_green(root, cfg)

        code = cmd_tdd(
            self._args(task_id="TASK-104", checkpoint=cp.checkpoint_id, commit=head, reason="fix"),
            cfg,
        )
        out = capsys.readouterr().out

        assert code == 2
        assert "Not repaired" in out and cp.checkpoint_id in out
        assert "did not establish a red" in out
        assert "tdd resume" in out
        assert "new lineage" not in out, "nothing was opened, so nothing may be announced"

    def test_a_successful_repair_still_reads_as_one(self, tmp_path, capsys):
        root, cfg, cp = _bed(tmp_path)
        repaired = _attempted_and_reverted(root, cfg)

        code = cmd_tdd(
            self._args(
                task_id="TASK-104", checkpoint=cp.checkpoint_id, commit=repaired, reason="shape"
            ),
            cfg,
        )
        out = capsys.readouterr().out

        assert code == 0
        assert "Repaired: new lineage" in out
        assert "Red re-confirmed" in out


@pytest.mark.slow
class TestALegacyNotRedLineage:
    """Databases written before #263 hold what this version will never write: a
    repair record pointing at a lineage that is not a confirmed red. Reading
    those rows is the whole reason the idempotent path still exists (Copilot,
    PR #264) — and it must not announce them with a tick over an exit code
    of 2, which is the same tick-then-refuse mismatch #263 is about."""

    def _legacy_repair(self, cfg: ExecutorConfig, cp: RedCheckpoint, commit: str) -> None:
        from spec_runner.remedy import RemedyRecord

        lineage = RedCheckpoint(
            task_id="TASK-104",
            namespace=resolve_namespace(cfg),
            commit_sha=commit,
            baseline_sha=cp.commit_sha,
            selector=cp.selector,
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=cp.config_hash,
            outcome=RedOutcome.NOT_RED,
            timestamp="2026-08-12T00:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(lineage)
            state.record_remedy(
                RemedyRecord(
                    namespace=resolve_namespace(cfg),
                    task_id="TASK-104",
                    checkpoint_id=cp.checkpoint_id,
                    operation=RemedyOperation.REPAIR,
                    reason="an older version's repair",
                    actor="operator@example.com",
                    timestamp="2026-08-12T00:00:00",
                    new_checkpoint_id=lineage.checkpoint_id,
                )
            )

    def test_the_repeat_does_not_read_as_success(self, tmp_path, capsys):
        root, cfg, cp = _bed(tmp_path)
        repaired = _attempted_and_reverted(root, cfg)
        self._legacy_repair(cfg, cp, repaired)

        code = cmd_tdd(
            argparse.Namespace(
                tdd_command="repair",
                task_id="TASK-104",
                checkpoint=cp.checkpoint_id,
                commit=repaired,
                reason="again",
                actor=None,
            ),
            cfg,
        )
        out = capsys.readouterr().out

        assert code == 2
        assert "✔️" not in out, "a tick over an exit code of 2 reads as a repair that worked"
        assert "Already applied" in out, "the record is still a fact"
        assert "did not establish a red" in out
        assert "stays gated" in out, "and say what that means for the task"


@pytest.mark.slow
class TestAnUnverifiableReplay:
    def test_it_changes_nothing_either(self, tmp_path, monkeypatch):
        """ "We could not look" is not "the repair failed" and certainly not
        "the repair worked" — and either way there is nothing to record. The
        old order had already retired the evidence by this point."""
        from spec_runner import remedy
        from spec_runner.tdd import RedVerification

        root, cfg, cp = _bed(tmp_path)
        repaired = _attempted_and_reverted(root, cfg)
        monkeypatch.setattr(
            remedy,
            "verify_red",
            lambda *a, **k: RedVerification(RedOutcome.UNVERIFIABLE, detail="runner not measured"),
        )

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-104", cp.checkpoint_id, repaired, reason="shape")
            active = [
                c.checkpoint_id
                for c in state.active_checkpoints(resolve_namespace(cfg), "TASK-104")
            ]

        assert result.operation is RemedyOperation.REPAIR
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.new_checkpoint_id is None
        assert result.note is not None and "runner not measured" in result.note
        assert active == [cp.checkpoint_id]
