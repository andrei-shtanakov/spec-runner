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
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .claims import check_claims, describe_violations, ensure_claimable, record_claims
from .git_ops import is_composite_shell_command
from .lifecycle import TddPhase
from .logging import get_logger
from .prompts_log import append_not_started, append_output
from .tdd_runners import (
    ReplayEnvironmentRefusal,
    RunOutcome,
    SelectionProof,
    Selector,
    SelectorRefusal,
    TddRunnerAdapter,
    adapter_for,
    infer_adapter,
    lockfile_identity,
)

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

#: How the authoring pass reports which test it wrote. A marker, not
#: inference: guessing the node id from a diff would make the checkpoint's
#: most load-bearing field a heuristic.
SELECTOR_MARKER = re.compile(r"^\s*TDD_SELECTOR:\s*(\S+)\s*$", re.MULTILINE)

#: Provenance labels for the agent-call ledger (#141 F-6). `green_implementation`
#: keeps its cost on the attempt row, where the state schema already publishes
#: it; the ledger holds the calls that had nowhere else to go.
RED_AUTHORING = "red_authoring"

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

    @property
    def checkpoint_id(self) -> str:
        """A stable, human-copyable id (#141 slice 2).

        Derived rather than the table's rowid: `tdd repair --checkpoint <id>`
        has to be typed by an operator, and a rowid would not survive a state
        rebuild.
        """
        seed = "|".join(
            [self.namespace, self.task_id, self.commit_sha, self.selector, self.timestamp]
        )
        return hashlib.sha256(seed.encode()).hexdigest()[:12]


def environment_id(project_root: Path) -> str:
    """Identify the environment a replay would run in, by lockfile content.

    The generic answer. An adapter that can say more — Elixir records the
    toolchain and the dependency source alongside the lock (#207) — replaces it
    at replay time, and the checkpoint stores whichever is richer.
    """
    return lockfile_identity(project_root)


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


#: The runners whose exit codes have been measured, by name. Kept as a name
#: here because callers and tests ask "which runner is this?"; the behaviour
#: lives in `tdd_runners`, where each answer is one adapter's business.
MEASURED_RUNNERS: dict[str, str] = {"pytest": "pytest"}


def detect_runner(test_command: str) -> str | None:
    """The measured runner behind ``test_command``, or None.

    Token-based, never a substring test: `"pytest" in command` would read
    `mix test --formatter PytestFormatter` as a pytest run, and the whole point
    of #198 is that believing the wrong runner is what produced a false red.
    """
    adapter = infer_adapter(test_command)
    return adapter.name if adapter else None


def resolve_adapter(config: ExecutorConfig) -> TddRunnerAdapter | None:
    """Which adapter judges this project's replays, or None to refuse.

    The explicit `tdd_runner` wins; absent, inference is allowed only where it
    cannot be wrong — an executable that *is* a known runner's. A declared
    runner that the command cannot carry raises, and is refused at config load
    and by `validate` long before a replay; catching it here as well keeps the
    replay honest if it is ever reached another way.
    """
    from .config import ConfigError

    try:
        name = config.resolve_tdd_runner()
    except ConfigError as exc:
        logger.warning("Refusing to verify a red", error=str(exc))
        return None
    return adapter_for(name) if name else None


