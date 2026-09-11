"""File claims — the byte-lock behind a confirmed RED (#141 slice 2).

A claim says: *this file, at these bytes, is frozen, because a confirmed RED
depends on it.* The pilot's first version checked only the file of the current
selector, so neighbouring tests were protected by a sentence in the agent's
prompt rather than by the instrument. Enforcement here covers **every active
claim in the namespace**, whoever made it.

Two properties are load-bearing and easy to lose:

- **Authoritative against the candidate commit**, never the working tree. A
  check against a mutable tree answers a question about a moment that has
  already passed by the time anything acts on the answer — the same reason the
  RED replay judges a commit.
- **Raw bytes.** A claim that tolerates a CRLF flip is not a byte-lock, so the
  hash is git's own blob SHA over the file's bytes with no normalisation.

Contract: ``docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md`` §1
"""

from __future__ import annotations

import posixpath
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .logging import get_logger
from .spec import git_blob_hash

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .state import ExecutorState
    from .task import Task as TaskT
    from .tdd import RedCheckpoint
    from .tdd_runners import Selector

logger = get_logger("claims")


class ClaimCheckError(RuntimeError):
    """The claims could not be checked. Never confused with "no violations"."""


class ClaimRefused(RuntimeError):
    """A file a confirmed red depends on cannot be claimed.

    Fail-closed by construction: a red whose files are not locked is a red the
    gate would pass over an open file, which is the hole the byte-lock exists
    to close. Better to refuse the red than to record it without its lock.
    """


class ClaimStatus(str, Enum):
    """A claim's life. Nothing is ever deleted — see slice 3."""

    ACTIVE = "active"
    #: Replaced by a later lineage (`tdd repair`).
    SUPERSEDED = "superseded"
    #: The red it belonged to was given up on (`tdd abandon`).
    ABANDONED = "abandoned"
    #: The task it protected completed (#260). Distinct from the two above
    #: because nothing went wrong: a claim guards the evidence between the
    #: confirmed red and the terminal gate, and after that gate there is no
    #: lifecycle left to protect. Holding the lock past completion froze the
    #: file for the whole workstream forever — every later legitimate edit
    #: (a review fix, a refactor, a new test in the same file) wedged every
    #: subsequent task, so a workstream degraded exactly as its code lived.
    RELEASED = "released"


class ViolationKind(str, Enum):
    """All three block. They are distinguished because they send the operator
    looking in different places."""

    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass(frozen=True)
class Claim:
    namespace: str
    task_id: str
    checkpoint_id: str
    checkpoint_sha: str
    path: str
    blob_sha: str
    created_at: str
    status: ClaimStatus = ClaimStatus.ACTIVE


@dataclass(frozen=True)
class ClaimViolation:
    kind: ViolationKind
    path: str
    task_id: str
    checkpoint_id: str
    detail: str | None = None


def claim_paths_for(selector: Selector) -> list[str]:
    """The files a **parsed** selector claims.

    Takes the typed object, not the raw string: the selector was parsed once by
    the adapter the config chose, and re-deriving it here would distribute the
    authority again. Today `::` and `:line` do not overlap, so a loop over the
    adapters happens to give the right answer — but a third adapter could make
    one string parse under two of them, and then the order of `ADAPTERS` would
    become hidden semantics of the byte-lock (owner's review, PR #211).

    The raw string survives only as evidence: it is what the agent said, and it
    is what a stored checkpoint displays.
    """
    from .tdd_runners import adapter_for

    adapter = adapter_for(selector.runner)
    if adapter is None:
        # A selector whose runner is no longer registered claims nothing, and
        # a red with nothing locked would pass the gate over an open file.
        return []
    return [str(path) for path in adapter.claim_paths(selector)]


#: The heading every agent-facing frozen-files block carries. One string, so a
#: test can assert the block reached a prompt without reproducing its prose.
FROZEN_HEADER = "TDD FROZEN FILES — do not modify, delete, rename or replace:"

