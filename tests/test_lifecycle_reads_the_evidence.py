"""#253 (F-31): the machine asserted "no confirmed red" while one sat in the DB.

The run that finally completed did so over a trail of refusals:

```
♻️  RED: reusing the confirmed red a9a0a5a0a1a8
[warning] Lifecycle transition refused: TASK-101 cannot move from red_authoring
          to green_implementing: GREEN requires a confirmed red
…
✅ Code review passed
[warning] Lifecycle transition refused: … to green_verifying: GREEN requires a
          confirmed red
✅ Completed in 126.2s
```

The contract this machine enforces is a statement about **evidence** — *GREEN
may not be reached without a confirmed red* — and the code read it off the
previous **row**. Those differ constantly, and not only after a resume:

- a **reused** red records `red_authoring` and no verification, because there
  is nothing to replay;
- a **resumed** red reinstates the checkpoint without replaying either.

Both then move to GREEN legitimately, and both were refused in the same words
while the red they were denied sat in the database.

The consequence was not cosmetic. The refusal is non-fatal, so the run
continued and simply **stopped recording green phases** — and phase history is
exactly what `has_reached` reads, which is the admissibility check #249 had
just fixed. A task resumed into a fresher state file would have flunked it.

The resolution of "guard or shrug": the **gate** is the guard — it refuses to
implement without a confirmed red, holding the candidate SHA it needs to judge
coverage. This machine records. After this fix a refusal here means the record
and the gate disagree about the same task, which is logged as an error and
still never fails the task, because bookkeeping that can fail work is a second,
weaker gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import (
    ILLEGAL,
    IllegalTransition,
    TddPhase,
    advance,
    has_confirmed_red,
)
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace


def _bed(tmp_path: Path) -> tuple[ExecutorConfig, str, str]:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "o@e.c"),
        ("config", "user.name", "O"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
    cfg = ExecutorConfig(
        project_root=root,
        state_file=tmp_path / ".state.db",
        logs_dir=tmp_path / ".logs",
        execution_mode="tdd",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    return cfg, resolve_namespace(cfg), sha


def _confirmed_red(cfg: ExecutorConfig, ns: str, sha: str, outcome=RedOutcome.EXPECTED_FAIL):
    return RedCheckpoint(
        task_id="TASK-1",
        namespace=ns,
        commit_sha=sha,
        baseline_sha=sha,
        selector="tests/t.py::t",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=outcome,
        timestamp="2026-08-13T00:00:00",
    )


class TestTheRuleIsAboutEvidence:
    def test_a_reused_red_may_move_to_green(self, tmp_path):
        """No verification row exists — there was nothing to replay — and the
        transition is legal because the evidence is there."""
        cfg, ns, sha = _bed(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_confirmed_red(cfg, ns, sha))
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)

            advance(state, ns, "TASK-1", TddPhase.GREEN_IMPLEMENTING)

            phases = [r["phase"] for r in state.tdd_phase_history("TASK-1", ns)]
        assert phases == ["red_authoring", "green_implementing"]

    def test_without_a_red_it_is_still_refused(self, tmp_path):
        """The contract itself is unchanged — this is the transition the module
        exists to refuse."""
        cfg, ns, _sha = _bed(tmp_path)
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)

            with pytest.raises(IllegalTransition, match="GREEN requires a confirmed red"):
                advance(state, ns, "TASK-1", TddPhase.GREEN_IMPLEMENTING)

            phases = [r["phase"] for r in state.tdd_phase_history("TASK-1", ns)]
        assert phases == ["red_authoring", "refused:green_implementing"]

    def test_a_red_that_was_not_confirmed_does_not_count(self, tmp_path):
        """`not_red` and `unverifiable` are records of a red that failed to be
        one. Only `expected_fail` is evidence."""
        cfg, ns, sha = _bed(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_confirmed_red(cfg, ns, sha, RedOutcome.NOT_RED))
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)

            with pytest.raises(IllegalTransition):
                advance(state, ns, "TASK-1", TddPhase.GREEN_IMPLEMENTING)

    def test_a_retired_red_does_not_count(self, tmp_path):
        """`red_checkpoint` returns the **active** lineage: a red abandoned or
        superseded is history, not standing evidence."""
        from spec_runner.remedy import CheckpointStatus

        cfg, ns, sha = _bed(tmp_path)
        checkpoint = _confirmed_red(cfg, ns, sha)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(checkpoint)
            state.set_checkpoint_status(ns, checkpoint.checkpoint_id, CheckpointStatus.ABANDONED)
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)

            assert has_confirmed_red(state, ns, "TASK-1") is False
            with pytest.raises(IllegalTransition):
                advance(state, ns, "TASK-1", TddPhase.GREEN_IMPLEMENTING)

    def test_another_tasks_red_is_not_this_tasks_evidence(self, tmp_path):
        cfg, ns, sha = _bed(tmp_path)
        other = _confirmed_red(cfg, ns, sha)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(other)

            assert has_confirmed_red(state, ns, "TASK-2") is False

    def test_the_illegal_pairs_are_unchanged(self):
        """The contract's shape stays put; only the question asked about it
        changed. A future edit that widened `ILLEGAL` would be a policy change
        and should have to say so."""
        assert (TddPhase.RED_AUTHORING, TddPhase.GREEN_IMPLEMENTING) in ILLEGAL
        assert (TddPhase.READY, TddPhase.GREEN_VERIFYING) in ILLEGAL
        assert (TddPhase.RED_VERIFYING, TddPhase.GREEN_IMPLEMENTING) not in ILLEGAL


class TestTheHistoryKeepsBeingWritten:
    def test_green_rows_survive_a_resumed_run(self, tmp_path):
        """The consequence that made this more than cosmetic: `has_reached`
        (#249) reads these rows, so a run whose greens went unrecorded would
        make the *next* resume inadmissible."""
        from spec_runner.lifecycle import has_reached

        cfg, ns, sha = _bed(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_confirmed_red(cfg, ns, sha))
            # A resumed run: authoring, then straight to green (the red was
            # reinstated, not replayed).
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)
            advance(state, ns, "TASK-1", TddPhase.GREEN_IMPLEMENTING)
            advance(state, ns, "TASK-1", TddPhase.GREEN_VERIFYING)

            assert has_reached(state, ns, "TASK-1", TddPhase.GREEN_IMPLEMENTING) is True

    def test_a_refusal_is_still_recorded(self, tmp_path):
        """A refused transition is a thing that happened."""
        cfg, ns, _sha = _bed(tmp_path)
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)
            with pytest.raises(IllegalTransition):
                advance(state, ns, "TASK-1", TddPhase.GREEN_VERIFYING)

            rows = [r["phase"] for r in state.tdd_phase_history("TASK-1", ns)]
        assert "refused:green_verifying" in rows

    def test_a_refusal_is_logged_once(self, tmp_path, monkeypatch):
        """One event, one line, at the severity the machine chose (Copilot,
        PR #259). Three call sites used to re-log the same refusal as a
        warning, so a contract violation arrived twice and disagreed with
        itself about how serious it was.

        The loggers are recorded rather than stderr: a first version read the
        rendered output and passed alone but failed inside the full suite,
        because another test had reconfigured structlog. A test whose verdict
        depends on who ran before it is not evidence.
        """
        from spec_runner import execution, hooks, lifecycle, tdd
        from spec_runner.task import Task

        emitted: list[tuple[str, str]] = []

        class _Recorder:
            def __getattr__(self, level):
                def log(event, **kw):
                    emitted.append((level, event))

                return log

        for module in (lifecycle, execution, tdd, hooks):
            monkeypatch.setattr(module, "logger", _Recorder())

        cfg, ns, _sha = _bed(tmp_path)
        task = Task(id="TASK-1", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)
            emitted.clear()
            execution._record_phase(state, cfg, task, TddPhase.GREEN_IMPLEMENTING)

        refusals = [(lvl, ev) for lvl, ev in emitted if "refused" in ev.lower()]
        assert refusals == [("error", "Lifecycle transition refused")], emitted

    def test_a_refusal_never_fails_the_task(self, tmp_path):
        """`_record_phase` swallows it by design: the gates decide, this
        remembers. Bookkeeping that can fail work is a second, weaker gate —
        and the *gate* has already refused this case anyway."""
        from spec_runner import execution
        from spec_runner.task import Task

        cfg, ns, _sha = _bed(tmp_path)
        task = Task(id="TASK-1", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            advance(state, ns, "TASK-1", TddPhase.RED_AUTHORING)

            # No exception reaches the caller.
            execution._record_phase(state, cfg, task, TddPhase.GREEN_IMPLEMENTING)
