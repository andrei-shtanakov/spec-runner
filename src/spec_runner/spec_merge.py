"""Deterministic merge of a delta spec into the source-of-truth requirements (M3).

Identity is the REQ/NFR id, so matching is exact (no whitespace-tolerant header
heuristics as in OpenSpec): ADDED requires a new id, MODIFIED replaces the
whole existing block, REMOVED deletes it (Reason + Migration are mandatory),
RENAMED rewrites only the heading name. All conflicts are collected first and
reported together — ``apply_merge`` never partially applies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .requirements import Delta, parse_requirements

_BOOTSTRAP_HEADER = "# Requirements\n"


class MergeConflictError(ValueError):
    """The delta cannot be applied to the target; message lists every conflict."""


@dataclass(frozen=True)
class MergePlan:
    """Dry-run result: the operations a merge would perform, or its conflicts."""

    operations: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the delta applies cleanly."""
        return not self.conflicts


def plan_merge(target_text: str, delta: Delta) -> MergePlan:
    """Compute the merge plan of ``delta`` against ``target_text`` (no writes).

    Returns a :class:`MergePlan` whose ``conflicts`` name every offending
    requirement id — an empty ``conflicts`` means :func:`apply_merge` will
    succeed on the same inputs.
    """
    target = {r.id: r for r in parse_requirements(target_text)}
    ops: list[str] = []
    conflicts: list[str] = []

    seen: set[str] = set()
    for rid in (
        [r.id for r in delta.added]
        + [r.id for r in delta.modified]
        + [r.id for r in delta.removed]
        + [r.req_id for r in delta.renamed]
    ):
        if rid in seen:
            conflicts.append(f"{rid}: multiple operations on the same id in one delta")
        seen.add(rid)

    for req in delta.added:
        if req.id in target:
            conflicts.append(f"{req.id}: ADDED but already exists in target")
        else:
            ops.append(f"ADD {req.id}: {req.name}")

    for req in delta.modified:
        if req.id not in target:
            conflicts.append(f"{req.id}: MODIFIED but not found in target")
        else:
            ops.append(f"MODIFY {req.id}: {req.name}")

    for rem in delta.removed:
        if rem.id not in target:
            conflicts.append(f"{rem.id}: REMOVED but not found in target")
        else:
            if not rem.reason or not rem.migration:
                conflicts.append(f"{rem.id}: REMOVED requires both **Reason** and **Migration**")
            else:
                ops.append(f"REMOVE {rem.id}: {rem.name} (reason: {rem.reason})")

    for ren in delta.renamed:
        existing = target.get(ren.req_id)
        if existing is None:
            conflicts.append(f"{ren.req_id}: RENAMED but not found in target")
        elif existing.name != ren.old_name:
            conflicts.append(
                f"{ren.req_id}: RENAMED FROM name {ren.old_name!r} does not match "
                f"target name {existing.name!r}"
            )
        else:
            ops.append(f"RENAME {ren.req_id}: {ren.old_name!r} → {ren.new_name!r}")

    return MergePlan(operations=tuple(ops), conflicts=tuple(conflicts))


def apply_merge(target_text: str, delta: Delta) -> str:
    """Apply ``delta`` to ``target_text`` and return the merged document.

    All-or-nothing: raises :class:`MergeConflictError` listing every conflict
    before touching anything. An empty target is bootstrapped from the delta's
    ADDED blocks (a project's first archived delta creates the file).
    """
    plan = plan_merge(target_text, delta)
    if not plan.ok:
        raise MergeConflictError(
            "delta does not apply cleanly:\n" + "\n".join(f"  - {c}" for c in plan.conflicts)
        )

    target = {r.id: r for r in parse_requirements(target_text)}
    text = target_text if target_text.strip() else _BOOTSTRAP_HEADER

    for req in delta.modified:
        old = target[req.id]
        # Preserve the original block's trailing spacing so document structure
        # around the block survives the swap.
        old_body = old.raw.rstrip("\n")
        trailing = old.raw[len(old_body) :]
        text = text.replace(old.raw, req.raw.rstrip("\n") + trailing, 1)

    for ren in delta.renamed:
        old = target[ren.req_id]
        old_heading = old.raw.splitlines(keepends=True)[0]
        new_heading = f"{'#' * old.level} {ren.req_id}: {ren.new_name}\n"
        text = text.replace(old.raw, old.raw.replace(old_heading, new_heading, 1), 1)

    for rem in delta.removed:
        text = text.replace(target[rem.id].raw, "", 1)

    if delta.added:
        if not text.endswith("\n"):
            text += "\n"
        for req in delta.added:
            text += "\n" + req.raw.rstrip("\n") + "\n"

    # Collapse the 3+ blank-line runs a removal can leave behind.
    return re.sub(r"\n{4,}", "\n\n\n", text)