def verify_red(
    config: ExecutorConfig,
    *,
    sha: str,
    selector: str | Selector,
    baseline_sha: str,
) -> RedVerification:
    """Replay ``selector`` against commit ``sha`` in a disposable worktree."""
    env_id = environment_id(Path(config.project_root))

    # Both refusals below come before anything is executed, and both answer the
    # same question — "can this exit code mean what we would read into it?"
    # Composite first, because it is the more specific statement about a
    # command that also happens to name no single runner.
    if is_composite_shell_command(config.test_command):
        # Same reasoning as the scoped-test refusal (#139): guessing which
        # component of `a && b && c` accepts a node id is how you run the wrong
        # program and then believe its answer.
        return RedVerification(
            RedOutcome.UNVERIFIABLE,
            "test_command is composite; cannot narrow it to a single node id",
            env_id,
        )

    # The runner, before the selector (#198). A red is confirmed by reading an
    # exit code, and an exit code only means what a *specific* runner says it
    # means: pytest exits 1 for "tests failed", ExUnit exits 1 for "the run
    # never happened" and 2 for "tests failed". Reading the second as the first
    # turned a test that was never executed into a confirmed red — silently,
    # with a checkpoint, claims and a satisfied gate behind it.
    adapter = resolve_adapter(config)
    if adapter is None:
        return RedVerification(
            RedOutcome.UNVERIFIABLE,
            f"cannot confirm a red for {selector!r}: no authoritative exit-code "
            f"mapping for test_command {config.test_command!r} — only pytest is "
            "recognised, and guessing another runner's codes is how a test that "
            "never ran becomes a confirmed red",
            env_id,
        )

    # The selector is the adapter's syntax, not a universal one: `::` is
    # pytest's node-id form and nothing outside an adapter may assume it. A
    # caller that already parsed it passes the object, so the string is read
    # exactly once by exactly one adapter.
    parsed = adapter.parse_selector(selector) if isinstance(selector, str) else selector
    if isinstance(parsed, SelectorRefusal):
        return RedVerification(RedOutcome.UNVERIFIABLE, parsed.message, env_id)

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

    prepared = None
    try:
        # Preflight reads the **checkpoint's** source, not the canonical tree.
        # The selector describes a test in the commit being replayed, and the
        # working tree has moved on — measured on a real run, where the agent's
        # new test sat at line 85 of the commit and line 85 of master was
        # something else entirely. Judging the commit is this module's premise;
        # reading the file from anywhere else quietly breaks it.
        refusal = adapter.preflight(worktree, parsed)
        if refusal is not None:
            return RedVerification(RedOutcome.UNVERIFIABLE, refusal.message, env_id)

        # Prove and isolate the environment before running anything (#207).
        # A `git worktree` carries tracked files only, so a language that keeps
        # its dependencies in the project directory has none here — measured on
        # a real Elixir project, where the replay could not compile at all.
        prepared = adapter.prepare_replay(root, worktree, parsed)
        if isinstance(prepared, ReplayEnvironmentRefusal):
            return RedVerification(RedOutcome.UNVERIFIABLE, prepared.message, env_id)
        env_id = prepared.environment_id or env_id
        result = _run_selector(config, worktree, adapter, parsed, prepared.env)
        return _classify(adapter, parsed, result, env_id)
    except Exception as exc:  # a broken replay is unverifiable, never a red
        return RedVerification(RedOutcome.UNVERIFIABLE, f"replay failed: {exc}", env_id)
    finally:
        # The private build goes with the worktree, on every path — success,
        # refusal, timeout and the exception above. A build left behind is
        # state the next replay could read.
        if prepared is not None and not isinstance(prepared, ReplayEnvironmentRefusal):
            for path in prepared.cleanup_paths:
                shutil.rmtree(path, ignore_errors=True)
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
            subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, text=True)