#: What an implementation pass says when the work cannot be done without
#: touching a frozen file. `TASK_BLOCKED: <reason>` is the form `execution`
#: parses; "TASK_BLOCKED" alone is read as a failure with no reason given.
ESCAPE_TASK = (
    "If the task cannot be completed without changing one, stop and report\n"
    "TASK_BLOCKED: <reason>. Only an operator may abandon or repair a claim."
)

#: The same rule for a reviewer. A review pass has a different vocabulary —
#: `TASK_BLOCKED` is not a marker `review` parses, so telling a reviewer to
#: emit it would produce "no verdict" and, under `review_policy: required`,
#: block the task for a reason that is not the truth. `REVIEW_FAILED` is the
#: honest marker for "there is an issue and I did not fix it"; `REVIEW_FIXED`
#: after editing a frozen file is exactly what must not happen.
ESCAPE_REVIEW = (
    "If a finding cannot be fixed without changing one, do NOT fix it: report\n"
    "REVIEW_FAILED and describe the finding. Editing a frozen file is not a\n"
    "review fix — only an operator may abandon or repair a claim."
)


def active_claim_paths(config: ExecutorConfig, state: ExecutorState | None = None) -> list[str]:
    """Every path frozen in this project's namespace, whoever froze it.

    Not filtered by task, for the same reason `check_claims` is not: the gate
    judges every active claim in the namespace, so every pass that can produce
    a candidate has to be told about every one of them.
    """
    from .tdd import resolve_namespace

    namespace = resolve_namespace(config)
    if state is not None:
        return sorted({c.path for c in state.active_claims(namespace)})

    from .state import ExecutorState as _State

    try:
        with _State(config) as opened:
            return sorted({c.path for c in opened.active_claims(namespace)})
    except Exception as exc:
        # Degraded, not a hole: the instrument still checks the claims against
        # the candidate commit. What is lost is the agent's chance to comply,
        # so it is logged rather than passed over in silence.
        logger.warning("Could not read active claims for the prompt", error=str(exc))
        return []


def frozen_files_block(paths: list[str], escape: str = ESCAPE_TASK) -> str:
    """The block itself, or "" when nothing is frozen."""
    if not paths:
        return ""
    listed = "\n".join(f"- {path}" for path in paths)
    return f"{FROZEN_HEADER}\n{listed}\n\n{escape}"


def append_frozen_files(
    prompt: str,
    config: ExecutorConfig,
    task: TaskT,
    *,
    state: ExecutorState | None = None,
    escape: str = ESCAPE_TASK,
) -> str:
    """Append the frozen-files block to an already-rendered prompt.

    **After rendering, never inside a template.** A project with its own
    `task` or `review` template renders from its own variables, so a block
    delivered as one more substitution would silently vanish for exactly the
    projects that customised the most — and the constraint would be missing
    from the prompt while the gate went on enforcing it. A template variable
    may exist for placement; this append is what makes it arrive.

    Dormant outside `tdd`/`verify_first`, but not because `active_claim_paths`
    is scoped to either: it "has rows for a task whose red was authored and
    frozen through `tdd._judge_red_commit`" is the wrong premise to reason
    from — `active_claim_paths` (and the `check_claims` the gate calls) reads
    every active claim in the *namespace*, whoever froze it, not the current
    task's own. A `verify_first` task can sit in a namespace another
    workstream's `tdd` task already froze a file in, and #380 review
    confirmed `evaluate_claims`/the pre-review claims check now judge that
    task too (BEH-24/FR-17, `gates.py`/`hooks.py`) — so this site staying
    `tdd`-only was #214 itself reopened: enforcement covered `verify_first`
    while the prompt telling the agent what is frozen did not, and the money
    burned proving that is exactly what #214 was written to stop. `standard`
    is the one mode still excluded on purpose — its claims gate is never
    registered, so there is truthfully nothing this site could tell it about.
    """
    # #429: an addressed-waived `standard` task gets the block too. The
    # waiver removes the baseline-RED requirement and NOTHING else — claims
    # are still checked at all three points, so a prompt that did not name
    # the frozen paths would be telling the agent to work blind against a
    # gate that will still refuse. Ordinary `standard` is untouched: the
    # predicate is `resolve_waiver`, which returns None without a marker.
    if (
        config.resolve_execution_mode(task) not in ("tdd", "verify_first")
        and config.resolve_waiver(task) is None
    ):
        return prompt
    block = frozen_files_block(active_claim_paths(config, state), escape)
    if not block:
        return prompt
    return f"{prompt.rstrip()}\n\n{block}\n"


