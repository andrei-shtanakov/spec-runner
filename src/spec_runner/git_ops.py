"""Git operations for spec-runner.

Contains branch management, file change detection, and test scoping
functions used by hooks during task execution.
"""

import contextlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import PROGRESS_FILE, ExecutorConfig
from .logging import get_logger
from .task import Task, history_file_for

logger = get_logger("git_ops")


def runtime_state_paths(config: ExecutorConfig) -> list[Path]:
    """Executor runtime files that must never be committed (#62).

    Committing the live SQLite state to a task branch is what broke run
    finalization in the field: a later branch switch reverted the DB under
    the open connection, losing the success status and all costs (#67).
    """
    state = config.state_file
    return [
        state,
        state.with_name(state.name + "-wal"),
        state.with_name(state.name + "-shm"),
        state.with_suffix(".lock"),
        config.logs_dir,
        config.stop_file,
        config.spec_lock_file,
        config.project_root / PROGRESS_FILE,
        history_file_for(config.tasks_file),
    ]


def stage_all_except_runtime(config: ExecutorConfig) -> bool:
    """Stage all changes except executor runtime state.

    ``git add -A`` followed by unstaging every runtime path. Uses
    ``git rm --cached`` (not ``git reset``) so it also works in a fresh repo
    without commits AND actively untracks runtime files that an earlier run
    already committed. Returns True when anything is left staged.

    Raises RuntimeError when ``git add`` itself fails (index lock,
    permissions): with a clean index that failure would otherwise be
    indistinguishable from "nothing to commit" and flow into a false
    no-op verdict (#97/#103).
    """
    add = _git(config, "add", "-A")
    if add.returncode != 0:
        raise RuntimeError(f"git add -A failed: {add.stderr.strip()[:200]}")
    rels: list[str] = []
    for p in runtime_state_paths(config):
        try:
            rels.append(str(p.relative_to(config.project_root)))
        except ValueError:
            continue  # outside the repo — git never saw it
    if rels:
        _git(config, "rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", *rels)
    # Harness-owned file (#96): spec/.gitignore is written by
    # ensure_runtime_gitignore, not by the task's agent. Committing it put a
    # file no agent chose to create into the workstream diff, which Maestro's
    # ex-post scope gate rightly flags. Keep it out of the commit set — unless
    # the user tracks it themselves (present in HEAD), in which case it keeps
    # its old travels-with-the-spec behavior and is never deleted here.
    gitignore_rel = "spec/.gitignore"
    in_head = _git(config, "cat-file", "-e", f"HEAD:{gitignore_rel}").returncode == 0
    if not in_head:
        _git(config, "rm", "--cached", "-q", "--ignore-unmatch", "--", gitignore_rel)
    staged = _git(config, "diff", "--cached", "--quiet")
    return staged.returncode != 0


# Patterns are relative to the spec dir and slash-free, so they also cover
# per-change dirs (spec/changes/<id>/) and --spec-prefix variants.
RUNTIME_GITIGNORE_ENTRIES = [
    ".executor-*",
    ".*task-history.log",
    ".*spec.lock",
]


def ensure_runtime_gitignore(config: ExecutorConfig) -> None:
    """Make sure ``spec/.gitignore`` covers executor runtime files (#62).

    Idempotent; only appends entries that are missing. The file lives inside
    the spec dir but is harness-owned: auto-commits exclude it unless the
    user tracks it themselves (#96). Staying untracked is safe — this hook
    runs before every task and re-creates the file if it went missing.
    """
    spec_base = config.project_root / "spec"
    if not spec_base.is_dir():
        return
    gitignore = spec_base / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in RUNTIME_GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return
    lines = list(existing)
    if not lines:
        lines.append("# spec-runner runtime state — never commit (managed by spec-runner)")
    lines.extend(missing)
    gitignore.write_text("\n".join(lines) + "\n")
    logger.info("Updated spec/.gitignore with runtime-state entries", added=missing)


def spec_dirty_paths(config: ExecutorConfig) -> list[str]:
    """Spec/config files with uncommitted changes, as git-status lines (#69).

    A run whose spec has no committed version predating execution has muddy
    provenance: the auto-commit later mixes the spec and the task's code into
    one commit, and an interrupted run leaves DONE edits in an uncommitted
    file. Checks the spec content files (tasks/requirements/design/
    constitution) and the config file.

    Uses ``git status --porcelain``, which does not report ignored files —
    so orchestrators that deliberately keep their generated specs untracked
    via gitignore/info-exclude (Maestro does) are unaffected.

    Returns [] when there is no git repo or no commits yet (fresh-repo
    bootstrap must not be blocked).
    """
    if _git(config, "rev-parse", "--git-dir").returncode != 0:
        return []
    if _git(config, "rev-parse", "HEAD").returncode != 0:
        return []

    candidates = [
        config.tasks_file,
        config.requirements_file,
        config.design_file,
        config.constitution_file,
        config.project_root / "spec-runner.config.yaml",
        config.project_root / "spec" / "executor.config.yaml",  # legacy location
    ]
    # No existence filter: a tracked-but-deleted spec file is dirt too, and
    # git status reports deletions for paths that are gone from the tree.
    rels: list[str] = []
    for p in candidates:
        try:
            rels.append(str(p.relative_to(config.project_root)))
        except ValueError:
            continue  # outside the repo
    if not rels:
        return []
    status = _git(config, "status", "--porcelain", "--", *rels)
    if status.returncode != 0:
        # Fail closed: an unreadable repo state must not silently pass the
        # guard (the two legitimate pass cases — no repo, no commits — were
        # already handled above).
        return [f"?? (git status failed: {status.stderr.strip()[:120]})"]
    return [line for line in status.stdout.splitlines() if line.strip()]