def _run_selector(
    config: ExecutorConfig,
    worktree: Path,
    adapter: TddRunnerAdapter,
    selector: Selector,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the project's test command, narrowed to one test, in ``worktree``."""
    # argv, not a shell string. The selector comes from an agent's output, and
    # `tests/x.py::t; rm -rf ~` must be an argument rather than a command. The
    # previous form quoted it correctly and was one edit away from not doing so;
    # composite commands are refused before this point, so nothing needs a shell.
    argv = adapter.build_command(config.test_command, selector)
    logger.info("Replaying claimed red", selector=str(selector.locator), worktree=str(worktree))
    return subprocess.run(
        argv,
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=REPLAY_TIMEOUT_SECONDS,
        env={**os.environ, **(env or {})},
    )


def _classify(
    adapter: TddRunnerAdapter,
    selector: Selector,
    result: subprocess.CompletedProcess,
    env_id: str,
) -> RedVerification:
    """Two independent answers, combined by one table (#198).

    `classify` says what the run did; `prove_selected` says whether the test we
    asked for is what ran. Only both together can confirm a red — on ExUnit an
    out-of-range line silently runs a *different* test and reports an ordinary
    failure, so an exit code alone cannot mean "the test you named failed".
    """
    outcome = adapter.classify(result)
    proof = adapter.prove_selected(selector, result)
    if proof is SelectionProof.PROVEN:
        if outcome is RunOutcome.TESTS_FAILED:
            return RedVerification(
                RedOutcome.EXPECTED_FAIL, "the selector failed on replay", env_id
            )
        if outcome is RunOutcome.TESTS_PASSED:
            return RedVerification(RedOutcome.NOT_RED, "the selector passed on replay", env_id)
    output = f"{result.stdout}\n{result.stderr}"
    return RedVerification(
        RedOutcome.UNVERIFIABLE,
        f"the test run exited {result.returncode} without reaching a verdict "
        f"({outcome.value}, selection {proof.value}): {_tail(output)}",
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
    "RED_AUTHORING",
    "SELECTOR_MARKER",
    "REPLAY_TIMEOUT_SECONDS",
    "RedCheckpoint",
    "RedOutcome",
    "AgentCall",
    "RedPhaseResult",
    "RedVerification",
    "environment_id",
    "record_red_checkpoint",
    "resolve_namespace",
    "run_red_phase",
    "verify_red",
]


@dataclass(frozen=True)
class RedPhaseResult:
    """What the RED phase established, and the checkpoint it recorded."""

    outcome: RedOutcome
    detail: str | None = None
    checkpoint: RedCheckpoint | None = None
    #: The phase failed to *look*, rather than looking and finding the answer
    #: (#252, #245). The gate can only see that no checkpoint exists; which
    #: kind of nothing that is belongs to whoever tried to observe it, and the
    #: difference is exit 1 versus exit 2.
    instrument_error: bool = False


def run_red_phase(
    task,
    config: ExecutorConfig,
    state: ExecutorState,
    *,
    log_progress=None,
) -> RedPhaseResult:
    """Author a failing test, commit it, and replay it to confirm the red.

    Every *replayed* outcome becomes a checkpoint, including a refuted claim:
    "the agent said red and the replay disagreed" is evidence, and dropping it
    would leave the next run unable to see that this already happened.

    The two failures that happen *before* a replay — no `TDD_SELECTOR` marker,
    and an authoring pass that changed nothing — record no checkpoint, because
    a `RedCheckpoint` is a statement about a commit and there is no commit for
    them to be about. They are not lost: they are returned to the caller, and
    the gate that follows records the `tests` phase outcome in the append-only
    `phase_results` history like any other.
    """
    from .prompt import build_red_prompt

    def _say(line: str) -> None:
        if log_progress:
            log_progress(line)

    baseline = _head(config)
    if not baseline:
        return RedPhaseResult(
            RedOutcome.UNVERIFIABLE, "no commit to author a red against (fresh repo)"
        )

    # F-4: a confirmed red that still covers this tree is evidence, not
    # something to re-derive. Re-authoring on every retry cost an agent call
    # each time and left one red commit and one active checkpoint per attempt —
    # a state the CAS-based remedies do not model.
    _phase(state, config, task, TddPhase.RED_AUTHORING)
    reusable, ambiguity = _reusable_checkpoint(config, state, task)
    if ambiguity:
        return RedPhaseResult(RedOutcome.UNVERIFIABLE, ambiguity)
    if reusable is not None:
        _say(f"\u267b\ufe0f  RED: reusing the confirmed red {reusable.checkpoint_id}")
        return RedPhaseResult(RedOutcome.EXPECTED_FAIL, "reused a confirmed red", reusable)

    # #213: the last point at which refusing costs nothing. A reused red got
    # this far for free, so the guard sits here and not at the top of the
    # phase — a task whose red is already confirmed must not be stopped for a
    # call it was not going to make.
    from .budget import BudgetRefused, check_before_call

    refusal = check_before_call(config, state, task.id, RED_AUTHORING)
    if refusal is not None:
        raise BudgetRefused(refusal)

    _say("\U0001f534 RED: authoring a failing test")
    red_prompt = build_red_prompt(task, config)
    prompt_log = _log_prompt(config, task, red_prompt)
    # A call that never returns still has to close its artefact (Copilot, PR
    # #298). Measured before fixing: with the agent binary missing, and again
    # on a timeout, the file was left holding the prompt alone — the exact
    # shape the invariant reserves for "the runner died mid-call", while the
    # runner was alive and the retry loop carried on.
    try:
        call = _run_agent(config, red_prompt)
    except subprocess.TimeoutExpired:
        append_output(prompt_log, "", note=f"timed out after {config.task_timeout_minutes}m")
        raise
    except OSError as exc:
        # No subprocess, so no spend — the same distinction the ledger draws by
        # writing no row here.
        append_not_started(prompt_log, f"the agent did not launch: {exc}")
        raise
    # Recorded before anything can refuse the result: the call happened and was
    # paid for whether or not it produced something usable.
    state.record_agent_call(
        task.id,
        RED_AUTHORING,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cost_usd=call.cost_usd,
    )
    # Beside the prompt it answered (#295). Until this, the RED artefact held
    # the question and not the answer, on the one call whose output decides
    # which file is frozen for the rest of the task — and it was the most
    # expensive call of the run that found it ($2.69). The ledger knew what the
    # call cost; nothing knew what the money had bought.
    append_output(
        prompt_log,
        call.text,
        call.stderr,
        returncode=call.returncode,
        cost_usd=call.cost_usd,
    )
    output = call.text

    marker = SELECTOR_MARKER.search(output or "")
    if not marker:
        return RedPhaseResult(
            RedOutcome.UNVERIFIABLE,
            "the authoring pass reported no TDD_SELECTOR marker, so there is nothing to replay",
        )
    selector = marker.group(1)

    # Parse **once**, with the adapter this project's config chose, and pass the
    # typed object down the pipeline. Every consumer — lint narrowing, the
    # replay, the byte-lock — used to re-derive the shape from the string, and
    # the byte-lock derived it differently (#210): it split on `::`, so a valid
    # ExUnit selector claimed nothing and a confirmed red was thrown away. One
    # parse, one authority; the string survives only as evidence.
    adapter = resolve_adapter(config)
    if adapter is None:
        return RedPhaseResult(
            RedOutcome.UNVERIFIABLE,
            f"no runner adapter for test_command {config.test_command!r}",
        )
    parsed_selector = adapter.parse_selector(selector)
    if isinstance(parsed_selector, SelectorRefusal):
        return RedPhaseResult(RedOutcome.UNVERIFIABLE, parsed_selector.message)

    sha = _commit_red(config, task, selector)
    if not sha:
        if _staged(config):
            # `_commit_red` returns "" for two different events, and they need
            # different answers (#261). This one is *the commit failed with the
            # authored work staged* — a hook, a lock, a bad identity — and
            # adopting an older commit here would step over work that exists.
            return RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                "the red could not be committed; the authored work is staged and uncommitted",
            )
        sha = _unregistered_red(config, state, task, selector)
        if not sha:
            return RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                "the authoring pass changed nothing, and the tree holds no unregistered red "
                "commit for this task to replay",
            )
        _say(f"♻️  RED: adopting the unregistered red commit {sha[:12]}")
    baseline = _parent_of(config, sha) or baseline

    # #252 D: the evidential test lives in a file of its own. A claim freezes
    # the whole file, so a red written into a file the project already had
    # freezes work the green legitimately needs — the pilot's TASK-101 could
    # not be resumed at all, because its own green had appended tests to the
    # claimed file. Checked here, before anything is claimed or recorded, so a
    # refused red leaves no lock behind.
    novelty = _refuse_pre_existing_file(config, parsed_selector, baseline)
    if novelty is not None:
        return novelty

    # #141 slice 2. Two checks before this commit may become a checkpoint.
    #
    # First: did authoring the red touch a file someone else has frozen? A
    # checkpoint derived from a tree that violates an active claim would make
    # the violation part of the record.
    violations = check_claims(config, state, resolve_namespace(config), sha)
    if violations:
        return RedPhaseResult(
            RedOutcome.UNVERIFIABLE,
            f"the red commit violates an active claim — {describe_violations(violations)}",
        )

    # Second: lint what is about to be frozen. After the checkpoint the file is
    # byte-immutable, so lint debt that got in is uncurable without an operator
    # and hits every later task in the suite — the same I001 trap fired three
    # times in one of the pilot's waves.
    lint_failure = _lint_claimed(config, parsed_selector)
    if lint_failure:
        return RedPhaseResult(RedOutcome.UNVERIFIABLE, lint_failure)

    _phase(state, config, task, TddPhase.RED_VERIFYING, selector)
    _say(f"\U0001f50d RED: replaying {selector}")
    verification = verify_red(config, sha=sha, selector=parsed_selector, baseline_sha=baseline)
    checkpoint = RedCheckpoint(
        task_id=task.id,
        namespace=resolve_namespace(config),
        commit_sha=sha,
        baseline_sha=baseline,
        selector=selector,
        environment_id=verification.environment_id,
        execution_mode="tdd",
        config_hash=_config_hash(config),
        outcome=verification.outcome,
        timestamp=datetime.now().isoformat(),
    )
    # Claims first, checkpoint second. The order is the whole safety property:
    # a process that dies between the two writes must not leave a *confirmed
    # red with no lock* — the red gate would pass while the file it depends on
    # is open for anyone to edit. Written this way, the same crash leaves no
    # confirmed red instead, and the next run re-authors. Found by the battle
    # test, which is what a battle test is for.
    #
    # Only a confirmed red freezes anything: a refuted or unverifiable one is
    # recorded as evidence and locks nothing.
    if verification.outcome is RedOutcome.EXPECTED_FAIL:
        try:
            ensure_claimable(config, parsed_selector)
            record_claims(config, state, checkpoint, parsed_selector)
        except Exception as exc:
            logger.error("Could not claim the red's files", task_id=task.id, error=str(exc))
            return RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                f"the red was confirmed but its files could not be claimed ({exc}); "
                "recording it without the lock would leave the gate passing over an open file",
            )
    state.record_red_checkpoint(checkpoint)
    return RedPhaseResult(verification.outcome, verification.detail, checkpoint)