def validate_claim_path(project_root: Path, path: str) -> str | None:
    """Return a refusal reason, or None when ``path`` may be claimed.

    Rejected rather than normalised, because each of these breaks what a claim
    is for: a symlink's bytes are its target's, so hashing it freezes something
    the claim does not name, and a path outside the repo is in no commit, so
    there is nothing to check it against.
    """
    root = Path(project_root).resolve()

    # Project-relative and canonical, or nothing. A claim's `path` is compared
    # against `git ls-tree` keys, which are always canonical and relative — so
    # an absolute path, or one carrying `.`/`..`, would never match its own
    # entry and would read as DELETED on a tree where the file is untouched.
    # A false violation is worse than a refusal: it blocks work for a reason
    # that is not true.
    if Path(path).is_absolute():
        return f"{path!r} is absolute; a claim path must be project-relative"
    if path != posixpath.normpath(path) or path.startswith("../"):
        return f"{path!r} is not canonical; a claim path must be normalised and inside the tree"

    candidate = (root / path).resolve()
    if root not in candidate.parents and candidate != root:
        return f"{path!r} resolves outside the repository"
    # `is_symlink` on the unresolved path: `resolve()` has already followed it.
    unresolved = root / path
    if unresolved.is_symlink():
        return f"{path!r} is a symlink; a claim must name the bytes it freezes"
    if not candidate.exists():
        return f"{path!r} does not exist"
    if not candidate.is_file():
        return f"{path!r} is not a regular file"
    return None


def claim_blob_sha(project_root: Path, path: str) -> str:
    """Git blob SHA over the file's raw bytes — no line-ending normalisation."""
    return git_blob_hash((Path(project_root) / path).read_bytes())


def selector_of(config: ExecutorConfig, checkpoint: RedCheckpoint) -> Selector | None:
    """Parse a **stored** checkpoint's selector, with the config's adapter.

    A record in the database holds the raw string, so somewhere it has to be
    read again. The authority for that reading is the adapter the config
    chose — never a loop over the registry, which is what would make the order
    of `ADAPTERS` into hidden semantics of the byte-lock.
    """
    from .tdd import resolve_adapter
    from .tdd_runners import Selector

    adapter = resolve_adapter(config)
    if adapter is None:
        return None
    parsed = adapter.parse_selector(checkpoint.selector)
    return parsed if isinstance(parsed, Selector) else None


def record_claims(
    config: ExecutorConfig,
    state: ExecutorState,
    checkpoint: RedCheckpoint,
    selector: Selector | None = None,
) -> list[Claim]:
    """Claim the files ``checkpoint``'s selector depends on.

    Identity is ``(task, lineage, path, bytes)`` — **not** ``(path, bytes)``.
    Two tasks can legitimately depend on the same file at the same content, and
    keying on the bytes alone meant the second one recorded nothing: its
    dependency was invisible, so the first task's `abandon` released a file the
    second still needed (F-3). Re-claiming within one lineage is still
    idempotent; a re-run must not stack duplicate rows.
    """
    root = Path(config.project_root)
    # The live pipeline hands the parsed object down; a caller holding only a
    # stored record gets it read back by the config's adapter.
    resolved = selector or selector_of(config, checkpoint)
    if resolved is None:
        raise ClaimRefused(
            f"selector {checkpoint.selector!r} cannot be parsed by this project's "
            "runner adapter, so nothing can be claimed"
        )
    ensure_claimable(config, resolved)
    existing = {
        (c.task_id, c.checkpoint_id, c.path, c.blob_sha)
        for c in state.active_claims(checkpoint.namespace)
    }
    recorded: list[Claim] = []

    for path in claim_paths_for(resolved):
        blob = claim_blob_sha(root, path)
        if (checkpoint.task_id, checkpoint.checkpoint_id, path, blob) in existing:
            continue
        claim = Claim(
            namespace=checkpoint.namespace,
            task_id=checkpoint.task_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_sha=checkpoint.commit_sha,
            path=path,
            blob_sha=blob,
            created_at=datetime.now().isoformat(),
        )
        state.record_claim(claim)
        recorded.append(claim)
    return recorded


