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

from spec_runner import tdd
from spec_runner.claims import (
    ClaimStatus,
    check_claims,
    record_verify_group_claims,
    release_claims,
)
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace


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