def _refuse_pre_existing_file(
    config: ExecutorConfig, selector: Selector, baseline: str
) -> RedPhaseResult | None:
    """Refuse a red written into a file that existed at ``baseline`` (#252 D).

    The rule is deliberately about **existence at the baseline**, not about the
    name: an exemption for "a file this task created earlier" would blur a
    simple, provable invariant into a story about who wrote what when. The
    adapter's `evidential_file` is what the prompt asks for; this is what the
    prompt's request has to mean, and the two say the same thing.

    Two failures with different answers, per the owner's decision:

    - the file **existed** — a policy refusal. No checkpoint is recorded, so
      the gate answers "no confirmed red" and the task fails as it does for a
      lint failure: one paid RED call, no retry.
    - git **could not answer** — an instrument error. "We could not look" is
      not "the file is new", and the run must not proceed on an unread index.

    Legacy checkpoints are neither migrated nor reinterpreted: this runs in the
    authoring path only, so a reused or resumed red never passes through it.
    """
    adapter = resolve_adapter(config)
    for path in adapter.claim_paths(selector) if adapter else ():
        if adapter is not None and not adapter.is_discoverable(path):
            return RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                f"the red was written to {path}, which this project's runner does not "
                f"collect — see `{adapter.evidential_file('TASK-ID')}` for the shape it does",
            )
        # `ls-tree`, not `cat-file -e` (Copilot, PR #280). Measured: `cat-file
        # -e` answers **128 for everything** — a path absent from a valid tree,
        # an invalid revision, a directory that is not a repository — and the
        # only difference is the wording of a fatal message. Reading a
        # returncode there let a bad baseline pass as "the file is new", which
        # is #245's rule broken in a new place. `ls-tree` separates the two
        # questions by construction:
        #
        #     rc 0, output    the path is in that tree
        #     rc 0, empty     it is not
        #     rc != 0         git could not answer — never "it is not"
        #
        # It is also the call `check_claims` already uses, so "is this path in
        # that tree" has one answer in this codebase.
        listed = subprocess.run(
            ["git", "ls-tree", "-z", "--name-only", baseline, "--", str(path)],
            cwd=config.project_root,
            capture_output=True,
            text=True,
        )
        if listed.returncode != 0:
            return RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                f"git could not say whether {path} existed at {baseline[:12]} "
                f"({listed.stderr.strip()[:120] or f'exit {listed.returncode}'})",
                instrument_error=True,
            )
        if listed.stdout.strip("\0\n "):
            return RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                f"the red was written into {path}, which already existed before this task "
                "started. A claim freezes the whole file, so the implementation could not "
                "add to it afterwards — write the failing test in a file of its own",
            )
    return None


