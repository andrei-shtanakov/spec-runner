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
import subprocess
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
    from .tdd import RedCheckpoint

logger = get_logger("claims")


class ClaimCheckError(RuntimeError):
    """The claims could not be checked. Never confused with "no violations"."""


class ClaimStatus(str, Enum):
    """A claim's life. Nothing is ever deleted — see slice 3."""

    ACTIVE = "active"
    #: Replaced by a later lineage (`tdd repair`).
    SUPERSEDED = "superseded"
    #: The red it belonged to was given up on (`tdd abandon`).
    ABANDONED = "abandoned"


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


def claim_paths_for(selector: str) -> list[str]:
    """The files a selector claims.

    A pytest node id names exactly one file. **Documented limitation** (§1.3):
    a test depending on a fixture in `conftest.py` does not claim that
    conftest, so editing the fixture can turn the red green and is not blocked.
    Widening this by import graph or coverage is a separate decision; guessing
    at it here would be worse than the honest gap.
    """
    if "::" not in selector:
        # Refused upstream by `verify_red`. Claiming nothing rather than
        # guessing keeps the two from disagreeing about what a selector is.
        return []
    return [selector.split("::", 1)[0]]


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


def record_claims(
    config: ExecutorConfig,
    state: ExecutorState,
    checkpoint: RedCheckpoint,
) -> list[Claim]:
    """Claim the files ``checkpoint``'s selector depends on.

    Re-claiming the same path at the same bytes is idempotent: a re-run must
    not be a violation and must not stack duplicate rows.
    """
    root = Path(config.project_root)
    existing = {(c.path, c.blob_sha) for c in state.active_claims(checkpoint.namespace)}
    recorded: list[Claim] = []

    for path in claim_paths_for(checkpoint.selector):
        refusal = validate_claim_path(root, path)
        if refusal:
            logger.warning("Refusing to claim a path", path=path, reason=refusal)
            continue
        blob = claim_blob_sha(root, path)
        if (path, blob) in existing:
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
        if present is not None:
            violations.append(
                ClaimViolation(
                    ViolationKind.MODIFIED,
                    claim.path,
                    claim.task_id,
                    claim.checkpoint_id,
                    f"claimed {claim.blob_sha[:12]}, found {present[:12]}",
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
                    f"the claimed bytes are now at {', '.join(sorted(elsewhere))}",
                )
            )
        else:
            violations.append(
                ClaimViolation(
                    ViolationKind.DELETED,
                    claim.path,
                    claim.task_id,
                    claim.checkpoint_id,
                    "the path is gone and its bytes are nowhere in the tree",
                )
            )
    return violations


def _tree_blobs(root: Path, sha: str) -> dict[str, str] | None:
    """``{path: blob sha}`` for every file in ``sha``, or None if unreadable.

    Read straight from the object database — `git ls-tree` of a commit cannot
    be influenced by the working tree, which is the point.
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--full-tree", "-z", sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    blobs: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    return blobs


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
    "Claim",
    "ClaimCheckError",
    "ClaimStatus",
    "ClaimViolation",
    "ViolationKind",
    "check_claims",
    "claim_blob_sha",
    "claim_paths_for",
    "describe_violations",
    "record_claims",
    "validate_claim_path",
]
