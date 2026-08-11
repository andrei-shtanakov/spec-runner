"""`spec-runner tdd status` / `tdd checkpoints` — the TDD state, readable (F-5).

The battle test could not run the remedies without reading the SQLite database
and re-deriving a SHA-256 by hand: `tdd abandon|repair --checkpoint <id>`
**requires** an id that no command printed. Worse for trust, after abandoning a
red the ordinary `status` still reported `✅ success`, because it reads attempt
history and knows nothing about the lifecycle.

Evidence nobody can reach is not evidence. These two commands are the reach.

Contract: ``docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md``
Report: ``docs/superpowers/specs/2026-08-11-tdd-battle-report.md``, F-5
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .claims import ClaimStatus
from .state import ExecutorState
from .tdd import RedOutcome, resolve_namespace

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig


def collect(config: ExecutorConfig, task_id: str | None = None) -> dict:
    """Everything the TDD lifecycle knows, as plain data.

    One reader for both commands and for `--json`, so the text a person sees
    and the payload a script parses cannot drift apart.
    """
    namespace = resolve_namespace(config)
    with ExecutorState(config) as state:
        active = state.active_checkpoints(namespace, task_id)
        retired = state.retired_checkpoints(namespace, task_id)
        claims = state.claims_for(namespace, task_id)
        phases = {
            tid: state.tdd_phase_history(tid, namespace)
            for tid in {cp.task_id for cp in active}
            | {row[0] for row in retired}
            | ({task_id} if task_id else set())
        }
        remedies = (
            [
                {
                    "operation": r.operation.value,
                    "checkpoint_id": r.checkpoint_id,
                    "new_checkpoint_id": r.new_checkpoint_id,
                    "actor": r.actor,
                    "reason": r.reason,
                    "timestamp": r.timestamp,
                }
                for r in state.remedies(task_id, namespace)
            ]
            if task_id
            else _all_remedies(state, namespace, active, retired)
        )

    return {
        "namespace": namespace,
        "execution_mode": config.execution_mode,
        "active_checkpoints": [
            {
                "checkpoint_id": cp.checkpoint_id,
                "task_id": cp.task_id,
                "commit_sha": cp.commit_sha,
                "baseline_sha": cp.baseline_sha,
                "selector": cp.selector,
                "outcome": cp.outcome.value,
                "environment_id": cp.environment_id,
                "timestamp": cp.timestamp,
            }
            for cp in active
        ],
        "retired_checkpoints": [
            {
                "task_id": r[0],
                "status": r[1],
                "outcome": r[2],
                "selector": r[3],
                "timestamp": r[4],
            }
            for r in retired
        ],
        "claims": [
            {
                "task_id": c[0],
                "path": c[1],
                "blob_sha": c[2],
                "status": c[3],
                "checkpoint_id": c[4],
            }
            for c in claims
        ],
        "remedies": remedies,
        "phases": {
            tid: [{"phase": h["phase"], "detail": h["detail"]} for h in history]
            for tid, history in phases.items()
        },
    }


def _all_remedies(state: ExecutorState, namespace: str, active, retired) -> list[dict]:
    seen = {cp.task_id for cp in active} | {row[0] for row in retired}
    out: list[dict] = []
    for tid in sorted(seen):
        for r in state.remedies(tid, namespace):
            out.append(
                {
                    "task_id": tid,
                    "operation": r.operation.value,
                    "checkpoint_id": r.checkpoint_id,
                    "new_checkpoint_id": r.new_checkpoint_id,
                    "actor": r.actor,
                    "reason": r.reason,
                    "timestamp": r.timestamp,
                }
            )
    return out


def lifecycle_of(data: dict, task_id: str) -> str:
    """One line for where a task stands — the thing plain `status` gets wrong.

    After an abandon the ordinary status still says "success", because the last
    *attempt* succeeded. True of the attempt and misleading about the task: it
    has no confirmed red and cannot proceed.
    """
    history = data.get("phases", {}).get(task_id) or []
    if history:
        last = history[-1]["phase"]
        if last == "done":
            return "done"
        if not last.startswith("refused:"):
            return f"in {last.replace('_', ' ')}"
    active = [c for c in data["active_checkpoints"] if c["task_id"] == task_id]
    if len(active) > 1:
        return f"{len(active)} active checkpoints — ambiguous, a remedy must name one"
    if active:
        cp = active[0]
        if cp["outcome"] == RedOutcome.EXPECTED_FAIL.value:
            return f"red confirmed ({cp['checkpoint_id']})"
        return f"red not confirmed: {cp['outcome']} ({cp['checkpoint_id']})"
    retired = [r for r in data["retired_checkpoints"] if r["task_id"] == task_id]
    if retired:
        return f"no active red — last checkpoint {retired[-1]['status']}; needs RED authoring"
    return "no red checkpoint yet"


def render(data: dict, task_id: str | None) -> str:
    """The human view. Deliberately shows retired records too: the point of
    never deleting them is that someone can reconstruct what was believed."""
    lines = [f"🧪 TDD state — workspace {data['namespace']} (mode: {data['execution_mode']})"]
    tasks = sorted(
        {c["task_id"] for c in data["active_checkpoints"]}
        | {r["task_id"] for r in data["retired_checkpoints"]}
        | {c["task_id"] for c in data["claims"]}
    )
    if task_id:
        tasks = [task_id]
    if not tasks:
        lines.append("   (nothing recorded)")
        return "\n".join(lines)

    for tid in tasks:
        lines.append("")
        lines.append(f"{tid}: {lifecycle_of(data, tid)}")
        for cp in [c for c in data["active_checkpoints"] if c["task_id"] == tid]:
            lines.append(
                f"   checkpoint {cp['checkpoint_id']}  {cp['commit_sha'][:12]}  "
                f"{cp['outcome']}  {cp['selector']}"
            )
            lines.append(f"      env {cp['environment_id']}  baseline {cp['baseline_sha'][:12]}")
        active_claims = [
            c for c in data["claims"] if c["task_id"] == tid and c["status"] == ClaimStatus.ACTIVE
        ]
        for c in active_claims:
            lines.append(f"   🔒 {c['path']}  ({c['blob_sha'][:12]})")
        retired_claims = len([c for c in data["claims"] if c["task_id"] == tid]) - len(
            active_claims
        )
        if retired_claims:
            lines.append(f"   ({retired_claims} retired claim(s))")
        for r in [x for x in data["remedies"] if x.get("task_id", tid) == tid]:
            arrow = f" → {r['new_checkpoint_id']}" if r["new_checkpoint_id"] else ""
            lines.append(
                f"   ⚖️  {r['operation']} {r['checkpoint_id']}{arrow} by {r['actor']}: {r['reason']}"
            )
    return "\n".join(lines)


def cmd_tdd_status(args, config: ExecutorConfig) -> int:
    data = collect(config, getattr(args, "task_id", None))
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    print(render(data, getattr(args, "task_id", None)))
    return 0


def cmd_tdd_checkpoints(args, config: ExecutorConfig) -> int:
    """Just the checkpoints, in the form `--checkpoint <id>` wants."""
    data = collect(config, getattr(args, "task_id", None))
    if getattr(args, "json", False):
        print(json.dumps(data["active_checkpoints"], indent=2))
        return 0
    if not data["active_checkpoints"]:
        print("No active checkpoints.")
        return 0
    print(f"{'CHECKPOINT':<14}{'TASK':<12}{'COMMIT':<14}{'OUTCOME':<16}SELECTOR")
    for cp in data["active_checkpoints"]:
        print(
            f"{cp['checkpoint_id']:<14}{cp['task_id']:<12}{cp['commit_sha'][:12]:<14}"
            f"{cp['outcome']:<16}{cp['selector']}"
        )
    return 0


__all__ = ["cmd_tdd_checkpoints", "cmd_tdd_status", "collect", "lifecycle_of", "render"]