def _lint_claimed(config: ExecutorConfig, selector: Selector) -> str | None:
    """Lint the file about to be frozen. Returns a refusal, or None.

    Runs **only a linter the project declared** (#220). `lint_command` defaults
    to `uv run ruff check .`, a Python guess; applying it to a project that
    declared no linter is how an Elixir suite got 251 ruff errors on a `.exs`
    file and `execution_mode: tdd` became unusable there. An inferred linter is
    not a gate the project asked for — the same reasoning as `tdd_runner`
    (#198), where a wrong inference was worse than no inference.

    What this is **not** gated on is `hooks.post_done.run_lint`. Those are
    different guarantees: one says "do not gate finished work on lint", this one
    says "do not freeze a file that does not lint". Reusing that switch here
    would be convenient and wrong.

    Narrowed to the claimed file when that is safe. When `lint_command` is
    composite the whole declared gate runs instead of guessing which component
    takes a path — #139's lesson, and deliberately not a second narrowing rule.

    #341 FR-01: a lint failure is not an immediate refusal. When the project
    declared a fix invocation (`lint_fix_command_declared`) and the check
    command is not composite (same exclusion as the check itself, and FR-09:
    a composite command's components are not ours to guess a fix flag for),
    the declared fix command is run narrowed to the same claim paths, and the
    check is repeated before giving up.
    """
    from .claims import claim_paths_for
    from .git_ops import is_composite_shell_command

    if not config.lint_command:
        return None
    if not config.lint_command_declared:
        logger.debug(
            "No commands.lint declared — skipping the pre-freeze lint",
            path=str(selector.path),
        )
        return None
    paths = claim_paths_for(selector)
    if not paths:
        return None

    composite = is_composite_shell_command(config.lint_command)
    check_command = config.lint_command
    if not composite:
        check_command = f"{check_command} {' '.join(shlex.quote(p) for p in paths)}"
    result = subprocess.run(
        check_command,
        shell=True,
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None

    fix_composite = is_composite_shell_command(config.lint_fix_command)
    if (
        not composite
        and not fix_composite
        and config.lint_fix_command_declared
        and config.lint_fix_command
    ):
        fix_command = f"{config.lint_fix_command} {' '.join(shlex.quote(p) for p in paths)}"
        fix_result = subprocess.run(
            fix_command,
            shell=True,
            cwd=config.project_root,
            capture_output=True,
            text=True,
        )
        logger.debug(
            "Ran the declared lint-fix command on the claimed file",
            path=str(selector.path),
            returncode=fix_result.returncode,
        )
        result = subprocess.run(
            check_command,
            shell=True,
            cwd=config.project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return None

    tail = _tail(f"{result.stdout}\n{result.stderr}")
    return (
        f"lint failed on the file about to be frozen ({', '.join(paths)}): {tail}. "
        "After a checkpoint it is byte-immutable, so this must be fixed before the red is fixed."
    )


def _phase(state, config, task, phase, detail=None) -> None:
    """Record a lifecycle transition, never fail a run over one (#141 4a)."""
    import contextlib

    from .lifecycle import IllegalTransition, advance

    # The gates are what refuse; this is the record. Raising here would make
    # bookkeeping a second, weaker enforcement point. Not re-logged either:
    # `advance` already did, with more context (Copilot, PR #259).
    with contextlib.suppress(IllegalTransition):
        advance(state, resolve_namespace(config), task.id, phase, detail)


def _config_hash(config: ExecutorConfig) -> str:
    """The policy this checkpoint was produced under (owner amendment 4)."""
    from .gates import GateContext

    return GateContext(task_id="", checkpoint_sha="", config=config).config_hash


@dataclass(frozen=True)
class AgentCall:
    """What one agent invocation produced *and* what it cost.

    The cost used to be parsed and dropped on the floor, so a TDD run's extra
    call never reached `spec-runner costs` (#141 F-6). Returning it is what
    makes it accountable.
    """

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    #: What the process printed and returned. Carried for the record only
    #: (#295): the RED artefact could not hold the call's answer because this
    #: seam dropped both before the call site ever saw them.
    stderr: str = ""
    returncode: int = 0


def _log_prompt(config: ExecutorConfig, task, prompt: str) -> Path | None:
    """The RED prompt, through the shared writer (#282).

    Returns the path so the call site can append the answer beside it, which is
    the half that was missing (#295).
    """
    from .prompts_log import log_prompt

    return log_prompt(config, task.id, "red", prompt)


def _run_agent(config: ExecutorConfig, prompt: str) -> AgentCall:
    """Run the coding agent once. Seam for tests."""
    from .runner import agent_env, build_cli_invocation, parse_cli_result

    invocation = build_cli_invocation(
        cmd=config.claude_command,
        prompt=prompt,
        model=config.get_model_for_role("implementer"),
        template=config.command_template,
        skip_permissions=config.skip_permissions,
        json_output=True,
    )
    result = subprocess.run(
        invocation.argv,
        capture_output=True,
        text=True,
        timeout=config.task_timeout_minutes * 60,
        cwd=config.project_root,
        env=agent_env(),
    )
    parsed = parse_cli_result(
        invocation.result_format, result.stdout, result.stderr, result.returncode
    )
    return AgentCall(
        text=parsed.text,
        input_tokens=parsed.input_tokens,
        output_tokens=parsed.output_tokens,
        cost_usd=parsed.cost_usd,
        stderr=result.stderr,
        returncode=result.returncode,
    )


def _head(config: ExecutorConfig) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _commit_red(config: ExecutorConfig, task, selector: str) -> str:
    """Commit the authored red. Returns the SHA, or "" when nothing changed.

    A separate commit, not folded into the task's: it is the tree the replay
    judges, and its provenance is the point.
    """
    from .git_ops import stage_all_except_runtime

    if not stage_all_except_runtime(config):
        return ""
    committed = subprocess.run(
        ["git", "commit", "-m", f"{task.id}: red for {selector}"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if committed.returncode != 0:
        logger.warning("Red commit failed", stderr=committed.stderr.strip()[:200])
        return ""
    return _head(config)


def _parent_of(config: ExecutorConfig, sha: str) -> str:
    """``sha``'s first parent, or "" for a root commit.

    The baseline of a red is the tree it was authored against. On the ordinary
    path that is HEAD before the commit — the same thing, read from the commit
    itself so the two cannot disagree; on the adoption path (#261) it is the
    only way to get it, since HEAD *is* the red.
    """
    result = subprocess.run(
        ["git", "rev-parse", f"{sha}^"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _unregistered_red(config: ExecutorConfig, state: ExecutorState, task, selector: str) -> str:
    """A red commit this task left on the branch and never registered (#261).

    A red rejected *after* being committed — for violating a claim, or for lint
    debt in the file about to be frozen — stays on the task branch with no
    checkpoint. The next authoring pass then finds the failing test already in
    the tree, quite reasonably changes nothing, and the phase refuses because
    there is no diff to commit. So one rejected red starved every later attempt,
    each of them paid for.

    The residue is not the problem; discarding it would be. It is the agent's
    work, and #231 is the standing lesson about a tool that deletes work it did
    not like. What was missing is that nothing ever *looked* at it.

    Two conditions, both narrow, because adopting the wrong commit would put a
    checkpoint on a tree nobody proposed:

    - HEAD's subject is exactly what `_commit_red` writes for **this** task and
      **the selector the agent just reported** — an agent that names a different
      test is not talking about this commit;
    - no checkpoint was ever recorded for it, in any status: a commit that had
      one is registered, and re-adopting it would re-litigate whatever retired
      it.

    The subject check subsumes what a baseline comparison would have caught: a
    HEAD that is still the baseline cannot carry this task's red-commit
    subject, because that subject is only ever written by `_commit_red`. And
    the caller has already established that nothing is staged — the difference
    between "there was nothing to commit" and "the commit failed with the work
    pending", of which only the first may be adopted over.
    """
    head = _head(config)
    if not head:
        return ""
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s", head],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if subject.returncode != 0 or subject.stdout.strip() != f"{task.id}: red for {selector}":
        return ""
    if state.checkpoint_exists_for_commit(resolve_namespace(config), head):
        return ""
    return head


def _staged(config: ExecutorConfig) -> bool:
    """Whether anything is staged. Asked of the index rather than the tree
    because the index is what `_commit_red` just prepared: it holds everything
    except the runtime state, which is precisely the set that should have been
    committed. Untracked runtime files are therefore not "dirt" here, and the
    two notions of dirtiness cannot drift apart.

    Fail-closed: an unreadable index counts as staged, since "we could not
    look" must not become "there was nothing there".
    """
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def _reusable_checkpoint(config: ExecutorConfig, state: ExecutorState, task):
    """A confirmed red that still covers this tree, or a reason it cannot be used.

    Matching is deliberately narrow: same workstream and task, same effective
    mode and policy hash (a checkpoint records the question it answered), and
    the red commit must be an **ancestor of HEAD** — a red on a branch this one
    does not descend from proves nothing about this tree.

    Several matches is a **state error**, not a choice. Two active lineages for
    one task mean something upstream is wrong, and quietly taking the newest
    would hide it.
    """
    namespace = resolve_namespace(config)
    candidates = [
        cp
        for cp in state.active_checkpoints(namespace, task.id)
        if cp.outcome is RedOutcome.EXPECTED_FAIL
        and cp.execution_mode == config.resolve_execution_mode(task)
        and cp.config_hash == _config_hash(config)
    ]
    reachable = [cp for cp in candidates if _is_ancestor(config, cp.commit_sha)]
    if len(reachable) > 1:
        listed = ", ".join(cp.checkpoint_id for cp in reachable)
        return None, (
            f"{len(reachable)} active checkpoints match this task ({listed}); "
            "the state is ambiguous — resolve it with `tdd abandon` or `tdd repair`"
        )
    return (reachable[0] if reachable else None), None


def _is_ancestor(config: ExecutorConfig, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=config.project_root,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