def get_task_branch_name(task: Task) -> str:
    """Generate branch name for task.

    Slugging strips all punctuation (#74) — plain space/slash replacement
    kept commas and '+' in branch names (`task/task-004-fake-executor-+-...`),
    valid for git but brittle for tooling and URLs.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task.name.lower()).strip("-")[:30].rstrip("-")
    return f"task/{task.id.lower()}-{slug}" if slug else f"task/{task.id.lower()}"


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


# Shell metacharacters that chain or redirect commands. Their presence means
# `test_command` is more than one program, and no textual edit of the string
# can know which component takes test paths (#139).
_SHELL_CHAIN = re.compile(r"&&|\|\||[;|\n]")

# A whitespace-delimited argument that is the test directory: `tests`,
# `tests/`, or `tests/<anything>`. Anchored on both sides so `contests/x` and
# `--ignore=vendor/tests` are not mistaken for it.
_TEST_PATH_ARG = re.compile(r"(?<!\S)tests(?:/\S*)?(?!\S)")


def is_composite_shell_command(command: str) -> bool:
    """True when ``command`` chains several programs (``&&``, ``||``, ``;``, ``|``)."""
    return bool(_SHELL_CHAIN.search(command))


def build_scoped_test_command(
    base_command: str,
    test_files: list[Path],
    project_root: Path,
) -> str:
    """Narrow a test command to specific files, or return it untouched.

    Returns ``base_command`` unchanged when it cannot be narrowed safely:

    - **no test files** — nothing to narrow to;
    - **a composite command** (#139) — `test_command` is a shell string, and
      real ones chain a pin check, pytest and a type checker. Appending paths
      put them on the *last* component (`pyrefly check`), and substituting the
      first `tests/` substring hit whichever component happened to contain it.
      Running the full declared gate is always safe; guessing which program
      accepts test paths is not, so this refuses instead.

    Otherwise the test-path argument is replaced wholesale — `pytest
    tests/unit` narrows to the mapped files rather than becoming
    `pytest <files>unit` — or the paths are appended when the command names no
    path at all.
    """
    if not test_files:
        return base_command
    if is_composite_shell_command(base_command):
        return base_command
    rel_paths = " ".join(str(f.relative_to(project_root)) for f in test_files)
    scoped, replaced = _TEST_PATH_ARG.subn(rel_paths, base_command, count=1)
    if replaced:
        return scoped
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


def pick_remote(config: ExecutorConfig) -> str | None:
    """Remote to push to: ``origin`` if present, else the first configured."""
    result = _git(config, "remote")
    if result.returncode != 0:
        return None
    remotes = result.stdout.split()
    if not remotes:
        return None
    return "origin" if "origin" in remotes else remotes[0]


def has_remote(config: ExecutorConfig) -> bool:
    """True when the repo has at least one configured git remote."""
    return pick_remote(config) is not None


def make_integration_branch_name(now: datetime | None = None) -> str:
    """Per-run integration branch name, unique to the second."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"spec-runner/run-{stamp}"


def create_integration_branch(config: ExecutorConfig, branch_name: str) -> IntegrationRun | None:
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


def finalize_integration_branch(config: ExecutorConfig, run: IntegrationRun) -> str | None:
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
        remote = pick_remote(config)
        if remote is None:
            logger.warning(
                "integration_pr: no git remote, leaving integration branch local",
                branch=run.branch,
                commits=commits,
            )
            return None

        push = _git(config, "push", "-u", remote, run.branch)
        if push.returncode != 0:
            logger.warning(
                "integration_pr: push failed",
                branch=run.branch,
                remote=remote,
                stderr=push.stderr.strip()[:200],
            )
            return None

        return _open_pr(config, run, commits)
    finally:
        back = _git(config, "checkout", run.base)
        if back.returncode != 0:
            # Loud, operator-facing failure (#62): a warning that scrolls away
            # left operators stranded on the run branch with a dirty tree.
            stderr = back.stderr.strip()[:200]
            logger.error(
                "integration_pr: could not return to base branch",
                base=run.base,
                branch=run.branch,
                stderr=stderr,
            )
            # stderr, not stdout: `run --json-result` prints its JSON to
            # stdout after this returns, and a stray line would corrupt it.
            print(
                f"❌ Could not return to base branch '{run.base}' "
                f"(working copy left on '{run.branch}'):\n"
                f"   {stderr}\n"
                f"   Resolve manually: commit/stash local changes, "
                f"then `git checkout {run.base}`.",
                file=sys.stderr,
            )


# Cap the commit list embedded in the PR body: keeps it readable and, with
# --body-file, well clear of any OS command-line length limits.
_MAX_PR_SUBJECTS = 50


def _open_pr(config: ExecutorConfig, run: IntegrationRun, commits: int) -> str | None:
    """Open one PR for the pushed integration branch. Returns URL or None."""
    subjects = (
        _git(config, "log", "--format=- %s", f"{run.base}..{run.branch}")
        .stdout.strip()
        .splitlines()
    )
    shown = subjects[:_MAX_PR_SUBJECTS]
    if len(subjects) > _MAX_PR_SUBJECTS:
        shown.append(f"- …and {len(subjects) - _MAX_PR_SUBJECTS} more")
    title = f"spec-runner: {commits} commit(s) from automated run"
    body = "Automated spec-runner run.\n\nCommits:\n" + "\n".join(shown)

    # --body-file (not --body) so a large body never hits arg-length limits.
    body_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(body)
            body_path = fh.name
        try:
            pr = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    run.base,
                    "--head",
                    run.branch,
                    "--title",
                    title,
                    "--body-file",
                    body_path,
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
    finally:
        if body_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(body_path)

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
