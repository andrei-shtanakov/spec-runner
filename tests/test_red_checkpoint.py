"""#141 slice 1b: verifying a RED and persisting the checkpoint.

A standalone, testable unit. Nothing calls it yet — wiring `tdd` mode into
execution arrives with the gate (1c), together, so the mode never exists in a
half-enforcing state where a red is recorded but green runs regardless.

What the design demands of a checkpoint (§3.3), and why each part is here:

    commit SHA   without it replay is impossible, and "red confirmed" is trust
                 in the agent's report — the thing this replaces
    selector     the *full* node-id: `-k` matches several tests, and a
                 checkpoint that matches several proves nothing about the one
    baseline     red *against what*
    namespace    identical TASK-NNN ids from different workstreams collide once
                 branches merge; the pilot nearly restored one task's claim
                 from another's honest red

Design: `docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md` §3.3, §3.7
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import (
    RedOutcome,
    environment_id,
    record_red_checkpoint,
    resolve_namespace,
    verify_red,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
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
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _commit_test(root: Path, body: str, name: str = "test_thing.py") -> str:
    (root / name).write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "red")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


FAILING = "def test_thing():\n    assert False, 'not implemented'\n"
PASSING = "def test_thing():\n    assert True\n"
BROKEN = "def test_thing(:\n"  # SyntaxError — a collection error, not a red


@pytest.mark.slow
class TestReplayDecidesWhatTheAgentClaims:
    def test_a_genuinely_failing_test_is_an_expected_fail(self, tmp_path):
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.EXPECTED_FAIL

    def test_a_passing_test_is_not_a_red(self, tmp_path):
        """The whole point. An agent reporting a red that passes on replay is
        exactly what the checkpoint exists to catch."""
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, PASSING)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.NOT_RED

    def test_a_test_that_cannot_be_collected_is_not_a_red_either(self, tmp_path):
        """A SyntaxError makes pytest exit non-zero, which looks like a failure
        and is not one: nothing was demonstrated about the behaviour."""
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, BROKEN)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        # What is promised is the exit code, not any particular pytest wording:
        # asserting on the tail of the output would pass or fail on phrasing.
        assert "exited 4" in (result.detail or "")

    def test_a_selector_matching_nothing_is_unverifiable(self, tmp_path):
        """Not NOT_RED: a selector that matches nothing did not demonstrate a
        passing test, it demonstrated nothing at all."""
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_absent", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "exited 4" in (result.detail or ""), (
            "measured, not assumed: an unresolvable node id is pytest's usage error (4), "
            "not the 'no tests collected' (5) one would guess"
        )


@pytest.mark.slow
class TestReplayIsDisposableAndLeavesNothing:
    def test_the_working_tree_is_untouched(self, tmp_path):
        """Replay must not be able to influence the run it is verifying."""
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        (root / "scratch.txt").write_text("uncommitted work\n")
        before = _git(root, "status", "--porcelain").stdout

        verify_red(_cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline)

        assert _git(root, "status", "--porcelain").stdout == before
        assert (root / "scratch.txt").read_text() == "uncommitted work\n"

    def test_no_worktree_is_left_registered(self, tmp_path):
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        verify_red(_cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline)
        listed = _git(root, "worktree", "list").stdout.strip().splitlines()
        assert len(listed) == 1, f"a disposable worktree outlived the replay: {listed}"

    def test_a_crash_mid_replay_still_removes_the_worktree(self, tmp_path, monkeypatch):
        from spec_runner import tdd

        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)

        def _boom(*a, **k):
            raise RuntimeError("test runner exploded")

        monkeypatch.setattr(tdd, "_run_selector", _boom)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert len(_git(root, "worktree", "list").stdout.strip().splitlines()) == 1


@pytest.mark.slow
class TestTheReplayJudgesTheCommitNotTheTree:
    def test_working_tree_edits_do_not_reach_the_replay(self, tmp_path):
        """Replay against a SHA is the entire reason the checkpoint commit
        exists. If the working tree leaked in, a red could be 'confirmed' by an
        edit that was never committed."""
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        # Make it pass in the working tree only.
        (root / "test_thing.py").write_text(PASSING)

        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.EXPECTED_FAIL, (
            "the committed tree is red; an uncommitted fix must not change that"
        )


class TestRefusalsThatNeedNoTestRun:
    def test_a_composite_test_command_is_unverifiable(self, tmp_path):
        """Same reasoning as #139's scoped tests: guessing which component of
        `a && b && c` takes a node-id is how you run the wrong program and
        believe its answer."""
        root = _repo(tmp_path)
        sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        result = verify_red(
            _cfg(root, test_command="ruff check . && python -m pytest"),
            sha=sha,
            selector="test_thing.py::test_thing",
            baseline_sha=sha,
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "composite" in (result.detail or "").lower()

    def test_a_selector_without_a_node_id_is_refused(self, tmp_path):
        """`-k`-style names match several tests; a checkpoint that matches
        several proves nothing about the one (§3.3)."""
        root = _repo(tmp_path)
        sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        result = verify_red(_cfg(root), sha=sha, selector="test_thing", baseline_sha=sha)
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "::" in (result.detail or "")

    def test_an_unknown_sha_is_unverifiable(self, tmp_path):
        root = _repo(tmp_path)
        result = verify_red(
            _cfg(root),
            sha="0" * 40,
            selector="test_thing.py::test_thing",
            baseline_sha="0" * 40,
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE


class TestEnvironmentIdentity:
    """§3.7: the replay environment must be *identifiable* by lockfile hash.
    The pilot shared the project venv and could not say what it had run in — a
    replay you cannot identify proves nothing about the run it reproduces."""

    def test_a_lockfile_is_hashed(self, tmp_path):
        root = _repo(tmp_path)
        (root / "uv.lock").write_text("version = 1\n")
        env = environment_id(root)
        assert env.startswith("uv.lock:") and len(env) > len("uv.lock:")

    def test_the_hash_changes_with_the_lockfile(self, tmp_path):
        root = _repo(tmp_path)
        (root / "uv.lock").write_text("version = 1\n")
        first = environment_id(root)
        (root / "uv.lock").write_text("version = 2\n")
        assert environment_id(root) != first

    def test_no_lockfile_is_recorded_as_unpinned_not_guessed(self, tmp_path):
        """Saying "unpinned" is honest; inventing an identity is not, and a
        project without a lockfile must still be able to use TDD mode."""
        assert environment_id(_repo(tmp_path)) == "unpinned"

    def test_the_first_known_lockfile_wins_deterministically(self, tmp_path):
        root = _repo(tmp_path)
        (root / "poetry.lock").write_text("a\n")
        (root / "uv.lock").write_text("b\n")
        assert environment_id(root).startswith("uv.lock:")


class TestNamespace:
    """§3.3: after several branches merge into one integration branch,
    identical TASK-NNN ids from different workstreams collide."""

    def test_two_workstreams_get_different_namespaces(self, tmp_path):
        a = _cfg(_repo(tmp_path / "a"))
        b = _cfg(_repo(tmp_path / "b"))
        assert resolve_namespace(a) != resolve_namespace(b)

    def test_the_namespace_is_stable_for_one_workstream(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        assert resolve_namespace(cfg) == resolve_namespace(cfg)

    def test_an_explicit_namespace_wins(self, tmp_path):
        """An orchestrator that knows its workstream identity should say so
        rather than have us infer it from a path."""
        cfg = _cfg(_repo(tmp_path), tdd_namespace="workstream-7")
        assert resolve_namespace(cfg) == "workstream-7"

    def test_the_spec_prefix_separates_phases_in_one_tree(self, tmp_path):
        root = _repo(tmp_path)
        assert resolve_namespace(_cfg(root)) != resolve_namespace(_cfg(root, spec_prefix="phase2-"))


class TestPersistence:
    def _cp(self, tmp_path, **overrides):
        from spec_runner.tdd import RedCheckpoint

        defaults: dict = {
            "task_id": "TASK-001",
            "namespace": "ws-a",
            "commit_sha": "a" * 40,
            "baseline_sha": "b" * 40,
            "selector": "tests/test_x.py::test_y",
            "environment_id": "uv.lock:abc123",
            "execution_mode": "tdd",
            "config_hash": "cfg1",
        }
        defaults.update(overrides)
        return RedCheckpoint(**defaults)

    def test_a_checkpoint_round_trips(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            record_red_checkpoint(state, self._cp(tmp_path))
            found = state.red_checkpoint("TASK-001", "ws-a")
        assert found is not None
        assert found.selector == "tests/test_x.py::test_y"
        assert found.environment_id == "uv.lock:abc123"

    def test_another_workstreams_checkpoint_is_not_this_ones(self, tmp_path):
        """The collision the pilot hit: same task id, different workstream."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            record_red_checkpoint(state, self._cp(tmp_path, namespace="ws-a"))
            assert state.red_checkpoint("TASK-001", "ws-b") is None

    def test_the_latest_checkpoint_wins(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            record_red_checkpoint(state, self._cp(tmp_path, selector="tests/a.py::first"))
            record_red_checkpoint(state, self._cp(tmp_path, selector="tests/a.py::second"))
            assert state.red_checkpoint("TASK-001", "ws-a").selector == "tests/a.py::second"

    def test_it_survives_a_reopen(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            record_red_checkpoint(state, self._cp(tmp_path))
        with ExecutorState(cfg) as state:
            assert state.red_checkpoint("TASK-001", "ws-a") is not None

    def test_the_mode_and_config_hash_are_stored(self, tmp_path):
        """Owner amendment 4: a checkpoint written under one policy must be
        distinguishable from one written under another, or replay silently
        re-interprets old evidence under today's rules."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            record_red_checkpoint(state, self._cp(tmp_path, execution_mode="tdd", config_hash="h1"))
            found = state.red_checkpoint("TASK-001", "ws-a")
        assert found.execution_mode == "tdd" and found.config_hash == "h1"


@pytest.mark.slow
class TestTheSelectorIsUntrustedInput:
    """The selector comes from an agent's output. Interpolated raw into a
    `shell=True` command it is not a test id, it is a shell command the harness
    runs on the operator's machine."""

    def test_a_selector_cannot_execute_a_second_command(self, tmp_path):
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        canary = root / "pwned.txt"

        result = verify_red(
            _cfg(root),
            sha=sha,
            selector=f"test_thing.py::test_thing; touch {canary}",
            baseline_sha=baseline,
        )

        assert not canary.exists(), "the selector was executed as a shell command"
        # Non-vacuity: the replay really ran and pytest really received the
        # whole string as one unresolvable node id, rather than the command
        # having failed earlier for some unrelated reason.
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "exited 4" in (result.detail or "")

    def test_the_quoting_does_not_break_an_ordinary_selector(self, tmp_path):
        """Quoting must not be paid for with a broken happy path."""
        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline
        )
        assert result.outcome is RedOutcome.EXPECTED_FAIL


@pytest.mark.slow
class TestTheBaselineIsChecked:
    """`baseline_sha` is the checkpoint's "red *against what*". A pair whose red
    does not descend from its claimed baseline is a false record — refusing is
    cheaper than storing it as evidence."""

    def test_a_baseline_that_is_not_an_ancestor_is_refused(self, tmp_path):
        root = _repo(tmp_path)
        _git(root, "checkout", "-q", "-b", "other")
        (root / "other.txt").write_text("x\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "unrelated")
        unrelated = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", "-")
        sha = _commit_test(root, FAILING)

        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=unrelated
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "ancestor" in (result.detail or "")

    def test_a_commit_is_its_own_baseline(self, tmp_path):
        root = _repo(tmp_path)
        sha = _commit_test(root, FAILING)
        result = verify_red(
            _cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=sha
        )
        assert result.outcome is RedOutcome.EXPECTED_FAIL


@pytest.mark.slow
class TestCleanupFailureIsNotSwallowed:
    def test_a_failed_removal_is_logged_and_pruned(self, tmp_path, monkeypatch):
        from spec_runner import tdd

        root = _repo(tmp_path)
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        sha = _commit_test(root, FAILING)

        real_run = subprocess.run
        pruned: list = []

        def _fake(cmd, *a, **k):
            if isinstance(cmd, list) and cmd[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(cmd, 1, "", "permission denied")
            if isinstance(cmd, list) and cmd[:3] == ["git", "worktree", "prune"]:
                pruned.append(cmd)
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(tdd.subprocess, "run", _fake)
        verify_red(_cfg(root), sha=sha, selector="test_thing.py::test_thing", baseline_sha=baseline)

        assert pruned, "a removal failure must be followed by a prune, not ignored"
        # The real worktree is still registered because removal was faked out;
        # clean it up so the temp repo is not left in a broken state.
        real_run(["git", "worktree", "prune"], cwd=root, capture_output=True)


class TestTheReadPatternIsIndexed:
    def test_an_index_covers_task_and_namespace(self, tmp_path):
        root = _repo(tmp_path)
        with ExecutorState(_cfg(root)) as state:
            rows = state._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='red_checkpoints'"
            ).fetchall()
        assert any("red_checkpoints" in r[0] for r in rows), (
            "the (task_id, namespace) lookup would degrade to a table scan"
        )