def record_verify_group_claims(
    config: ExecutorConfig,
    state: ExecutorState,
    task: TaskT,
    sha: str,
    selectors: list[str],
) -> list[Claim]:
    """Freeze a green-on-entry `verify_first` task's declared group (#367
    BEH-26/FR-19, TASK-010).

    BEH-20's green-only path never authors a red, so `record_claims` (called
    from `tdd.py::_judge_red_commit`) never runs for it — the group's
    evidential files stay open for the paid implementation pass that follows,
    which can rewrite what the live entry run proved while keeping it green.
    Only a byte-lock catches that; a second live re-verify would find nothing
    wrong with the rewrite either.

    Not a `RedCheckpoint`: BEH-20 reaches GREEN on verify evidence, never "a
    red recorded under another name" (see `lifecycle.has_verify_evidence`'s
    own docstring), so nothing here is written to `red_checkpoints`. A
    checkpoint identity is synthesised only long enough to give each path's
    `Claim` the same `(task, lineage, path, bytes)` shape `record_claims`
    already produces for a confirmed red — and, precisely because nothing is
    written to `red_checkpoints`, to give `remedy.release` a way to tell a
    verify-freeze claim apart from one a confirmed red still depends on: a
    `checkpoint_id` that resolves to no row there is exactly such a claim
    (#383 review, finding 1).

    Two properties a plain `record_claims` call cannot give this site.

    From #383 review round 2, finding 1 — **re-entry reuses the standing
    freeze, it never re-baselines it.** A retry re-freezing at whatever the
    *current* judged commit happens to contain is how a paid pass's own
    rewrite gets laundered through: attempt 1 freezes the group green at B1;
    a paid pass weakens that very test (still green) and `wants_candidate`
    commits it; a transient, unrelated refusal (an unrunnable re-verify) ends
    the attempt *before* any claims check ever runs, so the rewrite is never
    judged. If attempt 2's entry run then reads green off that same
    (weakened) HEAD and this function re-baselined onto it, the lock would
    now agree with the very bytes it exists to catch — `check_claims` would
    find nothing wrong, because the standing evidence would just be the
    rewrite. So a path this task already holds a verify-freeze claim on is
    **reused as-is**: the existing claim is left untouched, and no second
    claim is written for it. A judged tree that disagrees with a standing
    freeze is a claims violation for `check_claims`/`evaluate_claims` to
    catch wherever the RED gate already runs — not something this call may
    quietly resolve by moving the lock. Supersession is reserved for
    retirement (`release`/DONE — `_release_claims`, `remedy.release`), never
    for re-baselining a lock that is still standing. Reuse is scoped to
    lineages *this function itself synthesised* (a `checkpoint_id` with no
    row in `red_checkpoints`): BEH-21 walks a task through the ordinary
    RED-authoring cycle on one attempt and back to a green entry on a later
    one, and the genuine confirmed-red claim that earlier attempt took is a
    different lineage this call must not touch at all.

    From #383 review round 2, finding 2 — **claimability is judged against
    the same commit the bytes come from.** The live entry run judges `sha` in
    a disposable worktree (BEH-09); the project's real working tree can
    differ from it — most sharply when a prior, interrupted attempt left a
    file deleted or replaced there without committing. Checking claimability
    against `project_root` on disk (as `ensure_claimable` does for the
    RED-authoring path, where the working tree and the just-authored commit
    are the same thing) would refuse a file that is present and intact in
    `sha`, over a tree gap this module's own docstring already says must not
    matter.
    """
    from .tdd import RedCheckpoint, RedOutcome, resolve_adapter, resolve_namespace
    from .tdd_runners import Selector, parse_group_element

    adapter = resolve_adapter(config)
    if adapter is None:
        raise ClaimRefused(
            f"no adapter can confirm test_command {config.test_command!r}; "
            "the declared group cannot be claimed"
        )
    namespace = resolve_namespace(config)
    root = Path(config.project_root)
    tree = _tree_entries(root, sha)
    if tree is None:
        raise ClaimRefused(
            f"cannot read the judged commit {sha[:12]}; the declared group cannot be claimed"
        )

    # This task's own standing verify-freeze claims — unbacked checkpoint_id,
    # the ones this function itself made — are read once, up front, so the
    # loop below can reuse rather than duplicate them. A genuine confirmed-red
    # claim (BEH-21) is excluded by the same check and is never touched here.
    standing_by_path = {
        c.path: c
        for c in state.active_claims(namespace)
        if c.task_id == task.id and state.checkpoint_by_id(namespace, c.checkpoint_id) is None
    }

    to_claim: list[tuple[RedCheckpoint, str, str]] = []
    with _judged_worktree(root, sha) as worktree:
        for raw_selector in selectors:
            parsed = parse_group_element(adapter, raw_selector, worktree)
            if not isinstance(parsed, Selector):
                raise ClaimRefused(
                    f"{raw_selector!r} cannot be parsed by this project's runner adapter, "
                    "so nothing can be claimed"
                )
            checkpoint = RedCheckpoint(
                task_id=task.id,
                namespace=namespace,
                commit_sha=sha,
                baseline_sha=sha,
                selector=raw_selector,
                environment_id="verify_first",
                execution_mode="verify_first",
                config_hash="",
                outcome=RedOutcome.EXPECTED_FAIL,
                timestamp="",
            )
            for path in _ensure_claimable_at_commit(tree, parsed):
                if path in standing_by_path:
                    continue
                _mode, blob = tree[path]
                to_claim.append((checkpoint, path, blob))

    recorded: list[Claim] = []
    for checkpoint, path, blob in to_claim:
        claim = Claim(
            namespace=checkpoint.namespace,
            task_id=checkpoint.task_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_sha=checkpoint.commit_sha,
            path=path,
            blob_sha=blob,
            created_at=datetime.now().isoformat(),
        )
        state.record_claim(claim)
        recorded.append(claim)
    return recorded


