"""`spec` subcommands: status, approve, reject, adopt, check."""

from __future__ import annotations

import argparse
import subprocess
from datetime import UTC, datetime

from .config import ExecutorConfig
from .logging import get_logger
from .spec import (
    STAGES,
    SpecMeta,
    apply_approval,
    read_spec_body,
    read_spec_meta,
    resolve_next_stage,
    stage_path,
    write_spec,
)
from .validate import validate_spec_stage, verdict_from_result

logger = get_logger("spec")


def _now() -> str:
    """Return the current UTC time as an ISO-8601 ``Z`` string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _approver() -> str:
    """Return the git ``user.name``, or ``"unknown"`` if unavailable."""
    try:
        out = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _metas(config: ExecutorConfig) -> dict[str, SpecMeta | None]:
    """Read the frontmatter meta for every stage in ``STAGES``."""
    return {stage: read_spec_meta(stage_path(config, stage)) for stage in STAGES}


def cmd_spec_status(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Print per-stage status plus the recommended next action."""
    metas = _metas(config)
    for stage in STAGES:
        meta = metas[stage]
        if meta is None:
            print(f"{stage:12} —        unmanaged")
        else:
            print(
                f"{stage:12} {meta.status:8} v{meta.version}  validation={meta.validation or '?'}"
            )
    action, stage = resolve_next_stage(metas)
    print(f"\nnext: {action} → {stage}")
    return 0


def cmd_spec_approve(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Approve ``args.stage`` after re-validating the current body (TOCTOU guard).

    The cached ``validation`` frontmatter field is never trusted: the body is
    re-validated from scratch every time, since it may have changed since the
    last ``check``/``adopt`` stamped the cache.
    """
    stage = args.stage
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        print(f"{stage}: unmanaged (no frontmatter); run `spec adopt` first")
        return 2
    result = validate_spec_stage(stage, config)
    verdict = verdict_from_result(result)
    if verdict == "fail":
        print(f"{stage}: validation FAILED — not approved:")
        for error in result.errors:
            print(f"  - {error}")
        return 1
    apply_approval(config, stage, approver=_approver(), now=_now(), fresh_validation=verdict)
    new_meta = read_spec_meta(path)
    version = new_meta.version if new_meta is not None else "?"
    print(f"{stage}: approved (v{version})")
    return 0


def cmd_spec_reject(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Reopen ``args.stage`` as ``draft``."""
    stage = args.stage
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        print(f"{stage}: unmanaged")
        return 2
    meta.status = "draft"
    write_spec(path, meta, read_spec_body(path))
    print(f"{stage}: re-opened as draft")
    return 0


def cmd_spec_adopt(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Adopt an unmanaged file at ``args.stage``, stamping frontmatter onto it.

    Validates first: a passing/warning body is adopted as ``approved``; a
    failing body is adopted as ``draft`` unless ``args.force`` is set, in
    which case it is stamped ``approved`` with a loud warning. This never
    silently stamps APPROVED over an invalid spec.
    """
    stage = args.stage
    path = stage_path(config, stage)
    if not path.exists():
        print(f"{stage}: no file to adopt at {path}")
        return 2
    body = read_spec_body(path)  # strips frontmatter if somehow already present
    result = validate_spec_stage(stage, config)
    verdict = verdict_from_result(result)
    force = getattr(args, "force", False)
    if verdict == "fail" and not force:
        status = "draft"
        print(f"{stage}: validation failed → adopted as DRAFT (fix + approve)")
    else:
        status = "approved"
        if verdict == "fail":
            logger.warning("adopt_force_invalid", stage=stage, errors=len(result.errors))
            print(f"WARNING: {stage}: adopting INVALID spec as approved (--force)")
    meta = SpecMeta(
        spec_stage=stage,
        status=status,
        version=1,
        validation=verdict,
        approved_by=_approver() if status == "approved" else None,
        approved_at=_now() if status == "approved" else None,
    )
    write_spec(path, meta, body)
    print(f"{stage}: adopted ({status})")
    return 0


def cmd_spec_check(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Refresh the cached ``validation`` field for ``args.stage``."""
    stage = args.stage
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        print(f"{stage}: unmanaged")
        return 2
    verdict = verdict_from_result(validate_spec_stage(stage, config))
    meta.validation = verdict
    write_spec(path, meta, read_spec_body(path))
    print(f"{stage}: validation={verdict}")
    return 0 if verdict != "fail" else 1
