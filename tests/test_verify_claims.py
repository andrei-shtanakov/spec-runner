"""BEH-26/BEH-27 (#367 FR-19, TASK-010): the declared `**Verifies:**` group of
a `verify_first` task is byte-locked for the duration of the task, and the
lock is released — for that task only — once the task reaches DONE.

Green-only reaches its DONE without ever authoring a red (BEH-20), so
`tdd.py::_judge_red_commit`'s `record_claims` call never runs for it. Without
FR-19, the group's evidential files stayed open through the paid
implementation pass that follows the live entry run — a pass that is
physically capable of rewriting exactly what the evidence proved, while
keeping it green, so neither the live re-verify of the candidate commit nor
the merge-time review would object. Only a byte-lock (`claim_blob_sha`, raw
bytes, no line-ending normalisation — the same rule the red path already
uses) catches that.

`record_verify_group_claims` (claims.py) is the mechanism BEH-26 exercises;
the existing `release_claims`/`_release_claims` (#260) is what BEH-27 relies
on once TASK-010 widens its call site to the green-on-entry path too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.claims import (
    ClaimStatus,
    ViolationKind,
    check_claims,
    record_claims,
    record_verify_group_claims,
    release_claims,
)
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.remedy import RemedyError, release
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": True,
        "run_review": False,
        "callback_url": "",
        "lint_command": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _base_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    (root / "tests" / "test_group.py").write_text("def test_it():\n    assert 2 + 2 == 4\n")
    (root / "spec").mkdir()
    (root / "spec" / "tasks.md").write_text(
        "# Tasks\n\n### TASK-101: verify-first task\nP1 | TODO Est: 1h\n"
    )
    (root / "spec" / ".gitignore").write_text(".executor-*\n")
    _commit(root, "base")
    return root


def _verify_first_task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-101",
        "name": "verify-first task",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py::test_it"],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _run(task: Task, config: ExecutorConfig, state: ExecutorState, *, agent_side_effect):
    """`execute_task` with the paid CLI call replaced by ``agent_side_effect``,
    same seam the red authoring pass uses (`tdd._run_agent`, refused here so a
    green-on-entry task that tries to author one fails the test loudly)."""
    with (
        patch("spec_runner.execution.update_task_status"),
        patch("spec_runner.execution.log_progress"),
        patch(
            "spec_runner.execution.build_cli_invocation",
            return_value=CliInvocation(["echo", "hi"], "text"),
        ),
        patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
        patch("spec_runner.execution.pre_start_hook", return_value=True),
        patch("spec_runner.execution._run_agent_process", side_effect=agent_side_effect),
    ):
        return execute_task(task, config, state)


def _completes(config, invocation):
    return MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)


class TestBEH26DeclaredGroupIsFrozenForTheDuration:
    """kind: integration — a green-on-entry verify-first task's declared
    group is byte-locked for the duration of the task."""

    def test_claim_exists_before_the_paid_implementation_call_runs(self, tmp_path, monkeypatch):
        root = _base_repo(tmp_path)
        red_agent = MagicMock(
            side_effect=AssertionError("BEH-26: no red authoring for a green group")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        task = _verify_first_task()
        config = _cfg(root)
        seen_claim_counts: list[int] = []

        def _observe_then_complete(cfg, invocation):
            seen_claim_counts.append(len(state.active_claims(resolve_namespace(cfg))))
            return _completes(cfg, invocation)

        with ExecutorState(config) as state:
            result = _run(task, config, state, agent_side_effect=_observe_then_complete)

        red_agent.assert_not_called()
        assert seen_claim_counts == [1], (
            "BEH-26: the declared group's file must already be claimed by the "
            "time the paid implementation call runs"
        )
        assert result is True, "an unmodified group must not be blocked by its own freeze"

    def test_a_rewritten_but_still_green_group_file_blocks_the_task(self, tmp_path, monkeypatch):
        root = _base_repo(tmp_path)
        red_agent = MagicMock(
            side_effect=AssertionError("BEH-26: no red authoring for a green group")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        def _rewrite_the_group(config, invocation):
            # Weakens the assertion while keeping the file green — a live
            # re-verify of the candidate commit finds nothing wrong with it
            # either; only the byte-lock can catch this.
            path = Path(config.project_root) / "tests" / "test_group.py"
            path.write_text("def test_it():\n    assert True\n")
            return _completes(config, invocation)

        task = _verify_first_task()
        config = _cfg(root)
        with ExecutorState(config) as state:
            result = _run(task, config, state, agent_side_effect=_rewrite_the_group)

        red_agent.assert_not_called()
        assert result is not True, (
            "BEH-26: a paid pass that rewrites the declared group's evidential "
            "file — even while keeping it green — must be caught by the claims "
            "gate before the terminal transition"
        )

    def test_deleting_a_group_file_is_also_caught(self, tmp_path, monkeypatch):
        root = _base_repo(tmp_path)
        monkeypatch.setattr(
            tdd, "_run_agent", MagicMock(side_effect=AssertionError("no red authoring"))
        )

        def _delete_the_group_file(config, invocation):
            (Path(config.project_root) / "tests" / "test_group.py").unlink()
            _git(Path(config.project_root), "add", "-A")
            return _completes(config, invocation)

        task = _verify_first_task()
        config = _cfg(root)
        with ExecutorState(config) as state:
            result = _run(task, config, state, agent_side_effect=_delete_the_group_file)

        assert result is not True


class TestBEH27FreezeLiftsOnDoneAndDoesNotTaxNeighbours:
    """kind: integration — the freeze is released at DONE (`release_claims`,
    #260), scoped to the task's own namespace, never earlier and never wider."""

    def test_claims_are_released_once_the_task_reaches_done(self, tmp_path, monkeypatch):
        root = _base_repo(tmp_path)
        monkeypatch.setattr(
            tdd, "_run_agent", MagicMock(side_effect=AssertionError("no red authoring"))
        )
        task = _verify_first_task()
        config = _cfg(root)
        namespace = resolve_namespace(config)

        with ExecutorState(config) as state:
            result = _run(task, config, state, agent_side_effect=_completes)
            active_after = state.active_claims(namespace)

        assert result is True
        assert active_after == [], (
            "BEH-27: a task that reached DONE must not leave its claims ACTIVE"
        )

    def test_a_same_namespace_neighbour_is_only_blocked_until_done(self, tmp_path, monkeypatch):
        root = _base_repo(tmp_path)
        monkeypatch.setattr(
            tdd, "_run_agent", MagicMock(side_effect=AssertionError("no red authoring"))
        )
        task = _verify_first_task()
        config = _cfg(root)
        namespace = resolve_namespace(config)

        with ExecutorState(config) as state:
            # Mid-task: the paid call is running, the claim is ACTIVE, and a
            # neighbour depending on the same file would be refused — proven
            # here by checking the candidate the group's own commit is on.
            captured_sha: list[str] = []

            def _observe_then_complete(cfg, invocation):
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=cfg.project_root,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                captured_sha.append(head)
                violations = check_claims(cfg, state, namespace, head)
                assert violations == [], "the group's own unmodified file is not a violation"
                assert state.active_claims(namespace), (
                    "BEH-27: before DONE the same file is still frozen to a neighbour"
                )
                return _completes(cfg, invocation)

            result = _run(task, config, state, agent_side_effect=_observe_then_complete)
            active_after = state.active_claims(namespace)

        assert result is True
        assert captured_sha, "the mid-task probe must have run"
        assert active_after == [], (
            "BEH-27: once DONE is reached the same-namespace neighbour is no "
            "longer taxed by this task's claim"
        )

    def test_release_is_scoped_to_its_own_namespace(self, tmp_path):
        root = _base_repo(tmp_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        config_a = _cfg(root, tdd_namespace="workstream-a")
        config_b = _cfg(root, tdd_namespace="workstream-b")
        task = _verify_first_task()
        selectors = ["tests/test_group.py::test_it"]

        with ExecutorState(config_a) as state:
            record_verify_group_claims(config_a, state, task, head, selectors)
            record_verify_group_claims(config_b, state, task, head, selectors)

            assert len(state.active_claims("workstream-a")) == 1
            assert len(state.active_claims("workstream-b")) == 1

            freed = release_claims(state, "workstream-a", task.id)

            assert freed == 1
            assert state.active_claims("workstream-a") == []
            assert len(state.active_claims("workstream-b")) == 1, (
                "BEH-27: releasing one namespace's claims must not release "
                "another workstream's claim on the very same path — claims "
                "are namespace-scoped, never repo-wide"
            )
            remaining = state.active_claims("workstream-b")[0]
            assert remaining.status == ClaimStatus.ACTIVE


class TestOperatorDoorForAnUnfinishedVerifyFreeze:
    """#383 review, finding 1: a green-on-entry `verify_first` task whose
    attempt never reaches DONE (budget cap, fatal `TASK_FAILED`, exhausted
    retries, a claims-gate refusal on a later attempt) leaves its declared
    group frozen with no operator door — `abandon`/`repair`/`resume` all
    require a `red_checkpoints` row that `record_verify_group_claims` never
    writes (its own docstring: a green entry is not "a red recorded under
    another name"), and `release` used to demand DONE unconditionally, so the
    only way out was hand-editing SQLite. `release` now admits a task whose
    active claims are all such verify-freeze claims — a `checkpoint_id` that
    resolves to nothing in `red_checkpoints` — whatever the lifecycle
    reached.
    """

    def test_release_unlocks_an_unfinished_verify_first_freeze(self, tmp_path):
        root = _base_repo(tmp_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        config = _cfg(root)
        task = _verify_first_task()
        namespace = resolve_namespace(config)

        with ExecutorState(config) as state:
            record_verify_group_claims(config, state, task, head, ["tests/test_group.py::test_it"])
            assert state.active_claims(namespace), "the freeze must have claimed something"

            result = release(config, state, task.id, reason="attempt never reached DONE")

        assert result.released == 1
        with ExecutorState(config) as state:
            assert state.active_claims(namespace) == []

    def test_release_still_refuses_a_genuine_unfinished_red(self, tmp_path):
        """The door must not widen past what it targets: a `tdd`-mode claim
        backed by a real confirmed red still demands DONE."""
        root = _base_repo(tmp_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        config = _cfg(root)
        namespace = resolve_namespace(config)
        checkpoint = RedCheckpoint(
            task_id="TASK-999",
            namespace=namespace,
            commit_sha=head,
            baseline_sha=head,
            selector="tests/test_group.py::test_it",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(config),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-09-06T00:00:00",
        )

        with ExecutorState(config) as state:
            state.record_red_checkpoint(checkpoint)
            record_claims(config, state, checkpoint)

            with pytest.raises(RemedyError, match="has not reached DONE"):
                release(config, state, "TASK-999", reason="premature")


class TestReEntryReusesRatherThanRebaselines:
    """#383 review round 2, finding 1 (major): superseding this task's own
    prior verify-freeze on every re-entry let a retry re-baseline the
    byte-lock onto the agent's own rewrite — the exact laundering BEH-26
    exists to catch. Scenario the review traced: attempt 1 freezes the group
    green at B1; the paid pass weakens that very test (still green);
    `wants_candidate` commits it as the candidate; a transient INSTRUMENT
    refusal (an unrunnable re-verify, hooks.py's re-verify sites) skips the
    claims check entirely and the attempt fails without ever judging the
    rewrite. Attempt 2 enters green off that same, now-weakened HEAD — the
    old (superseding) code re-baselined the lock onto the weakened bytes
    there, so `check_claims` judged the rewrite against itself and passed.

    The bytes the *first* green entry proved must stay locked for the life
    of the task: a re-entry reuses the standing freeze, and a judged tree
    that no longer matches it is a claims violation for `check_claims` to
    catch — not something `record_verify_group_claims` may silently
    re-baseline onto."""

    def test_a_second_freeze_reuses_the_standing_claim_rather_than_rebaselining(self, tmp_path):
        root = _base_repo(tmp_path)
        config = _cfg(root)
        namespace = resolve_namespace(config)
        task = _verify_first_task()
        selectors = ["tests/test_group.py::test_it"]
        sha0 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        with ExecutorState(config) as state:
            record_verify_group_claims(config, state, task, sha0, selectors)
            first_active = state.active_claims(namespace)
            assert len(first_active) == 1
            first_blob = first_active[0].blob_sha

            # Attempt 2: a paid pass weakened the group file and
            # `wants_candidate` committed it before a transient, unrelated
            # refusal skipped the claims check — HEAD moved, but the group's
            # evidential bytes were never actually judged against a lock.
            (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
            sha1 = _commit(root, "candidate from attempt 1 (weakened)")

            record_verify_group_claims(config, state, task, sha1, selectors)
            second_active = state.active_claims(namespace)

        assert len(second_active) == 1, "a re-entry must not stack a second claim either"
        assert second_active[0].blob_sha == first_blob, (
            "the bytes the first green entry proved must stay locked for the life of "
            "the task — a re-entry must reuse the standing freeze, never re-baseline "
            "it onto a later attempt's rewrite"
        )
        assert second_active[0].checkpoint_sha == sha0, (
            "the standing claim's own commit identity must not move either"
        )
        with ExecutorState(config) as state:
            # This is where the rewrite must be refused: a claims check
            # against the candidate the weakened test actually landed in.
            violations = check_claims(config, state, namespace, sha1)
        assert [v.path for v in violations] == ["tests/test_group.py"]
        assert violations[0].kind is ViolationKind.MODIFIED

    def test_the_first_freezes_claim_is_left_active_not_touched(self, tmp_path):
        root = _base_repo(tmp_path)
        config = _cfg(root)
        namespace = resolve_namespace(config)
        task = _verify_first_task()
        selectors = ["tests/test_group.py::test_it"]
        sha0 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        with ExecutorState(config) as state:
            record_verify_group_claims(config, state, task, sha0, selectors)
            (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
            sha1 = _commit(root, "candidate from attempt 1")
            record_verify_group_claims(config, state, task, sha1, selectors)

            rows = state.claims_for(namespace, task.id)

        assert [row[3] for row in rows] == [ClaimStatus.ACTIVE.value], (
            "a re-entry must not touch the standing freeze at all — supersession is "
            "for release/DONE, not for re-baselining a lock"
        )


class TestBEH26SurvivesAnInstrumentRefusalBetweenAttempts:
    """#383 review round 2, finding 1: the exact two-attempt shape the review
    traced end to end (evidence: `tests/test_verify_branching.py:306`'s own
    `TestBEH21ClaimsReleaseAcrossRetries`, reused here for the green-freeze
    path). Attempt 1 freezes the group green, its paid pass weakens the
    evidential test, `wants_candidate` commits that as the candidate, and a
    transient INSTRUMENT refusal (an unrunnable re-verify) ends the attempt
    before the claims check ever runs. Attempt 2 enters green off that same,
    already-weakened HEAD. The standing freeze from attempt 1 must still
    name attempt 1's original bytes — a rewrite the review would have caught
    must not slip through just because a later attempt's *own* entry run
    happened to read green on it.
    """

    def _post_done_hook_stub(self, root):
        from spec_runner.hooks import post_done_hook as _real_post_done_hook

        calls: list[int] = []

        def _stub(task, config, success, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                # Mirrors `wants_candidate` committing the paid pass's work
                # before a later gate/reverify/plugin refuses it
                # (hooks.py:1011 region) — the weakened test is real and in
                # HEAD by the time this attempt is recorded as failed.
                (root / "tests" / "test_group.py").write_text(
                    "def test_it():\n    assert True  # weakened by attempt 1\n"
                )
                _commit(root, "attempt 1: weakened, committed before refusing")
                return (
                    False,
                    "reverify could not be confirmed (infrastructure): transient worktree failure",
                    "skipped",
                    "",
                    False,
                )
            # Attempt 2 goes through the REAL hook — including the real
            # pre-terminal claims gate — so this test proves the standing
            # freeze actually stops the laundering at the gate, not merely
            # that the recorded claim data looks right in isolation.
            return _real_post_done_hook(task, config, success, *args, **kwargs)

        return _stub, calls

    def test_the_weakened_candidate_is_not_laundered_through_on_attempt_two(
        self, tmp_path, monkeypatch
    ):
        root = _base_repo(tmp_path)
        monkeypatch.setattr(
            tdd, "_run_agent", MagicMock(side_effect=AssertionError("no red authoring"))
        )
        stub, hook_calls = self._post_done_hook_stub(root)
        task = _verify_first_task()
        config = _cfg(root)
        namespace = resolve_namespace(config)

        with (
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution.log_progress"),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["echo", "hi"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
            patch("spec_runner.execution.post_done_hook", side_effect=stub),
            patch("spec_runner.execution.pre_start_hook", return_value=True),
            patch("spec_runner.execution._run_agent_process") as mock_run,
            ExecutorState(config) as state,
        ):
            mock_run.return_value = MagicMock(
                stdout="output TASK_COMPLETE", stderr="", returncode=0
            )

            attempt_1 = execute_task(task, config, state)
            assert attempt_1 is False, "attempt 1 must be the one that refuses"
            claim_after_1 = state.active_claims(namespace)
            assert len(claim_after_1) == 1, "attempt 1 must have frozen the group"
            locked_blob = claim_after_1[0].blob_sha

            head_after_1 = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.strip()
            committed_blob_after_1 = subprocess.run(
                ["git", "rev-parse", f"{head_after_1}:tests/test_group.py"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert locked_blob != committed_blob_after_1, (
                "the weakened bytes must really have landed in HEAD before attempt 1 "
                "refused — otherwise this is not the shape the review traced"
            )

            attempt_2 = execute_task(task, config, state)
            claim_after_2 = state.active_claims(namespace)

        assert len(hook_calls) == 2, "the fixture must actually walk both attempts"
        assert attempt_2 is not True, (
            "the real pre-terminal claims gate must refuse attempt 2 — the weakened "
            "test must not reach DONE just because attempt 2's own entry run read "
            "green on it"
        )
        assert claim_after_2 and claim_after_2[0].blob_sha == locked_blob, (
            "the standing freeze must still name attempt 1's original bytes, not the "
            "weakened candidate attempt 1 committed before refusing"
        )
        with ExecutorState(config) as state:
            head_after_2 = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.strip()
            violations = check_claims(config, state, namespace, head_after_2)
        assert violations, (
            "the weakened test must still read as a claims violation against the "
            "standing (attempt-1) freeze — attempt 2 must not have laundered it through"
        )


class TestFreezeBytesAreReadFromTheJudgedCommit:
    """#383 review, finding 2 (part b): the frozen bytes must be the ones the
    live entry run actually judged (`sha`'s tree) — the module's own stated
    rule, "authoritative against the candidate commit, never the working
    tree" (claims.py:11-14) — never whatever happens to be on disk when the
    freeze runs. Reproduces the gap directly: a stray, uncommitted edit left
    in the working tree (e.g. by an interrupted prior attempt) must not leak
    into the frozen blob."""

    def test_a_dirty_working_tree_does_not_leak_into_the_frozen_blob(self, tmp_path):
        root = _base_repo(tmp_path)
        config = _cfg(root)
        namespace = resolve_namespace(config)
        task = _verify_first_task()
        selectors = ["tests/test_group.py::test_it"]
        sha0 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        committed_blob = subprocess.run(
            ["git", "rev-parse", f"{sha0}:tests/test_group.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Residue from an interrupted paid call: the working tree is dirty
        # relative to HEAD, uncommitted.
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert 2 + 2 == 4  # stray edit\n"
        )

        with ExecutorState(config) as state:
            record_verify_group_claims(config, state, task, sha0, selectors)
            claim = state.active_claims(namespace)[0]

        assert claim.blob_sha == committed_blob, (
            "the frozen bytes must come from the judged commit, not the dirty working tree"
        )
