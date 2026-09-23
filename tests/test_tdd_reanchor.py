"""`tdd reanchor` — carry a confirmed red across a rebase.

Measured on TASK-001 of the #480 workstream: the confirmed red was recorded
on `08c44f4`, and the branch was rebased before PR #556 merged, so master
carries the same change as `1138199`. The test bytes under the claim are
identical (`e7a83383`) and so is `git patch-id --stable` — but every door
asks about ancestry: `complete` and `resume` want the red to be an ancestor,
and `repair` wants its baseline (the old red) to be one. After a rebase none
is, so a delivered task could not be closed.

`reanchor` moves the lineage to the rebased copy **only** when that copy is
provably the same change: a single parent on both sides, equal non-empty
patch-ids, byte-identical claimed files, and a replay that still fails on
the new commit. The lifecycle is not touched — green stays reached — and the
three writes (new checkpoint and claims, old ones superseded, the remedy row)
are one transaction. Owner decision, 2026-09-23.
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
    reanchor,
)
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace

TASK = "TASK-001"
REASON = (
    "restore the red lineage after the rebase of PR #556: patch-id equal, claim bytes unchanged"
)
SELECTOR = "tests/test_x.py::test_y"
TEST_BODY = "from app import value\n\n\ndef test_y():\n    assert value() == 2\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _head(root)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root.parent / ".state.db",
        "logs_dir": root.parent / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _repo(tmp_path: Path, *, test_in_base: bool = False) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "app.py").write_text("def value():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")
    if test_in_base:
        (root / "tests" / "test_x.py").write_text(
            "# header\n" + TEST_BODY.replace("== 2", "== 1") + "\n" * 12 + "# footer\n"
        )
    _commit(root, "base")
    return root


def _record_red(cfg: ExecutorConfig, red_sha: str) -> RedCheckpoint:
    """The paid run's record: a confirmed red on ``red_sha``, its claim, and a
    lifecycle stopped at `green_verifying` by the terminal gate."""
    checkpoint = RedCheckpoint(
        task_id=TASK,
        namespace=resolve_namespace(cfg),
        commit_sha=red_sha,
        baseline_sha=red_sha,
        selector=SELECTOR,
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-09-20T20:11:14",
    )
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
    return checkpoint


def _rebased(tmp_path: Path, *, master_change=None):
    """The #556 shape. The red is recorded on the task branch; master moves;
    the branch is rebased (the red becomes a new commit with the same
    change) and the green lands on top of it on main.

    Returns root, config, the old checkpoint, the rebased red and the green.
    """
    root = _repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "task")
    (root / "tests" / "test_x.py").write_text(TEST_BODY)
    old_red = _commit(root, "TASK-001: red")
    cfg = _cfg(root)
    checkpoint = _record_red(cfg, old_red)

    _git(root, "checkout", "-q", "main")
    if master_change is None:
        (root / "NOTES.md").write_text("master moved on\n")
    else:
        master_change(root)
    _commit(root, "master moves on")
    _git(root, "cherry-pick", old_red)
    new_red = _head(root)
    (root / "app.py").write_text("def value():\n    return 2\n\n\n# green\n")
    green = _commit(root, "TASK-001: green")
    return root, cfg, checkpoint, new_red, green


def _recorded(cfg: ExecutorConfig) -> tuple:
    namespace = resolve_namespace(cfg)
    with ExecutorState(cfg) as state:
        return (
            [cp.checkpoint_id for cp in state.active_checkpoints(namespace, TASK)],
            sorted(state.claims_for(namespace)),
            state.remedies(TASK, namespace),
            state.tdd_phase_history(TASK, namespace),
        )


class TestTheRebasedLineage:
    def test_the_lineage_moves_and_complete_then_closes_the_task(self, tmp_path):
        root, cfg, old, new_red, green = _rebased(tmp_path)
        namespace = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            with pytest.raises(RemedyError, match="not an ancestor"):
                complete(cfg, state, TASK, green, reason="before the reanchor")
            history_before = state.tdd_phase_history(TASK, namespace)

            result = reanchor(cfg, state, TASK, old.checkpoint_id, new_red, reason=REASON)

            assert result.operation is RemedyOperation.REANCHOR
            assert result.outcome is RedOutcome.EXPECTED_FAIL
            [standing] = state.active_checkpoints(namespace, TASK)
            assert standing.checkpoint_id == result.new_checkpoint_id
            assert standing.commit_sha == new_red
            assert standing.baseline_sha == _git(root, "rev-parse", f"{new_red}^").stdout.strip()
            assert standing.selector == SELECTOR

            claims = state.claims_for(namespace, TASK)
            old_claims = [c for c in claims if c[4] == old.checkpoint_id]
            new_claims = [c for c in claims if c[4] == standing.checkpoint_id]
            assert [c[3] for c in old_claims] == [ClaimStatus.SUPERSEDED.value]
            assert [c[3] for c in new_claims] == [ClaimStatus.ACTIVE.value]
            assert new_claims[0][1:3] == old_claims[0][1:3]  # same path, same bytes

            [remedy] = state.remedies(TASK, namespace)
            assert remedy.operation is RemedyOperation.REANCHOR
            assert remedy.new_checkpoint_id == standing.checkpoint_id

            # The lifecycle is untouched: green stays reached.
            assert state.tdd_phase_history(TASK, namespace) == history_before
            assert current_phase(state, namespace, TASK) is TddPhase.GREEN_VERIFYING

            closed = complete(cfg, state, TASK, green, reason="after the reanchor")
            assert closed.released == 1

    def test_a_repeat_is_already_applied(self, tmp_path):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        with ExecutorState(cfg) as state:
            first = reanchor(cfg, state, TASK, old.checkpoint_id, new_red, reason=REASON)
            again = reanchor(cfg, state, TASK, old.checkpoint_id, new_red, reason=REASON)
            assert again.already_applied
            assert again.new_checkpoint_id == first.new_checkpoint_id
            assert len(state.remedies(TASK, resolve_namespace(cfg))) == 1


class TestRefusalsWriteNothing:
    def _refused(self, cfg, checkpoint_id, commit, match):
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match=match):
            reanchor(cfg, state, TASK, checkpoint_id, commit, reason=REASON)
        assert _recorded(cfg) == before

    def test_a_stale_checkpoint_is_refused(self, tmp_path):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        self._refused(cfg, "0123456789ab", new_red, "not an active checkpoint")

    def test_an_unbroken_line_has_nothing_to_reanchor(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests" / "test_x.py").write_text(TEST_BODY)
        red = _commit(root, "red")
        cfg = _cfg(root)
        old = _record_red(cfg, red)
        (root / "NOTES.md").write_text("later\n")
        later = _commit(root, "later")
        self._refused(cfg, old.checkpoint_id, later, "already an ancestor")

    def test_a_different_change_is_refused_on_patch_id(self, tmp_path):
        root, cfg, old, _new_red, _green = _rebased(tmp_path)
        (root / "tests" / "test_x.py").write_text(TEST_BODY + "\n\ndef test_z():\n    pass\n")
        other = _commit(root, "a different red")
        self._refused(cfg, old.checkpoint_id, other, "patch-id")

    def test_changed_claim_bytes_are_refused_even_with_an_equal_patch_id(self, tmp_path):
        """The red *modifies* a file master also changed elsewhere: the diff
        (and so the patch-id) is the same, the resulting bytes are not."""
        root = _repo(tmp_path, test_in_base=True)
        _git(root, "checkout", "-q", "-b", "task")
        test = root / "tests" / "test_x.py"
        test.write_text(test.read_text().replace("== 1", "== 2"))
        old_red = _commit(root, "red")
        cfg = _cfg(root)
        old = _record_red(cfg, old_red)
        _git(root, "checkout", "-q", "main")
        test.write_text(test.read_text().replace("# footer", "# footer changed on master"))
        _commit(root, "master touches the same file elsewhere")
        _git(root, "cherry-pick", old_red)
        new_red = _head(root)
        assert _patch_id(root, old_red) == _patch_id(root, new_red)
        self._refused(cfg, old.checkpoint_id, new_red, "claimed bytes")

    def test_a_commit_head_does_not_contain_is_refused(self, tmp_path):
        root, cfg, old, _new_red, _green = _rebased(tmp_path)
        _git(root, "checkout", "-q", "-b", "elsewhere", "main~2")
        (root / "ELSEWHERE.md").write_text("never merged\n")
        _commit(root, "unmerged work")
        _git(root, "cherry-pick", old.commit_sha)
        stray = _head(root)
        _git(root, "checkout", "-q", "main")
        self._refused(cfg, old.checkpoint_id, stray, "not in HEAD")

    def test_an_empty_patch_id_is_refused(self, tmp_path):
        root, cfg, old, _new_red, _green = _rebased(tmp_path)
        _git(root, "commit", "-q", "--allow-empty", "-m", "empty")
        self._refused(cfg, old.checkpoint_id, _head(root), "empty patch-id")

    def test_a_merge_commit_is_refused(self, tmp_path):
        root, cfg, old, _new_red, _green = _rebased(tmp_path)
        _git(root, "checkout", "-q", "-b", "side", "main~1")
        (root / "SIDE.md").write_text("side\n")
        _commit(root, "side")
        _git(root, "checkout", "-q", "main")
        _git(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self._refused(cfg, old.checkpoint_id, _head(root), "single parent")

    def test_a_red_that_no_longer_fails_is_refused(self, tmp_path):
        """Master gained the implementation before the rebase: the rebased
        copy is the same change, but at it the test already passes — that is
        not a red, and a lineage cannot stand on it."""

        def implement(root: Path) -> None:
            (root / "app.py").write_text("def value():\n    return 2\n")

        root, cfg, old, new_red, _green = _rebased(tmp_path, master_change=implement)
        self._refused(cfg, old.checkpoint_id, new_red, "passes at")

    def test_refused_inside_an_agent(self, tmp_path, monkeypatch):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        monkeypatch.setenv(AGENT_MARKER, "1")
        self._refused(cfg, old.checkpoint_id, new_red, "agent")

    def test_refused_while_the_executor_lock_is_held(self, tmp_path):
        from spec_runner.config import ExecutorLock

        root, cfg, old, new_red, _green = _rebased(tmp_path)
        lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
        assert lock.acquire()
        try:
            self._refused(cfg, old.checkpoint_id, new_red, "running")
        finally:
            lock.release()

    def test_a_reason_is_required(self, tmp_path):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="reason"):
            reanchor(cfg, state, TASK, old.checkpoint_id, new_red, reason=" ")


class TestAnUnverifiableReplay:
    def test_writes_nothing_and_says_so(self, tmp_path):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        composite = _cfg(root, test_command="python -m pytest && true")
        before = _recorded(cfg)
        with ExecutorState(composite) as state:
            result = reanchor(composite, state, TASK, old.checkpoint_id, new_red, reason=REASON)
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.new_checkpoint_id is None
        assert _recorded(cfg) == before


class TestAtomicity:
    def test_a_failed_write_rolls_everything_back(self, tmp_path):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        with ExecutorState(cfg) as state:
            assert state._conn is not None
            state._conn.execute(
                "CREATE TRIGGER no_remedy BEFORE INSERT ON tdd_remedies "
                "BEGIN SELECT RAISE(ABORT, 'disk on fire'); END"
            )
            state._conn.commit()
        before = _recorded(cfg)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="nothing was recorded"):
            reanchor(cfg, state, TASK, old.checkpoint_id, new_red, reason=REASON)
        assert _recorded(cfg) == before


class TestTheCommand:
    def _run(self, cfg: ExecutorConfig, *argv: str) -> int:
        from spec_runner.cli import _build_parser
        from spec_runner.remedy import cmd_tdd

        args = _build_parser().parse_args(["tdd", "reanchor", *argv])
        return cmd_tdd(args, cfg)

    def test_success_exits_zero(self, tmp_path, capsys):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        code = self._run(
            cfg, TASK, "--checkpoint", old.checkpoint_id, "--commit", new_red, "--reason", REASON
        )
        assert code == 0
        assert "Reanchored" in capsys.readouterr().out

    def test_a_refusal_exits_one(self, tmp_path, capsys):
        root, cfg, old, _new_red, green = _rebased(tmp_path)
        code = self._run(
            cfg, TASK, "--checkpoint", old.checkpoint_id, "--commit", green, "--reason", REASON
        )
        assert code == 1
        assert "⛔" in capsys.readouterr().out

    def test_an_unverifiable_replay_exits_two(self, tmp_path, capsys):
        root, cfg, old, new_red, _green = _rebased(tmp_path)
        composite = _cfg(root, test_command="python -m pytest && true")
        code = self._run(
            composite,
            TASK,
            "--checkpoint",
            old.checkpoint_id,
            "--commit",
            new_red,
            "--reason",
            REASON,
        )
        assert code == 2
        assert "nothing was recorded" in capsys.readouterr().out.lower()

    def test_checkpoint_is_required(self):
        from spec_runner.cli import _build_parser

        with pytest.raises(SystemExit):
            _build_parser().parse_args(["tdd", "reanchor", TASK, "--commit", "x", "--reason", "r"])


def _patch_id(root: Path, commit: str) -> str:
    shown = _git(root, "show", commit).stdout
    out = subprocess.run(
        ["git", "patch-id", "--stable"], cwd=root, input=shown, capture_output=True, text=True
    ).stdout
    return out.split()[0] if out.strip() else ""