def release_claims(state: ExecutorState, namespace: str, task_id: str) -> int:
    """Retire ``task_id``'s claims because the task finished (#260).

    A claim protects the evidential test from the confirmed red until the
    terminal gate. Past that gate the lifecycle it guarded is over, the work
    has been through whatever human gate the project has, and the lock protects
    nothing — while still costing everything: the pilot's three completed tasks
    held their test files frozen for the whole workstream, so a review fix and
    an assertion hardening (both merged through that human gate) wedged the
    *next* task's red before it could be authored.

    Retired, not deleted, like every other status change here: what was
    believed and when is evidence too. The count is returned so the caller can
    say what it did — a release that quietly touched nothing reads the same as
    one that unlocked a file.
    """
    return state.supersede_claims(namespace, task_id, ClaimStatus.RELEASED)


def ensure_claimable(config: ExecutorConfig, selector: Selector) -> list[str]:
    """The paths ``selector`` will claim, or raise `ClaimRefused`.

    Called *before* anything is written, so a red whose file cannot be locked
    is refused rather than recorded lock-less. Skipping an unclaimable path
    with a warning — as an earlier version did — produced exactly the state
    this module exists to prevent: a confirmed red the gate passes, over a file
    nobody is protecting.
    """
    paths = claim_paths_for(selector)
    if not paths:
        raise ClaimRefused(
            f"selector {selector!r} names no file to claim; a red with nothing locked "
            "would pass the gate over an open file"
        )
    root = Path(config.project_root)
    for path in paths:
        refusal = validate_claim_path(root, path)
        if refusal:
            raise ClaimRefused(f"cannot claim {path}: {refusal}")
    return paths


