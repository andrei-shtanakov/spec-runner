"""Git operations for spec-runner.

Contains branch management, file change detection, and test scoping
functions used by hooks during task execution.
"""

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ExecutorConfig
from .logging import get_logger
from .task import Task

logger = get_logger("git_ops")


def get_task_branch_name(task: Task) -> str:
    """Generate branch name for task"""
    safe_name = task.name.lower().replace(" ", "-").replace("/", "-")[:30]
    return f"task/{task.id.lower()}-{safe_name}"


def get_main_branch(config: ExecutorConfig) -> str:
    """Determine main branch name (main or master).

    Detection order:
    1. Config setting (main_branch)
    2. Remote HEAD (origin/HEAD)
    3. Existing main or master branch
    4. Current branch (if no main/master exists yet)
    5. Default to "main"
    """
    # 0. Use config if explicitly set
    if config.main_branch:
        return config.main_branch

    # 1. Try remote HEAD
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0:
        # refs/remotes/origin/main -> main
        return result.stdout.strip().split("/")[-1]

    # 2. Check if main or master branch exists
    for branch in ["main", "master"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        if result.returncode == 0:
            return branch

    # 3. If no main/master, use current branch as "main"
    # (handles fresh repos where first branch might be named differently)
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "main"  # default for brand new repos


def ensure_on_main_branch(config: ExecutorConfig) -> None:
    """Ensure we're on main branch after all tasks complete."""
    try:
        main_branch = get_main_branch(config)

        # Check current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        current_branch = result.stdout.strip()

        if current_branch != main_branch:
            logger.info("Switching to main branch", branch=main_branch)
            result = subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode == 0:
                logger.info("On main branch", branch=main_branch)
            else:
                logger.warning(
                    "Could not switch to main branch",
                    branch=main_branch,
                    stderr=result.stderr.strip(),
                )
    except Exception:
        logger.debug("git_switch_failed", exc_info=True)


def find_changed_source_files(project_root: Path, changed_since: float) -> list[Path]:
    """Find .py files in src/ with mtime > changed_since."""
    src_dir = project_root / "src"
    if not src_dir.exists():
        return []
    changed: list[Path] = []
    for p in src_dir.rglob("*.py"):
        if p.stat().st_mtime > changed_since:
            changed.append(p)
    return changed


def map_source_to_test_files(source_files: list[Path], project_root: Path) -> list[Path]:
    """Map src/pkg/module/file.py -> tests/test_file.py by convention."""
    tests_dir = project_root / "tests"
    if not tests_dir.exists():
        return []
    mapped: list[Path] = []
    for src in source_files:
        test_name = f"test_{src.name}"
        # Search tests/ for matching test file
        for candidate in tests_dir.rglob(test_name):
            if candidate not in mapped:
                mapped.append(candidate)
    return mapped


def build_scoped_test_command(
    base_command: str,
    test_files: list[Path],
    project_root: Path,
) -> str:
    """Replace generic test path with specific file paths."""
    if not test_files:
        return base_command
    rel_paths = " ".join(str(f.relative_to(project_root)) for f in test_files)
    # Replace common patterns: "tests/" or "tests" at end of command
    for pattern in ["tests/ ", "tests/", "tests "]:
        if pattern in base_command:
            return base_command.replace(pattern, rel_paths + " ", 1)
    # Append test files if no pattern matched
    return f"{base_command} {rel_paths}"


@dataclass
class IntegrationRun:
    """State for a run that collects every task on one integration branch.

    ``base`` is the real main branch the final PR targets; ``branch`` is the
    per-run integration branch that tasks merge into.
    """

    branch: str
    base: str


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in the project root, capturing output."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )


def has_remote(config: ExecutorConfig) -> bool:
    """True when the repo has at least one configured git remote."""
    result = _git(config, "remote")
    return result.returncode == 0 and bool(result.stdout.strip())


def make_integration_branch_name(now: datetime | None = None) -> str:
    """Per-run integration branch name, unique to the second."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"spec-runner/run-{stamp}"


def create_integration_branch(
    config: ExecutorConfig, branch_name: str
) -> IntegrationRun | None:
    """Fork ``branch_name`` off the real main branch and check it out.

    Returns None (caller falls back to normal per-task merge) when the base
    branch cannot be checked out or the integration branch cannot be created.
    """
    base = get_main_branch(config)
    checkout = _git(config, "checkout", base)
    if checkout.returncode != 0:
        logger.warning(
            "integration_pr: cannot checkout base branch, falling back",
            base=base,
            stderr=checkout.stderr.strip()[:200],
        )
        return None
    created = _git(config, "checkout", "-b", branch_name)
    if created.returncode != 0:
        logger.warning(
            "integration_pr: cannot create integration branch, falling back",
            branch=branch_name,
            stderr=created.stderr.strip()[:200],
        )
        return None
    logger.info("Integration branch created", branch=branch_name, base=base)
    return IntegrationRun(branch=branch_name, base=base)


def finalize_integration_branch(
    config: ExecutorConfig, run: IntegrationRun
) -> str | None:
    """Push the integration branch and open one PR; clean up when empty.

    Returns the PR URL on success, else None. When no task produced a commit,
    the empty integration branch is deleted and no PR is opened. A missing
    remote or ``gh`` degrades to a warning, leaving the branch local.
    """
    count = _git(config, "rev-list", "--count", f"{run.base}..{run.branch}")
    try:
        commits = int(count.stdout.strip() or "0")
    except ValueError:
        commits = 0

    if commits == 0:
        logger.info("Integration branch empty, cleaning up", branch=run.branch)
        _git(config, "checkout", run.base)
        _git(config, "branch", "-D", run.branch)
        return None

    # A non-empty branch is never deleted (it holds the run's work); leave the
    # working copy back on the base branch regardless of how far we get.
    try:
        if not has_remote(config):
            logger.warning(
                "integration_pr: no git remote, leaving integration branch local",
                branch=run.branch,
                commits=commits,
            )
            return None

        push = _git(config, "push", "-u", "origin", run.branch)
        if push.returncode != 0:
            logger.warning(
                "integration_pr: push failed",
                branch=run.branch,
                stderr=push.stderr.strip()[:200],
            )
            return None

        subjects = _git(
            config, "log", "--format=- %s", f"{run.base}..{run.branch}"
        ).stdout.strip()
        title = f"spec-runner: {commits} commit(s) from automated run"
        body = f"Automated spec-runner run.\n\nCommits:\n{subjects}"
        try:
            pr = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--base", run.base,
                    "--head", run.branch,
                    "--title", title,
                    "--body", body,
                ],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
        except FileNotFoundError:
            logger.warning(
                "integration_pr: gh not found; branch pushed, open the PR manually",
                branch=run.branch,
            )
            return None
        if pr.returncode != 0:
            logger.warning(
                "integration_pr: gh pr create failed",
                branch=run.branch,
                stderr=pr.stderr.strip()[:200],
            )
            return None
        url = pr.stdout.strip()
        logger.info("Opened integration PR", url=url, branch=run.branch)
        return url
    finally:
        _git(config, "checkout", run.base)
