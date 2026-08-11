"""Verifying a RED and persisting the checkpoint — #141 slice 1b.

A confirmed red means **the selector was executed and failed in the expected
way**. Not "the agent said it failed": an agent's report of its own red is
precisely the evidence this lifecycle exists to replace.

Verification replays the *commit*, in a disposable worktree. That is the whole
reason the red checkpoint commit exists — replay without a commit to replay
against is trust in whatever happens to be in the working tree at the moment,
which is the thing being replaced. It also means a replay can never influence
the run it is judging.

Three outcomes, not two:

    EXPECTED_FAIL   the selector ran and failed — a confirmed red
    NOT_RED         the selector ran and passed — the claim was wrong
    UNVERIFIABLE    nothing was demonstrated: the test could not be collected,
                    the selector matched nothing, the SHA is unknown, the test
                    command cannot be narrowed safely

`UNVERIFIABLE` is deliberately not folded into `NOT_RED`. "The test passes" is
a fact about the code; "we could not find out" is a fact about us, and the
consumer (the gate, 1c) owes them different responses — the same distinction
`GateStatus` draws between `unsatisfied` and `instrument_error`.

Standalone by design: nothing imports this yet. Wiring `tdd` mode into
execution arrives with the gate (1c), together, so the mode never exists in a
half-enforcing state where a red is recorded but green runs regardless.

Design: ``docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md`` §3.3, §3.7
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .git_ops import is_composite_shell_command
from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .state import ExecutorState

logger = get_logger("tdd")

#: Lockfiles that identify an environment, most specific first. The order is
#: fixed so the answer is deterministic when a repo carries more than one.
LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
    "mix.lock",
)

#: How long a replay may take before it is abandoned as unverifiable. A hung
#: test run must not hang the executor.
REPLAY_TIMEOUT_SECONDS = 900


class RedOutcome(str, Enum):
    """What the replay established."""

    EXPECTED_FAIL = "expected_fail"
    NOT_RED = "not_red"
    #: Nothing was established. Not the same as "it passes".
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class RedVerification:
    """The result of replaying one claimed red."""

    outcome: RedOutcome
    detail: str | None = None
    environment_id: str = "unpinned"


@dataclass(frozen=True)
class RedCheckpoint:
    """A durable claim that a specific test failed on a specific tree.

    Every field earns its place from the pilot (§3.3): without the SHA replay
    is impossible; a bare test *name* matches several tests and so proves
    nothing about the one; the baseline says red *against what*; and the
    namespace keeps identical `TASK-NNN` ids from different workstreams apart
    once their branches meet.
    """

    task_id: str
    namespace: str
    commit_sha: str
    baseline_sha: str
    selector: str
    environment_id: str
    execution_mode: str
    config_hash: str
    outcome: RedOutcome = RedOutcome.EXPECTED_FAIL
    timestamp: str = ""


def environment_id(project_root: Path) -> str:
    """Identify the environment a replay would run in, by lockfile content.

    Returns ``"<lockfile>:<hash>"``, or ``"unpinned"`` when the project pins
    nothing. Saying "unpinned" is honest and keeps TDD mode available to
    projects without a lockfile; inventing an identity would not be.
    """
    for name in LOCKFILES:
        candidate = project_root / name
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
            return f"{name}:{digest}"
    return "unpinned"


def resolve_namespace(config: ExecutorConfig) -> str:
    """Which workstream a checkpoint belongs to.

    An explicit ``tdd_namespace`` wins: an orchestrator that knows its own
    workstream identity should state it rather than have it inferred. The
    fallback is derived from the resolved project root plus ``spec_prefix``,
    which separates both parallel worktrees and phases inside one tree.
    """
    declared = getattr(config, "tdd_namespace", "") or ""
    if declared.strip():
        return declared.strip()
    seed = f"{Path(config.project_root).resolve()}|{config.spec_prefix}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def verify_red(
    config: ExecutorConfig,
    *,
    sha: str,
    selector: str,
    baseline_sha: str,
) -> RedVerification:
    """Replay ``selector`` against commit ``sha`` in a disposable worktree."""
    env_id = environment_id(Path(config.project_root))

    if "::" not in selector:
        # `-k`-style names match several tests, and a checkpoint that matches
        # several proves nothing about the one (§3.3).
        return RedVerification(
            RedOutcome.UNVERIFIABLE,
            f"selector {selector!r} is not a node id (expected 'path::test')",
            env_id,
        )

    if is_composite_shell_command(config.test_command):
        # Same reasoning as the scoped-test refusal (#139): guessing which
        # component of `a && b && c` accepts a node id is how you run the wrong
        # program and then believe its answer.
        return RedVerification(
            RedOutcome.UNVERIFIABLE,
            "test_command is composite; cannot narrow it to a single node id",
            env_id,
        )

    root = Path(config.project_root)

    # `baseline_sha` is the "red *against what*" of the checkpoint (§3.3), and
    # a pair where the red is not a descendant of its claimed baseline is a
    # false record — cheaper to refuse here than to store and puzzle over. A
    # commit is its own ancestor, so baseline == sha is fine.
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", baseline_sha, sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode != 0:
        return RedVerification(
            RedOutcome.UNVERIFIABLE,
            f"{baseline_sha[:12]} is not an ancestor of {sha[:12]} "
            f"(or one of them does not resolve)",
            env_id,
        )

    parent = tempfile.mkdtemp(prefix="spec-runner-red-")
    worktree = Path(parent) / "tree"
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        return RedVerification(
            RedOutcome.UNVERIFIABLE,
            f"could not check out {sha[:12]}: {added.stderr.strip()[:200]}",
            env_id,
        )

    try:
        return _classify(_run_selector(config, worktree, selector), env_id)
    except Exception as exc:  # a broken replay is unverifiable, never a red
        return RedVerification(RedOutcome.UNVERIFIABLE, f"replay failed: {exc}", env_id)
    finally:
        # Always, including on the exception path: a leaked worktree makes the
        # next `git worktree add` fail and the branch un-deletable. Removal can
        # itself fail (permissions, a transient filesystem), and swallowing
        # that would turn the guarantee above into a hope — so it is logged and
        # followed by a prune, which clears a registration whose directory is
        # already gone.
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(parent, ignore_errors=True)
        if removed.returncode != 0:
            logger.warning(
                "Could not remove the replay worktree; pruning",
                worktree=str(worktree),
                error=removed.stderr.strip()[:200],
            )
            subprocess.run(
                ["git", "worktree", "prune"], cwd=root, capture_output=True, text=True
            )


def _run_selector(
    config: ExecutorConfig, worktree: Path, selector: str
) -> subprocess.CompletedProcess:
    """Run the project's test command, narrowed to one node id, in ``worktree``."""
    # `shell=True` is unavoidable — `test_command` is a shell string by
    # contract — but the selector is not ours: it comes from an agent's output.
    # Interpolating it raw makes `tests/x.py::t; rm -rf ~` a shell command that
    # the harness runs on the operator's machine.
    command = f"{config.test_command} {shlex.quote(selector)}"
    logger.info("Replaying claimed red", selector=selector, worktree=str(worktree))
    return subprocess.run(
        command,
        shell=True,
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=REPLAY_TIMEOUT_SECONDS,
    )


