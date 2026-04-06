"""Git operations for spec-runner.

Contains branch management, file change detection, and test scoping
functions used by hooks during task execution.
"""

import subprocess
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
