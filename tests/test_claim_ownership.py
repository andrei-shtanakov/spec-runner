"""F-3: a claim belongs to a task, not to a byte pattern.

The battle test found TASK-002 authoring the *same* test content TASK-001 had
frozen: enforcement correctly saw no violation, TASK-002 got a confirmed red —
and **no claim of its own**, because `record_claims` skipped a `(path, blob)`
already claimed by anyone in the workstream. `tdd abandon TASK-001` then
released the file entirely while TASK-002's confirmed red still depended on it.

The contract's "one task's remedy does not release another task's independent
claim" was defeated not by the remedy but because the second claim was never
recorded.

Report: `docs/superpowers/specs/2026-08-11-tdd-battle-report.md`, F-3.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import ClaimStatus, check_claims, record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.remedy import abandon
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, resolve_namespace

pytestmark = pytest.mark.slow

SAME_BYTES = "def test_thing():\n    assert False\n"


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
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _commit(root: Path, files: dict[str, str], message: str = "c") -> str:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _checkpoint(
    cfg, sha, *, task, selector="tests/test_x.py::test_thing", stamp="00"
) -> RedCheckpoint:
    return RedCheckpoint(
        task_id=task,
        namespace=resolve_namespace(cfg),
        commit_sha=sha,
        baseline_sha=sha,
        selector=selector,
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash="h",
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp=f"2026-08-11T00:00:{stamp}",
    )


def _two_tasks_same_bytes(tmp_path):
    """Both tasks depend on the same file, at the same bytes."""
    root = _repo(tmp_path)
    cfg = _cfg(root)
    sha = _commit(root, {"tests/test_x.py": SAME_BYTES})
    first = _checkpoint(cfg, sha, task="TASK-001", stamp="01")
    second = _checkpoint(cfg, sha, task="TASK-002", stamp="02")
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(first)
        record_claims(cfg, state, first)
        state.record_red_checkpoint(second)
        record_claims(cfg, state, second)
    return root, cfg, first, second


class TestIdenticalBytesMakeIndependentClaims:
    def test_both_tasks_own_a_claim(self, tmp_path):
        root, cfg, _first, _second = _two_tasks_same_bytes(tmp_path)
        with ExecutorState(cfg) as state:
            owners = sorted(c.task_id for c in state.active_claims(resolve_namespace(cfg)))
        assert owners == ["TASK-001", "TASK-002"], (
            "the second task's dependency on the file was invisible"
        )

    def test_the_same_task_claiming_twice_is_still_idempotent(self, tmp_path):
        """Per-task identity must not become "record one row per attempt"."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": SAME_BYTES})
        cp = _checkpoint(cfg, sha, task="TASK-001")
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, cp)
            record_claims(cfg, state, cp)
            assert len(state.active_claims(resolve_namespace(cfg))) == 1