def check_claims(
    config: ExecutorConfig,
    state: ExecutorState,
    namespace: str,
    candidate_sha: str,
) -> list[ClaimViolation]:
    """Every active claim in ``namespace``, checked against ``candidate_sha``."""
    claims = state.active_claims(namespace)
    if not claims:
        return []

    root = Path(config.project_root)
    tree = _tree_blobs(root, candidate_sha)
    if tree is None:
        # Same fail-closed reasoning as `record_claim`: "we could not read the
        # tree" is not "the claims are intact". Returning [] here would make an
        # unreadable commit look like a clean one and pass the gate.
        raise ClaimCheckError(f"cannot read the candidate commit {candidate_sha[:12]}")
    by_blob: dict[str, list[str]] = {}
    for path, blob in tree.items():
        by_blob.setdefault(blob, []).append(path)

    violations: list[ClaimViolation] = []
    for claim in claims:
        present = tree.get(claim.path)
        if present == claim.blob_sha:
            continue
        # A claim with no backing `red_checkpoints` row is a verify-first
        # freeze (`record_verify_group_claims`), never a genuine confirmed
        # red — the one door that can retire it without DONE is `tdd
        # release`, not `abandon`/`repair` (#383 review round 2, finding 3:
        # a neighbour blocked by a stale one had no way to tell which kind it
        # was hitting, or that a door for it exists at all).
        door = (
            f"; a verify-first freeze with no confirmed red behind it — if "
            f"{claim.task_id} never reached DONE, an operator can retire it via "
            f"`spec-runner tdd release {claim.task_id}`"
            if state.checkpoint_by_id(namespace, claim.checkpoint_id) is None
            else ""
        )
        if present is not None:
            violations.append(
                ClaimViolation(
                    ViolationKind.MODIFIED,
                    claim.path,
                    claim.task_id,
                    claim.checkpoint_id,
                    f"claimed {claim.blob_sha[:12]}, found {present[:12]}{door}",
                )
            )
            continue
        elsewhere = [p for p in by_blob.get(claim.blob_sha, []) if p != claim.path]
        if elsewhere:
            violations.append(
                ClaimViolation(
                    ViolationKind.RENAMED,
                    claim.path,
                    claim.task_id,
                    claim.checkpoint_id,
                    f"the claimed bytes are now at {', '.join(sorted(elsewhere))}{door}",
                )
            )
        else:
            violations.append(
                ClaimViolation(
                    ViolationKind.DELETED,
                    claim.path,
                    claim.task_id,
                    claim.checkpoint_id,
                    f"the path is gone and its bytes are nowhere in the tree{door}",
                )
            )
    return violations


@contextmanager
def _judged_worktree(root: Path, sha: str) -> Iterator[Path]:
    """A disposable `git worktree` checked out at ``sha``, removed on exit.

    `parse_group_element`'s file-target branch resolves existence, symlink-
    ness and discoverability against whatever path it is handed — the same
    machinery `live_verify.py` points at a worktree like this one for the
    live entry run (BEH-09). Handing it `project_root` instead would judge a
    file target against the *working* tree, exactly the gap `record_claims`'s
    module docstring and `_ensure_claimable_at_commit` (#383 review round 2,
    finding 2) exist to rule out for node ids — a file present and intact in
    `sha` but deleted or replaced on disk by a prior, interrupted attempt
    would be refused for no reason the judged commit itself supports.
    """
    parent = tempfile.mkdtemp(prefix="spec-runner-claim-")
    worktree = Path(parent) / "tree"
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        raise ClaimRefused(
            f"could not check out {sha[:12]} to judge the declared group: "
            f"{added.stderr.strip()[:200]}"
        )
    try:
        yield worktree
    finally:
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, text=True)
        shutil.rmtree(parent, ignore_errors=True)


