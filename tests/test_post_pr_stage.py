"""Optional post-PR review stage (#102 M3).

Opt-in: with the default `review_pr.post_pr: off` the integration_pr flow
is byte-identical. `verify` runs the read-only loop; `full` checks out the
run branch, runs the whole loop, and always returns to base.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from spec_runner.cli import _post_pr_review_stage
from spec_runner.config import ExecutorConfig
from spec_runner.git_ops import IntegrationRun

PR_URL = "https://github.com/o/r/pull/9"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _repo_with_run_branch(tmp_path: Path) -> tuple[Path, IntegrationRun]:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "branch", "spec-runner/run-1")
    return tmp_path, IntegrationRun(branch="spec-runner/run-1", base="main")


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "state.db",
        "review_pr_post_pr_wait_seconds": 0,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestPostPrStage:
    def test_default_off_invokes_nothing(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        with patch("spec_runner.review_pr.cmd_review_pr") as mock_cmd:
            _post_pr_review_stage(_cfg(root), PR_URL, run)
        mock_cmd.assert_not_called()

    def test_unknown_mode_skips_with_warning(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        with patch("spec_runner.review_pr.cmd_review_pr") as mock_cmd:
            _post_pr_review_stage(_cfg(root, review_pr_post_pr="yolo"), PR_URL, run)
        mock_cmd.assert_not_called()

    def test_verify_mode_runs_read_only_without_checkout(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        with patch("spec_runner.review_pr.cmd_review_pr", return_value=0) as mock_cmd:
            _post_pr_review_stage(_cfg(root, review_pr_post_pr="verify"), PR_URL, run)
        assert mock_cmd.call_count == 1
        stage_args = mock_cmd.call_args.args[0]
        assert stage_args.pr_ref == PR_URL
        assert stage_args.verify_only is True
        # never left the base branch
        assert _git(root, "branch", "--show-current").stdout.strip() == "main"

    def test_full_mode_checks_out_branch_and_returns(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        seen_branches: list[str] = []

        def spy(args, config):
            seen_branches.append(_git(root, "branch", "--show-current").stdout.strip())
            return 0

        with patch("spec_runner.review_pr.cmd_review_pr", side_effect=spy):
            _post_pr_review_stage(_cfg(root, review_pr_post_pr="full"), PR_URL, run)
        assert seen_branches == ["spec-runner/run-1"]  # loop ran ON the run branch
        assert _git(root, "branch", "--show-current").stdout.strip() == "main"  # and returned

    def test_full_mode_returns_to_base_even_on_loop_crash(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        with patch("spec_runner.review_pr.cmd_review_pr", side_effect=RuntimeError("boom")):
            _post_pr_review_stage(_cfg(root, review_pr_post_pr="full"), PR_URL, run)
        assert _git(root, "branch", "--show-current").stdout.strip() == "main"

    def test_full_mode_missing_branch_skips_safely(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        run = IntegrationRun(branch="no-such-branch", base="main")
        with patch("spec_runner.review_pr.cmd_review_pr") as mock_cmd:
            _post_pr_review_stage(_cfg(root, review_pr_post_pr="full"), PR_URL, run)
        mock_cmd.assert_not_called()
        assert _git(root, "branch", "--show-current").stdout.strip() == "main"

    def test_wait_is_respected(self, tmp_path):
        root, run = _repo_with_run_branch(tmp_path)
        cfg = _cfg(root, review_pr_post_pr="verify", review_pr_post_pr_wait_seconds=7)
        with (
            patch("spec_runner.review_pr.cmd_review_pr", return_value=0),
            patch("time.sleep") as mock_sleep,
        ):
            _post_pr_review_stage(cfg, PR_URL, run)
        mock_sleep.assert_called_once_with(7)

    def test_config_yaml_keys(self, tmp_path):
        import argparse

        from spec_runner.config import build_config, load_config_from_yaml

        cfg_file = tmp_path / "spec-runner.config.yaml"
        cfg_file.write_text("review_pr:\n  post_pr: verify\n  post_pr_wait_seconds: 5\n")
        cfg = build_config(load_config_from_yaml(cfg_file), argparse.Namespace(command="status"))
        assert cfg.review_pr_post_pr == "verify"
        assert cfg.review_pr_post_pr_wait_seconds == 5
        assert ExecutorConfig().review_pr_post_pr == "off"