#: "The tests ran and some failed" — the only exit code that can mean a
#: confirmed red. pytest's convention, shared by most runners.
#:
#: Everything else non-zero is treated as *unverifiable*, deliberately without
#: trying to tell the cases apart. Measured on pytest 8: an unresolvable node
#: id and a test file with a syntax error both exit **4**, not the 5 ("no tests
#: collected") one would guess — 5 is for a directory with no tests. Since a
#: wrong guess here would turn "we could not run it" into "it failed", the
#: mapping stays as narrow as what was actually measured, and the exit code
#: goes into the detail so a human can see what happened.
_TESTS_FAILED = 1


def _classify(result: subprocess.CompletedProcess, env_id: str) -> RedVerification:
    if result.returncode == 0:
        return RedVerification(RedOutcome.NOT_RED, "the selector passed on replay", env_id)
    if result.returncode == _TESTS_FAILED:
        return RedVerification(RedOutcome.EXPECTED_FAIL, "the selector failed on replay", env_id)
    output = f"{result.stdout}\n{result.stderr}"
    return RedVerification(
        RedOutcome.UNVERIFIABLE,
        f"the test run exited {result.returncode} without reaching a verdict: {_tail(output)}",
        env_id,
    )


def _tail(output: str, lines: int = 3) -> str:
    kept = [line for line in output.strip().splitlines() if line.strip()][-lines:]
    return " / ".join(kept)[:300]


def record_red_checkpoint(state: ExecutorState, checkpoint: RedCheckpoint) -> None:
    """Persist a verified checkpoint. Append-only, like every other record."""
    state.record_red_checkpoint(
        RedCheckpoint(
            **{
                **checkpoint.__dict__,
                "timestamp": checkpoint.timestamp or datetime.now().isoformat(),
            }
        )
    )


__all__ = [
    "LOCKFILES",
    "REPLAY_TIMEOUT_SECONDS",
    "RedCheckpoint",
    "RedOutcome",
    "RedVerification",
    "environment_id",
    "record_red_checkpoint",
    "resolve_namespace",
    "verify_red",
]
