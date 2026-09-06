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

Deliberately narrow in one respect, and no longer in another (#375 review):
this module runs the group and classifies each selector through its
adapter's own `classify`/`prove_selected`/`execution_proven` dictionary — the
same primitives `tdd.py`'s red replay uses — rather than reading a raw exit
code, because an exit code alone cannot tell a genuine failure from a broken
instrument (#198's lesson) or a skipped/xfailed selector from an executed one
(#367 FR-08). It still does not decide the green-only / TDD branching that a
genuine pass or failure eventually drives (#367 FR-08+, later work), nor does
it persist durable evidence (#367 FR-07/FR-10, later work) — both stay
layered on top of this one; a real `TESTS_FAILED` verdict is only ever an
*observation* this module reports, never a refusal it issues.

`test_command` is not handed to `adapter.build_command` unmodified: that
method only *appends* the selector (`tdd.py`'s red replay relies on exactly
that shape, frozen by #141/#198), and a default `test_command` that already
names a test directory would then run the whole suite plus the one selector —
the opposite of "restricted to the declared group" (FR-06).
`adapter.build_scoped_command` — a separate method the red-replay path never
calls — replaces the command's own path argument instead, mirroring the
replace-not-append rule `git_ops.build_scoped_test_command` already applies
for the post-done hook.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import ExecutorConfig
from .git_ops import is_composite_shell_command
from .task import Task
from .tdd import REPLAY_TIMEOUT_SECONDS, resolve_adapter
from .tdd_runners import ReplayEnvironmentRefusal, RunOutcome, SelectionProof, SelectorRefusal


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


def _tail(text: str, limit: int = 500) -> str:
    return "\n".join(line for line in text.strip().splitlines() if line.strip())[-limit:]


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

    if not task.verifies:
        # #375 review: an empty (or undeclared) group is not a vacuous pass —
        # "0 passed and exit 0 prove nothing" is FR-09's rule for a selection
        # that ran and matched nothing, and it applies just as much to a
        # selection that never started. `validate._validate_verify_first_declarations`
        # already refuses this before a run can be scheduled; reaching this
        # branch means the tasks file changed between validate and this
        # attempt.
        return VerifyRunResult(
            sha,
            False,
            False,
            "verify_first declared no group to run (an empty or missing "
            "**Verifies:**); 0 selectors executing proves nothing",
        )

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

    cleanup_paths: list[Path] = []
    try:
        for raw_selector in task.verifies:
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

            argv = adapter.build_scoped_command(config.test_command, parsed)
            if log_progress is not None:
                log_progress(f"⏳ verify: {raw_selector}")
            result = subprocess.run(
                argv,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=REPLAY_TIMEOUT_SECONDS,
                env={**os.environ, **prepared.env},
            )

            # #375 review: the verdict is read from the adapter's own
            # classify/prove_selected dictionary — the same one `tdd._classify`
            # uses for the red replay — never from a raw exit code. A code
            # alone cannot separate a genuine failure from a broken instrument
            # (`SELECTION_FAILED`/`COLLECTION_OR_COMPILE_ERROR`/`RUNNER_ERROR`
            # all exit non-zero on pytest, and ExUnit's codes are inverted
            # relative to pytest's entirely, #198) or a skipped/xfailed
            # selector from an executed one (FR-08's third fact).
            outcome = adapter.classify(result)
            proof = adapter.prove_selected(parsed, result)
            tail = _tail(f"{result.stdout}\n{result.stderr}")

            if outcome is RunOutcome.TESTS_FAILED and proof is SelectionProof.PROVEN:
                # A real, attributable test failure. Reported as an
                # observation for the caller to record — never a refusal this
                # module issues itself (FR-14: the branching this eventually
                # drives is later work, and until it exists a genuine failure
                # must not be read as an instrument that could not tell).
                return VerifyRunResult(
                    sha,
                    True,
                    False,
                    f"{raw_selector} failed on replay (exit {result.returncode}): {tail}",
                )

            if (
                outcome is RunOutcome.TESTS_PASSED
                and proof is SelectionProof.PROVEN
                and adapter.execution_proven(parsed, result)
            ):
                continue  # this selector is green; judge the next one

            # Everything else is an instrument-error: the run could not
            # establish a verdict about the requested selector, so it must
            # not read as either a pass or a genuine failure (#367 FR-08/09).
            if outcome is RunOutcome.SELECTION_FAILED:
                reason = f"{raw_selector} selected nothing (exit {result.returncode})"
            elif outcome is RunOutcome.COLLECTION_OR_COMPILE_ERROR:
                reason = f"{raw_selector} could not be collected (exit {result.returncode})"
            elif outcome is RunOutcome.RUNNER_ERROR:
                reason = f"the runner itself failed on {raw_selector} (exit {result.returncode})"
            elif proof is SelectionProof.REFUTED:
                reason = f"{raw_selector}: a different test executed than the one requested"
            elif proof is SelectionProof.UNKNOWN:
                reason = f"{raw_selector}: the run did not prove which test executed"
            elif outcome is RunOutcome.TESTS_PASSED and proof is SelectionProof.PROVEN:
                # Reached only when `execution_proven` was False: the selector
                # matched (e.g. a SKIPPED/XFAIL line still carries its node
                # id) but was never actually executed — 0 proven executions
                # is not green (FR-09).
                reason = f"{raw_selector} was not executed (skipped, xfail, or deselected)"
            else:
                reason = (
                    f"{raw_selector}: unrecognized run outcome "
                    f"({outcome.value}, selection {proof.value})"
                )
            return VerifyRunResult(sha, False, False, f"{reason}: {tail}")
        return VerifyRunResult(sha, True, True, "declared group passed")
    except subprocess.TimeoutExpired as exc:
        return VerifyRunResult(sha, False, False, f"verify run timed out: {exc}")
    except Exception as exc:  # a broken replay is unverifiable, never a pass
        return VerifyRunResult(sha, False, False, f"verify run failed: {exc}")
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
