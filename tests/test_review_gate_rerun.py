"""Tests/lint must re-run after REVIEW_FIXED mutates the code (#65)."""

from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import post_done_hook
from spec_runner.state import ReviewVerdict
from spec_runner.task import Task


def _task() -> Task:
    return Task(
        id="TASK-001",
        name="demo",
        priority="p0",
        status="in_progress",
        estimate="",
        description="",
        checklist=[],
    )


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "create_git_branch": False,
        "auto_commit": False,
        "run_tests_on_done": True,
        "run_lint_on_done": False,
        "run_review": True,
        "review_parallel": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _review(verdict: ReviewVerdict):
    return patch(
        "spec_runner.hooks.run_code_review",
        return_value=(verdict, None, "review output"),
    )


class TestRerunGatesAfterReviewFix:
    def test_fixed_verdict_reruns_tests_and_fails_on_red(self, tmp_path):
        counter = tmp_path / "runs.txt"
        cfg = _cfg(
            tmp_path,
            # Green on the first (pre-review) run, red on the re-run:
            # a review "fix" that broke the suite.
            test_command=f'echo x >> "{counter}"; [ "$(wc -l < "{counter}")" -le 1 ]',
        )
        with _review(ReviewVerdict.FIXED):
            ok, err, review_status, _ = post_done_hook(_task(), cfg, True)
        assert ok is False
        assert err is not None and err.startswith("Tests failed after review fixes")
        assert review_status == ReviewVerdict.FIXED.value
        assert counter.read_text().count("x") == 2  # gate ran twice

    def test_fixed_verdict_passes_when_rerun_green(self, tmp_path):
        counter = tmp_path / "runs.txt"
        cfg = _cfg(tmp_path, test_command=f'echo x >> "{counter}"')
        with _review(ReviewVerdict.FIXED):
            ok, err, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is True, err
        assert counter.read_text().count("x") == 2

    def test_passed_verdict_runs_tests_once(self, tmp_path):
        counter = tmp_path / "runs.txt"
        cfg = _cfg(tmp_path, test_command=f'echo x >> "{counter}"')
        with _review(ReviewVerdict.PASSED):
            ok, _, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is True
        assert counter.read_text().count("x") == 1

    def test_fixed_verdict_reruns_blocking_lint(self, tmp_path):
        lint_marker = tmp_path / "lint-runs.txt"
        cfg = _cfg(
            tmp_path,
            run_tests_on_done=False,
            run_lint_on_done=True,
            lint_blocking=True,
            # Green pre-review (auto-fix path not entered), red on re-run.
            lint_command=(f'echo x >> "{lint_marker}"; [ "$(wc -l < "{lint_marker}")" -le 1 ]'),
        )
        with _review(ReviewVerdict.FIXED):
            ok, err, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is False
        assert err is not None and err.startswith("Lint errors after review fixes")

    def test_fixed_lint_nonblocking_only_warns(self, tmp_path):
        lint_marker = tmp_path / "lint-runs.txt"
        cfg = _cfg(
            tmp_path,
            run_tests_on_done=False,
            run_lint_on_done=True,
            lint_blocking=False,
            lint_command=(f'echo x >> "{lint_marker}"; [ "$(wc -l < "{lint_marker}")" -le 1 ]'),
        )
        with _review(ReviewVerdict.FIXED):
            ok, err, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is True, err
