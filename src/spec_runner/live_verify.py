"""The live verify-first run (#367 BEH-07/BEH-08/BEH-09).

Under `verify_first`, the declared `**Verifies:**` group is run for real as
the task's first action — before any paid agent call, including the RED
authoring pass `tdd` mode runs first today. Two properties the run itself
must hold, independent of whatever later work does with the outcome:

- **scoped** (BEH-08): the command that runs is narrowed to the declared
  selectors, never the project's whole suite — even on a default
  `test_command` that names a directory (`pytest tests/ ...`).
- **commit, not tree** (BEH-09): the verdict is about a named commit (HEAD at
  the moment execution reaches this point), replayed in a disposable detached
  worktree exactly like `tdd.verify_red` — so a working tree that has since
  moved on cannot change what was judged, and the same commit replayed again
  gives the same answer.

Deliberately narrow: this module runs the group and reports what happened. It
does not decide the green-only / TDD / instrument-error branching (#367
FR-08+) or persist durable evidence (#367 FR-07/FR-10, later work) — both are
separate concerns layered on top of this one.

`test_command` is not handed to `adapter.build_command` unmodified: that
method only *appends* the selector (`tdd.py`'s red replay relies on exactly
that shape, frozen by #141/#198), and a default `test_command` that already
names `tests/` would then run the whole suite plus the one selector — the
opposite of "restricted to the declared group" (FR-06). `_strip_test_path_args`
below removes the bare test-path argument first, mirroring the
replace-not-append rule `git_ops.build_scoped_test_command` already applies
for the post-done hook, without touching the shared adapter method.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import ExecutorConfig
from .git_ops import is_composite_shell_command
from .task import Task
from .tdd import resolve_adapter
from .tdd_runners import ReplayEnvironmentRefusal, SelectorRefusal, command_tokens

#: Same shape as `git_ops._TEST_PATH_ARG`: a whitespace-delimited argument
#: that is the test directory, matched whole so `contests/x` is untouched.
_TEST_PATH_TOKENS = {"tests"}


@dataclass(frozen=True)
class VerifyRunResult:
    """What the live run observed, for the caller to act on.

    `sha` is set whenever HEAD could be resolved, even on a later refusal, so
    a caller can always name the commit a refusal is about.
    """

    sha: str
    #: True once a runner actually executed the group (whatever the verdict);
    #: False for a refusal that never got that far (composite command, no
    #: adapter, an unresolvable HEAD or worktree).
    ran: bool
    passed: bool
    detail: str


def _strip_test_path_args(test_command: str) -> str:
    """`test_command` with any bare test-path argument removed.

    The adapter's own `build_command` still decides how to *append* the
    selector (each runner's locator has a different shape — a pytest node id,
    an ExUnit `path:line`); this only clears the directory argument a default
    `test_command` names first, so that append lands on the declared group
    alone rather than on the group plus the whole suite (FR-06).
    """
    tokens = command_tokens(test_command)
    kept = [
        token
        for token in tokens
        if token not in _TEST_PATH_TOKENS and not token.startswith("tests/")
    ]
    return shlex.join(kept) if kept else test_command


def run_live_verify(
    task: Task,
    config: ExecutorConfig,
    *,
    log_progress: Callable[[str], None] | None = None,
) -> VerifyRunResult:
    """Run `task.verifies` for real, against HEAD, before any paid call.

    One subprocess per declared selector (FR-06: a single shared command
    cannot attribute a combined result back to each selector). The first
    selector that fails or cannot be run stops the group — the group is a
    single verdict, not an average.
    """
    root = Path(config.project_root)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    if head.returncode != 0:
        return VerifyRunResult("", False, False, f"could not resolve HEAD: {head.stderr.strip()}")
    sha = head.stdout.strip()

    if is_composite_shell_command(config.test_command):
        return VerifyRunResult(
            sha,
            False,
            False,
            f"test_command {config.test_command!r} is composite; cannot narrow it "
            "to the declared group",
        )

    adapter = resolve_adapter(config)
    if adapter is None:
        return VerifyRunResult(
            sha,
            False,
            False,
            f"no adapter can confirm test_command {config.test_command!r}",
        )

    parent = tempfile.mkdtemp(prefix="spec-runner-verify-")
    worktree = Path(parent) / "tree"
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        return VerifyRunResult(
            sha, False, False, f"could not check out {sha[:12]}: {added.stderr.strip()[:200]}"
        )

    scoped_command = _strip_test_path_args(config.test_command)
    cleanup_paths: list[Path] = []
    try:
        for raw_selector in task.verifies or []:
            parsed = adapter.parse_selector(raw_selector)
            if isinstance(parsed, SelectorRefusal):
                # Validated already (`validate._validate_verify_first_declarations`)
                # before a verify_first run can start; reachable only if the
                # tasks file changed between validate and this attempt.
                return VerifyRunResult(sha, False, False, parsed.message)

            refusal = adapter.preflight(worktree, parsed)
            if refusal is not None:
                return VerifyRunResult(sha, False, False, refusal.message)

            prepared = adapter.prepare_replay(root, worktree, parsed)
            if isinstance(prepared, ReplayEnvironmentRefusal):
                return VerifyRunResult(sha, False, False, prepared.message)
            cleanup_paths.extend(prepared.cleanup_paths)

            argv = adapter.build_command(scoped_command, parsed)
            if log_progress is not None:
                log_progress(f"⏳ verify: {raw_selector}")
            result = subprocess.run(
                argv,
                cwd=worktree,
                capture_output=True,
                text=True,
                env={**os.environ, **prepared.env},
            )
            if result.returncode != 0:
                tail = "\n".join(
                    line
                    for line in (result.stdout + "\n" + result.stderr).strip().splitlines()
                    if line.strip()
                )[-500:]
                return VerifyRunResult(
                    sha,
                    True,
                    False,
                    f"{raw_selector} did not pass (exit {result.returncode}): {tail}",
                )
        return VerifyRunResult(sha, True, True, "declared group passed")
    finally:
        for path in cleanup_paths:
            shutil.rmtree(path, ignore_errors=True)
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(parent, ignore_errors=True)
        if removed.returncode != 0:
            subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, text=True)
