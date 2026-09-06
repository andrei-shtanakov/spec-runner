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
genuine pass or failure eventually drives (#367 FR-08+, later work) — that
stays layered on top of this one. It does, however, compose and judge the
reusability of durable evidence (#367 FR-10/FR-11): `build_verify_evidence`
and `reusable_verify_evidence` live here; only the actual write into `state`
(`ExecutorState.record_verify_evidence`) stays outside this module. A real
`TESTS_FAILED` verdict is only ever an *observation* this module reports,
never a refusal it issues.

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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ExecutorConfig
from .git_ops import is_composite_shell_command
from .task import Task
from .tdd import REPLAY_TIMEOUT_SECONDS, resolve_adapter
from .tdd_runners import ReplayEnvironmentRefusal, RunOutcome, SelectionProof, SelectorRefusal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import ExecutorState

#: The GROUP's timeout ceiling (#375 review round 3, finding 3 / #367 FR-06,
#: NFR-02, tasks-spec resolutions): FR-06 counts the declared ceiling "на
#: ГРУППУ (сумма прогонов)", not per selector — a hung fixture in a five-
#: selector group must not cost `5 * REPLAY_TIMEOUT_SECONDS` before the task
#: even reaches its first paid call. An independent named constant, not a
#: multiple of `REPLAY_TIMEOUT_SECONDS`: each selector's own timeout still
#: shrinks to whatever is left of the group's budget (`min(
#: REPLAY_TIMEOUT_SECONDS, remaining)`), so the two ceilings compose rather
#: than one silently absorbing the other.
VERIFY_GROUP_TIMEOUT_SECONDS = 1800


class VerifyOutcome(str, Enum):
    """The three, and only three, verdicts a live verify run may reach
    (#367 BEH-10/BEH-23). Named so a caller can branch on the outcome
    itself instead of reconstructing it from `ran`/`passed` — a shape a
    genuine `TESTS_FAILED` replay and a refused-before-running instrument
    error used to share.
    """

    GREEN = "green"
    TEST_FAILURE = "test_failure"
    INSTRUMENT_ERROR = "instrument_error"


class ExecutionProof(str, Enum):
    """The third axis of FR-08 (#367 BEH-11/BEH-23): whether the requested
    check was proven to have actually run, as opposed to matched-but-skipped
    (a SKIPPED/XFAIL line still carries the requested node id, so
    `SelectionProof` alone reads it as `PROVEN`) or simply not established.

    Adapters report only the first two values today — `execution_proven`
    answers a plain bool — but the classifier below stays total over all
    three so a future adapter that cannot tell either way still lands on a
    named outcome rather than an implicit fourth one.
    """

    EXECUTED = "executed"
    NOT_EXECUTED = "not_executed"
    UNDETERMINED = "undetermined"


def classify_verify_outcome(
    run_outcome: RunOutcome, proof: SelectionProof, execution: ExecutionProof
) -> VerifyOutcome:
    """The one place FR-08's three axes become one of the three named
    outcomes (#367 BEH-23). Exhaustive by construction: the only two
    combinations that are not `instrument_error` are named explicitly here,
    and every other combination — including `RunOutcome.UNRECOGNIZED`, a
    build/collection error, a refuted or unproven selection, and a
    proven-but-not-executed pass — falls through to `instrument_error`
    rather than an unenumerated fourth value.
    """
    if run_outcome is RunOutcome.TESTS_FAILED and proof is SelectionProof.PROVEN:
        # Attributable to the requested selector — a genuine failure, never
        # read as an instrument that could not tell (FR-14 is later work).
        return VerifyOutcome.TEST_FAILURE
    if (
        run_outcome is RunOutcome.TESTS_PASSED
        and proof is SelectionProof.PROVEN
        and execution is ExecutionProof.EXECUTED
    ):
        # All three facts BEH-11 requires: the run passed, the requested
        # selector (not some other test) is what ran, and it was proven to
        # actually execute rather than merely match while skipped/xfailed.
        return VerifyOutcome.GREEN
    return VerifyOutcome.INSTRUMENT_ERROR


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
    #: The selectors actually presented to the adapter to run, in order —
    #: not merely `task.verifies` again (#367 BEH-15/FR-10): a group that
    #: stops on its second selector's failure has an executed group of
    #: length 1, and a refusal before the loop starts has none at all.
    group_executed: tuple[str, ...] = ()
    #: The adapter that judged the run, by name (e.g. "pytest") — empty when
    #: no adapter was ever resolved (a refusal before that point).
    adapter: str = ""
    #: Named outcome (#367 BEH-10/BEH-23), derived from `ran`/`passed` so
    #: every construction site below gets it without repeating the mapping:
    #: this module only ever produces (True, True)=green,
    #: (True, False)=test_failure, or (False, False)=instrument_error.
    outcome: VerifyOutcome = field(init=False)

    def __post_init__(self) -> None:
        if self.ran and self.passed:
            outcome = VerifyOutcome.GREEN
        elif self.ran:
            outcome = VerifyOutcome.TEST_FAILURE
        else:
            outcome = VerifyOutcome.INSTRUMENT_ERROR
        object.__setattr__(self, "outcome", outcome)


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
    adapter_name = adapter.name

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
            sha,
            False,
            False,
            f"could not check out {sha[:12]}: {added.stderr.strip()[:200]}",
            adapter=adapter_name,
        )

    cleanup_paths: list[Path] = []
    #: Selectors actually presented to the adapter to run, in order — grows
    #: as the loop below runs each one, so a return mid-group carries exactly
    #: what was executed rather than the full declared list (#367 BEH-15).
    presented: list[str] = []
    # The group's budget is logged before the first run, not discovered
    # after the fact (tasks-spec resolution) — an operator watching the log
    # sees the ceiling this group is held to before any selector executes.
    group_deadline = time.monotonic() + VERIFY_GROUP_TIMEOUT_SECONDS
    if log_progress is not None:
        log_progress(
            f"⏳ verify: group budget {VERIFY_GROUP_TIMEOUT_SECONDS}s for "
            f"{len(task.verifies)} selector(s)"
        )
    try:
        # #375 review round 4, finding 2: prepared ONCE for the whole group,
        # not once per selector. Every selector replays the SAME worktree —
        # the same commit and the same environment — so `mix deps` plus a
        # cold compile (ExUnit's `prepare_replay`) has nothing selector-
        # specific to redo; repeating it per selector paid for N cold
        # compiles out of the one group budget instead of one. The `selector`
        # parameter that `prepare_replay` takes is unused by both adapters
        # today (pytest's is a passthrough; ExUnit's only proves the shared
        # deps/build state) — the first selector stands in for the group.
        first_raw = task.verifies[0]
        first_parsed = adapter.parse_selector(first_raw)
        if isinstance(first_parsed, SelectorRefusal):
            return VerifyRunResult(sha, False, False, first_parsed.message, adapter=adapter_name)

        early_refusal = adapter.preflight(worktree, first_parsed)
        if early_refusal is not None:
            return VerifyRunResult(sha, False, False, early_refusal.message, adapter=adapter_name)

        remaining = group_deadline - time.monotonic()
        if remaining <= 0:
            return VerifyRunResult(
                sha,
                False,
                False,
                f"verify group budget ({VERIFY_GROUP_TIMEOUT_SECONDS}s) exhausted "
                "before the replay environment could be prepared",
                adapter=adapter_name,
            )
        if log_progress is not None:
            log_progress("⏳ verify: preparing the replay environment (once for the group)")
        prepared = adapter.prepare_replay(root, worktree, first_parsed)
        if isinstance(prepared, ReplayEnvironmentRefusal):
            return VerifyRunResult(sha, False, False, prepared.message, adapter=adapter_name)
        cleanup_paths.extend(prepared.cleanup_paths)

        for raw_selector in task.verifies:
            remaining = group_deadline - time.monotonic()
            if remaining <= 0:
                return VerifyRunResult(
                    sha,
                    False,
                    False,
                    f"verify group budget ({VERIFY_GROUP_TIMEOUT_SECONDS}s) exhausted "
                    f"before {raw_selector} could run",
                    group_executed=tuple(presented),
                    adapter=adapter_name,
                )

            parsed = adapter.parse_selector(raw_selector)
            if isinstance(parsed, SelectorRefusal):
                # Validated already (`validate._validate_verify_first_declarations`)
                # before a verify_first run can start; reachable only if the
                # tasks file changed between validate and this attempt.
                return VerifyRunResult(
                    sha,
                    False,
                    False,
                    parsed.message,
                    group_executed=tuple(presented),
                    adapter=adapter_name,
                )

            # Re-checked per selector even though `first_parsed` already
            # passed it above: preflight is a per-selector claim (a specific
            # file/line is a valid test), not a group-wide one, and running
            # it again for the first selector is a cheap, pure re-parse — the
            # cost `prepare_replay` above avoids repeating is the compile,
            # not this.
            refusal = adapter.preflight(worktree, parsed)
            if refusal is not None:
                return VerifyRunResult(
                    sha,
                    False,
                    False,
                    refusal.message,
                    group_executed=tuple(presented),
                    adapter=adapter_name,
                )

            argv = adapter.build_scoped_command(config.test_command, parsed)
            presented.append(raw_selector)
            if log_progress is not None:
                log_progress(f"⏳ verify: {raw_selector}")
            # #375 review round 3, finding 3: the per-selector timeout
            # shrinks with the group's remaining budget instead of always
            # being the full `REPLAY_TIMEOUT_SECONDS` — a later selector in
            # the group gets whatever is left, not a fresh allowance.
            remaining = group_deadline - time.monotonic()
            selector_timeout = min(float(REPLAY_TIMEOUT_SECONDS), max(remaining, 0.0))
            result = subprocess.run(
                argv,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=selector_timeout,
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
            execution = (
                ExecutionProof.EXECUTED
                if adapter.execution_proven(parsed, result)
                else ExecutionProof.NOT_EXECUTED
            )
            tail = _tail(f"{result.stdout}\n{result.stderr}")

            # #367 BEH-11/BEH-23: the decision goes through the one
            # exhaustive classifier, per selector — an aggregate "the run
            # overall passed" is never enough (`prove_selected` answers about
            # the requested selector, not the set).
            verify_outcome = classify_verify_outcome(outcome, proof, execution)

            if verify_outcome is VerifyOutcome.TEST_FAILURE:
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
                    group_executed=tuple(presented),
                    adapter=adapter_name,
                )

            if verify_outcome is VerifyOutcome.GREEN:
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
            return VerifyRunResult(
                sha,
                False,
                False,
                f"{reason}: {tail}",
                group_executed=tuple(presented),
                adapter=adapter_name,
            )
        # BEH-08's evidence clause: name the group AS EXECUTED, not just that
        # something passed (#375 review round 2, finding 4). Every selector
        # in `task.verifies` was presented to the adapter in order — the loop
        # above returns early on the first refusal or non-pass verdict — so
        # by the time this line is reached "as executed" and "as declared"
        # are the same list, and naming it here is what lets a reader
        # confirm that rather than take it on faith.
        executed = ", ".join(task.verifies)
        return VerifyRunResult(
            sha,
            True,
            True,
            f"declared group passed: {executed}",
            group_executed=tuple(presented),
            adapter=adapter_name,
        )
    except subprocess.TimeoutExpired as exc:
        return VerifyRunResult(
            sha,
            False,
            False,
            f"verify run timed out: {exc}",
            group_executed=tuple(presented),
            adapter=adapter_name,
        )
    except Exception as exc:  # a broken replay is unverifiable, never a pass
        return VerifyRunResult(
            sha,
            False,
            False,
            f"verify run failed: {exc}",
            group_executed=tuple(presented),
            adapter=adapter_name,
        )
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


@dataclass(frozen=True)
class VerifyEvidence:
    """A durable record of one live verify-first run (#367 FR-10/BEH-15).

    Distinct from `tdd.RedCheckpoint` on purpose (#367 FR-12/BEH-19): a
    green-only run never wrote a red, and a reader of red checkpoints that
    does not know this record exists must keep answering "no red" for it —
    which a shared table or a shared shape could not guarantee.

    The composition is FR-10's own list: task/workstream identity, the
    judged commit, the group as declared and as executed, the policy config
    hash, the environment identity, the judging adapter, the outcome, the
    failure detail (in the refusal's own words), when, and the harness as
    the record's author — enough for a third party to reproduce the run
    without the original log (BEH-16).
    """

    task_id: str
    namespace: str
    commit_sha: str
    group_declared: tuple[str, ...]
    group_executed: tuple[str, ...]
    config_hash: str
    environment_id: str
    adapter: str
    outcome: str
    detail: str
    timestamp: str
    actor: str = "harness"


def _evidence_config_hash(config: ExecutorConfig, task_id: str, commit_sha: str) -> str:
    """The same `POLICY_KEYS` hash the gates use, for the same reason (#367
    FR-11): an evidence row is a statement about a tree under a policy, and
    the hash is what lets a later attempt tell whether the policy moved.
    """
    from .gates import GateContext

    return GateContext(task_id=task_id, checkpoint_sha=commit_sha, config=config).config_hash


def build_verify_evidence(
    task: Task, config: ExecutorConfig, result: VerifyRunResult
) -> VerifyEvidence:
    """Assemble the durable record for one `VerifyRunResult` (#367 BEH-15).

    Pure — no I/O, no state. `ExecutorState.record_verify_evidence` calls
    this and persists the result; kept separate so the composition can be
    inspected or reproduced without a database.
    """
    from .tdd import environment_id, resolve_namespace

    return VerifyEvidence(
        task_id=task.id,
        namespace=resolve_namespace(config),
        commit_sha=result.sha,
        group_declared=tuple(task.verifies or ()),
        group_executed=result.group_executed,
        config_hash=_evidence_config_hash(config, task.id, result.sha),
        environment_id=environment_id(Path(config.project_root)),
        adapter=result.adapter,
        outcome=result.outcome.value,
        detail=result.detail,
        timestamp=datetime.now().isoformat(),
    )


def reusable_verify_evidence(
    config: ExecutorConfig, state: ExecutorState, task: Task
) -> VerifyEvidence | None:
    """A prior verify-evidence row that still answers this task's question,
    or `None` if a fresh live run is required (#367 FR-11, Q-07(a)).

    Five axes, all of which must hold — the same rule `_reusable_checkpoint`
    already applies to a red, extended by the tree-hash axis a *pre-run*
    reuse decision needs and a pre-merge gate acceptance does not (Q-07):

    - the recorded outcome is green (#375 review, finding 2): the same rule
      `_reusable_checkpoint` applies via `RedOutcome.EXPECTED_FAIL` — a
      `test_failure` or `instrument_error` row answered its question with
      "no" or "could not tell", never "yes, skip the live run";
    - the policy config hash matches (BEH-17: a `POLICY_KEYS` value,
      including `execution_mode`/`tdd_runner`, changed the question);
    - the declared group matches **as a sequence** — reordering
      `**Verifies:**` is a different question too (BEH-18);
    - the candidate descends from the evidence's commit, by the same
      `_descends_from` rule (and the same `AncestryUnknown` on unprovable
      ancestry) the red gate uses (BEH-18a) — propagated to the caller
      rather than swallowed, since "could not tell" and "no" are different
      facts with different owners;
    - the candidate's tree is byte-identical to the evidence commit's tree:
      descent alone would accept a candidate with new commits on top, which
      a *pre-run* skip must not do — the code under test could have moved.
    """
    from .tdd import resolve_namespace

    namespace = resolve_namespace(config)
    evidence = state.verify_evidence(namespace, task.id)
    if evidence is None:
        return None

    if evidence.outcome != VerifyOutcome.GREEN.value:
        return None
    if evidence.config_hash != _evidence_config_hash(config, task.id, evidence.commit_sha):
        return None
    if list(evidence.group_declared) != list(task.verifies or ()):
        return None

    from .gates import _descends_from

    if not _descends_from(config, evidence.commit_sha, "HEAD"):
        return None
    old_tree = _tree_hash(config, evidence.commit_sha)
    new_tree = _tree_hash(config, "HEAD")
    # An unreadable tree on either side is "could not tell", not "same tree"
    # (`None != None` is `False`) — silently allowing reuse there would let
    # a git-level failure masquerade as a byte-identical match, defeating
    # the exact guarantee this axis exists for (#367 BEH-18a).
    if old_tree is None or new_tree is None or old_tree != new_tree:
        return None
    return evidence


def _tree_hash(config: ExecutorConfig, commit_sha: str) -> str | None:
    """The git tree object a commit points at, or `None` if it cannot be read."""
    result = subprocess.run(
        ["git", "rev-parse", f"{commit_sha}^{{tree}}"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()