def _tree_entries(root: Path, sha: str) -> dict[str, tuple[str, str]] | None:
    """``{path: (mode, blob sha)}`` for every blob in ``sha``, or None if
    unreadable.

    Read straight from the object database — `git ls-tree` of a commit cannot
    be influenced by the working tree, which is the point. The mode is kept
    alongside the blob so a caller can tell a symlink (`120000`) from a
    regular file without a second look at the working tree — see
    `_ensure_claimable_at_commit`, which judges claimability from this alone
    (#383 review round 2, finding 2).
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--full-tree", "-z", sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    entries: dict[str, tuple[str, str]] = {}
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            entries[path] = (parts[0], parts[2])
    return entries


def _tree_blobs(root: Path, sha: str) -> dict[str, str] | None:
    """``{path: blob sha}`` for every file in ``sha``, or None if unreadable.

    `_tree_entries` with the mode dropped — kept as its own function because
    most callers (`check_claims`) only ever needed the blob.
    """
    entries = _tree_entries(root, sha)
    if entries is None:
        return None
    return {path: blob for path, (_mode, blob) in entries.items()}


def _ensure_claimable_at_commit(tree: dict[str, tuple[str, str]], selector: Selector) -> list[str]:
    """Like `ensure_claimable`, but judged against a commit's tree — never
    the working tree (#383 review round 2, finding 2).

    `ensure_claimable` checks `project_root` on disk, which the RED-authoring
    path can rely on because the working tree and the commit it just authored
    are the same thing at that moment. A verify-first freeze has no such
    guarantee: the live entry run judges `sha` in a disposable worktree
    (BEH-09), and the project's real working tree can differ from it — a
    prior, interrupted attempt leaving a file deleted or replaced there is
    exactly the gap this function must not mistake for the file being gone.
    """
    paths = claim_paths_for(selector)
    if not paths:
        raise ClaimRefused(
            f"selector {selector!r} names no file to claim; a freeze with nothing locked "
            "would pass the gate over an open file"
        )
    for path in paths:
        refusal = _validate_claim_path_in_tree(path, tree)
        if refusal:
            raise ClaimRefused(f"cannot claim {path}: {refusal}")
    return paths


def _validate_claim_path_in_tree(path: str, tree: dict[str, tuple[str, str]]) -> str | None:
    """Return a refusal reason, or None when ``path`` may be claimed —
    the same shape checks `validate_claim_path` makes, judged against a
    commit's tree instead of the working tree.
    """
    if Path(path).is_absolute():
        return f"{path!r} is absolute; a claim path must be project-relative"
    if path != posixpath.normpath(path) or path.startswith("../"):
        return f"{path!r} is not canonical; a claim path must be normalised and inside the tree"
    entry = tree.get(path)
    if entry is None:
        return f"{path!r} is not present in the judged commit"
    mode, _blob = entry
    if mode == "120000":
        return f"{path!r} is a symlink; a claim must name the bytes it freezes"
    return None


def describe_violations(violations: list[ClaimViolation]) -> str:
    """One line an operator can act on.

    Includes each violation's detail — without it a rename reads as
    "renamed tests/x.py" and does not say *where to*, which throws away the
    reason the kinds are distinguished at all.
    """
    return "; ".join(
        f"{v.kind.value} {v.path} (claimed by {v.task_id}, checkpoint {v.checkpoint_id}"
        + (f"; {v.detail}" if v.detail else "")
        + ")"
        for v in violations
    )


__all__ = [
    "ESCAPE_REVIEW",
    "ESCAPE_TASK",
    "FROZEN_HEADER",
    "Claim",
    "ClaimCheckError",
    "ClaimRefused",
    "ClaimStatus",
    "ClaimViolation",
    "ViolationKind",
    "active_claim_paths",
    "append_frozen_files",
    "check_claims",
    "frozen_files_block",
    "claim_blob_sha",
    "claim_paths_for",
    "selector_of",
    "describe_violations",
    "ensure_claimable",
    "record_claims",
    "record_verify_group_claims",
    "release_claims",
    "validate_claim_path",
]
