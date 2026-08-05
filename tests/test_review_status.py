"""Intermediate 🔍 REVIEW file status (#66): DONE stops lying about review."""

import contextlib
from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import post_done_hook
from spec_runner.state import ReviewVerdict
from spec_runner.task import (
    Task,
    get_next_tasks,
    parse_tasks,
    resolve_dependencies,
    update_task_status,
)
from spec_runner.validate import validate_task_fields

TASKS_MD = (
    "## Milestone 1: M\n\n"
    "### TASK-001: First\n"
    "🔴 P0 | 🔍 REVIEW | Est: 1d\n\n"
    "**Checklist:**\n- [x] a\n\n"
    "**Depends on:** —\n"
    "**Blocks:** [TASK-002]\n\n"
    "### TASK-002: Second\n"
    "🟠 P1 | ⏸️ BLOCKED | Est: 1d\n\n"
    "**Checklist:**\n- [ ] b\n\n"
    "**Depends on:** [TASK-001]\n"
)


def _write(tmp_path: Path, body: str = TASKS_MD) -> Path:
    p = tmp_path / "tasks.md"
    p.write_text(body)
    return p


class TestReviewStatusParsing:
    def test_parses_review_status(self, tmp_path):
        tasks = parse_tasks(_write(tmp_path))
        assert tasks[0].status == "review"

    def test_update_writes_review_emoji(self, tmp_path):
        p = _write(tmp_path)
        update_task_status(p, "TASK-002", "in_progress")
        update_task_status(p, "TASK-002", "review")
        content = p.read_text()
        assert "🔍" in content
        assert parse_tasks(p)[1].status == "review"

    def test_review_to_done_transition(self, tmp_path):
        p = _write(tmp_path)
        assert parse_tasks(p)[0].status == "review"  # fixture starts in review
        update_task_status(p, "TASK-001", "done")
        assert parse_tasks(p)[0].status == "done"


class TestReviewStatusScheduling:
    def test_review_task_is_resumed_like_in_progress(self, tmp_path):
        tasks = parse_tasks(_write(tmp_path))
        ready = get_next_tasks(tasks)
        assert ready and ready[0].id == "TASK-001"

    def test_review_is_not_done_for_dependents(self, tmp_path):
        tasks = parse_tasks(_write(tmp_path))
        resolved = resolve_dependencies(tasks)
        second = next(t for t in resolved if t.id == "TASK-002")
        assert second.status == "blocked"
        assert second.depends_on == ["TASK-001"]

    def test_restart_skips_review_tasks(self, tmp_path):
        tasks = parse_tasks(_write(tmp_path))
        ready = get_next_tasks(tasks, include_in_progress=False)
        assert all(t.id != "TASK-001" for t in ready)


class TestReviewStatusValidation:
    def test_review_is_valid_status(self):
        task = Task(
            id="TASK-001",
            name="x",
            priority="p0",
            status="review",
            estimate="",
        )
        result = validate_task_fields([task])
        assert not any("invalid status" in e for e in result.errors)


class TestPostDoneHookWritesReview:
    def test_review_status_visible_during_review_then_done(self, tmp_path):
        tasks_file = _write(tmp_path)
        update_task_status(tasks_file, "TASK-001", "in_progress")
        cfg = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            auto_commit=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
            review_parallel=False,
        )
        # ExecutorConfig derives tasks_file from spec_dir; point spec at tmp.
        observed: dict[str, str] = {}

        def fake_review(task, config, **kwargs):
            # What does tasks.md say WHILE review runs?
            statuses = {t.id: t.status for t in parse_tasks(config.tasks_file)}
            observed["during_review"] = statuses[task.id]
            return (ReviewVerdict.PASSED, None, "ok")

        task = parse_tasks(tasks_file)[0]
        with (
            patch("spec_runner.hooks.run_code_review", side_effect=fake_review),
            patch.object(
                ExecutorConfig,
                "tasks_file",
                property(lambda self: tmp_path / "tasks.md"),
            ),
        ):
            ok, err, _, _, _ = post_done_hook(task, cfg, True)
        assert ok is True, err
        assert observed["during_review"] == "review"
        assert parse_tasks(tasks_file)[0].status == "done"

    def test_kill_during_review_leaves_review_status(self, tmp_path):
        """The #66 scenario: run dies in review — file says REVIEW, not DONE."""
        tasks_file = _write(tmp_path)
        update_task_status(tasks_file, "TASK-001", "in_progress")
        cfg = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            auto_commit=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
            review_parallel=False,
        )
        task = parse_tasks(tasks_file)[0]
        with (
            patch(
                "spec_runner.hooks.run_code_review",
                side_effect=KeyboardInterrupt,
            ),
            patch.object(
                ExecutorConfig,
                "tasks_file",
                property(lambda self: tmp_path / "tasks.md"),
            ),
            contextlib.suppress(KeyboardInterrupt),
        ):
            post_done_hook(task, cfg, True)
        assert parse_tasks(tasks_file)[0].status == "review"