class TestARemedyReleasesOnlyItsOwnLineage:
    def test_abandoning_one_task_leaves_the_other_lock_standing(self, tmp_path):
        """The battle's exact sequence."""
        root, cfg, first, _second = _two_tasks_same_bytes(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", first.checkpoint_id, reason="battle")
            remaining = state.active_claims(resolve_namespace(cfg))
        assert [c.task_id for c in remaining] == ["TASK-002"]

    def test_the_file_stays_locked_while_any_claim_is_active(self, tmp_path):
        root, cfg, first, _second = _two_tasks_same_bytes(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", first.checkpoint_id, reason="battle")
        candidate = _commit(root, {"tests/test_x.py": "def test_thing():\n    assert True\n"})
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert [v.task_id for v in violations] == ["TASK-002"], (
            "one task giving up must not unlock a file another still depends on"
        )

    def test_a_remedy_retires_only_the_named_lineage(self, tmp_path):
        """A task can hold claims from more than one lineage after a repair;
        abandoning one must not sweep the others."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha_a = _commit(root, {"tests/test_a.py": SAME_BYTES})
        sha_b = _commit(root, {"tests/test_b.py": SAME_BYTES})
        older = _checkpoint(
            cfg, sha_a, task="TASK-001", selector="tests/test_a.py::test_thing", stamp="01"
        )
        newer = _checkpoint(
            cfg, sha_b, task="TASK-001", selector="tests/test_b.py::test_thing", stamp="02"
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(older)
            record_claims(cfg, state, older)
            state.record_red_checkpoint(newer)
            record_claims(cfg, state, newer)
            state.supersede_claims(
                resolve_namespace(cfg),
                "TASK-001",
                ClaimStatus.ABANDONED,
                checkpoint_id=older.checkpoint_id,
            )
            still = [c.path for c in state.active_claims(resolve_namespace(cfg))]
        assert still == ["tests/test_b.py"]


class TestConflictingBytesStillBlock:
    def test_a_different_content_claim_on_a_claimed_path_is_a_violation(self, tmp_path):
        root, cfg, _first, _second = _two_tasks_same_bytes(tmp_path)
        candidate = _commit(root, {"tests/test_x.py": "def test_thing():\n    assert 1\n"})
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert len(violations) == 2, "both owners' claims are violated by the same edit"


class TestTheOperatorCanSeeAndUseTheState:
    """F-5. The remedies require a `--checkpoint <id>` that no command printed;
    running them in the battle meant reading SQLite and re-deriving a SHA-256
    by hand. Evidence nobody can reach is not evidence."""

    def _args(self, **kw):
        import argparse

        kw.setdefault("json", False)
        kw.setdefault("task_id", None)
        return argparse.Namespace(**kw)

    def test_checkpoints_prints_the_id_the_remedies_ask_for(self, tmp_path, capsys):
        from spec_runner.tdd_status import cmd_tdd_checkpoints

        root, cfg, first, _second = _two_tasks_same_bytes(tmp_path)
        cmd_tdd_checkpoints(self._args(), cfg)
        assert first.checkpoint_id in capsys.readouterr().out

    def test_status_json_carries_checkpoints_claims_and_lifecycle(self, tmp_path, capsys):
        import json

        from spec_runner.tdd_status import cmd_tdd_status

        root, cfg, first, _second = _two_tasks_same_bytes(tmp_path)
        cmd_tdd_status(self._args(json=True), cfg)
        data = json.loads(capsys.readouterr().out)
        assert {c["checkpoint_id"] for c in data["active_checkpoints"]} >= {first.checkpoint_id}
        assert {c["path"] for c in data["claims"]} == {"tests/test_x.py"}

    def test_after_an_abandon_the_task_does_not_read_as_success(self, tmp_path, capsys):
        """The battle's complaint: plain `status` still said ✅ success."""
        from spec_runner.tdd_status import cmd_tdd_status, collect, lifecycle_of

        root, cfg, first, _second = _two_tasks_same_bytes(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", first.checkpoint_id, reason="battle")

        assert "needs RED authoring" in lifecycle_of(collect(cfg), "TASK-001")
        cmd_tdd_status(self._args(task_id="TASK-001"), cfg)
        out = capsys.readouterr().out
        assert "success" not in out.lower()
        assert "abandon" in out, "the remedy that changed the state must be visible"

    def test_a_remedy_can_omit_the_id_when_exactly_one_is_active(self, tmp_path, capsys):
        from spec_runner.remedy import cmd_tdd

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": SAME_BYTES})
        cp = _checkpoint(cfg, sha, task="TASK-001")
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(cp)
            record_claims(cfg, state, cp)

        code = cmd_tdd(
            self._args(
                tdd_command="abandon", task_id="TASK-001", checkpoint=None, reason="r", actor="ann"
            ),
            cfg,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert cp.checkpoint_id in out, "the chosen id must be printed, never silently assumed"

    def test_several_active_checkpoints_fail_closed(self, tmp_path, capsys):
        """ "Probably that one" is not a thing to guess about an authority
        decision."""
        from spec_runner.remedy import cmd_tdd

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha_a = _commit(root, {"tests/test_a.py": SAME_BYTES})
        sha_b = _commit(root, {"tests/test_b.py": SAME_BYTES})
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(
                _checkpoint(
                    cfg, sha_a, task="TASK-001", selector="tests/test_a.py::test_thing", stamp="01"
                )
            )
            state.record_red_checkpoint(
                _checkpoint(
                    cfg, sha_b, task="TASK-001", selector="tests/test_b.py::test_thing", stamp="02"
                )
            )

        code = cmd_tdd(
            self._args(
                tdd_command="abandon", task_id="TASK-001", checkpoint=None, reason="r", actor=None
            ),
            cfg,
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "2 active checkpoints" in out and "--checkpoint" in out

    def test_the_parser_itself_allows_omitting_the_checkpoint(self):
        """A handler test is not a parser test: mine passed `checkpoint=None`
        straight to `cmd_tdd`, so argparse still requiring the flag went
        unnoticed until a live run."""
        from spec_runner.cli import _build_parser

        args = _build_parser().parse_args(["tdd", "abandon", "TASK-001", "--reason", "r"])
        assert args.checkpoint is None

    def test_the_parser_still_requires_a_reason(self):
        from spec_runner.cli import _build_parser

        with pytest.raises(SystemExit):
            _build_parser().parse_args(["tdd", "abandon", "TASK-001"])
