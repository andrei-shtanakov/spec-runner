"""Post-merge sync command (#73): close the run → PR → merge → next-run loop.

After the human merges the integration PR, the operator used to hand-run
`git pull --ff-only`, delete merged run/task branches locally and on the
remote, and sanity-check executor state. `spec-runner sync` does exactly
that, reporting each step as a verdict (sync-state-as-verdict, the pattern
proven elsewhere in the ecosystem) and exiting non-zero when the tree still
cannot host the next run.
"""

import argparse
import subprocess
from dataclasses import dataclass

from .config import ExecutorConfig, ExecutorLock
from .git_ops import get_main_branch, pick_remote, runtime_state_paths
from .logging import get_logger
from .state import ExecutorState

logger = get_logger("sync")

# Only branches spec-runner itself creates are ever deleted
# (for-each-ref glob patterns, relative to a refs/... root).
_MANAGED_GLOBS = ("task/*", "spec-runner/run-*")

PR_URL_META_KEY = "last_run_pr_url"


@dataclass
class SyncStep:
    """One sync step outcome."""

    name: str
    ok: bool
    detail: str = ""


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )


def _managed_branches(config: ExecutorConfig, ref_prefix: str) -> list[str]:
    """Branch names under `ref_prefix` matching the managed patterns."""
    result = _git(
        config,
        "for-each-ref",
        "--format=%(refname:short)",
        *[f"{ref_prefix}/{glob}" for glob in _MANAGED_GLOBS],
    )
    if result.returncode != 0:
        return []
    return [b for b in result.stdout.split() if b]


def _is_merged(config: ExecutorConfig, ref: str, base: str) -> bool:
    return _git(config, "merge-base", "--is-ancestor", ref, base).returncode == 0


def run_sync(config: ExecutorConfig, *, dry_run: bool = False) -> list[SyncStep]:
    """Execute the post-merge sync; returns the step verdicts.

    Fails fast on the preconditions (active run, no repo, dirty tree,
    non-ff pull) — later steps are only meaningful on a clean, current base.
    """
    steps: list[SyncStep] = []

    def fail(name: str, detail: str) -> list[SyncStep]:
        steps.append(SyncStep(name, False, detail))
        return steps

    # 0. No active run — sync mutates the tree the executor may be using.
    lock = ExecutorLock(config.state_file.with_suffix(".lock"))
    if not lock.acquire():
        held = getattr(lock, "_held_by", {})
        return fail("no active run", f"executor lock held by PID {held.get('pid', '?')}")
    try:
        # 1. Git repo present.
        if _git(config, "rev-parse", "--git-dir").returncode != 0:
            return fail("git repo", "not a git repository")
        steps.append(SyncStep("git repo", True))

        base = get_main_branch(config)

        # 2. Clean worktree. Executor runtime state never counts as dirt —
        # it is usually gitignored (#62), but on repos without that coverage
        # sync's own lock file would otherwise fail its own check.
        # -uall lists untracked files individually (no collapsed `?? dir/`
        # entries), so the runtime-path prefix filter below can apply.
        status = _git(config, "status", "--porcelain", "-uall")
        runtime_rels = []
        for p in runtime_state_paths(config):
            try:
                runtime_rels.append(str(p.relative_to(config.project_root)))
            except ValueError:
                continue
        dirt = [
            line
            for line in status.stdout.splitlines()
            if line.strip() and not any(line[3:].startswith(rel) for rel in runtime_rels)
        ]
        if dirt:
            return fail(
                "clean worktree",
                "uncommitted changes present — commit or stash before syncing:\n" + "\n".join(dirt),
            )
        steps.append(SyncStep("clean worktree", True))

        # 3. On the base branch.
        current = _git(config, "branch", "--show-current").stdout.strip()
        if current != base:
            if dry_run:
                steps.append(SyncStep("switch to base", True, f"would checkout {base}"))
            else:
                checkout = _git(config, "checkout", base)
                if checkout.returncode != 0:
                    return fail("switch to base", checkout.stderr.strip()[:200])
                steps.append(
                    SyncStep("switch to base", True, f"{current or '(detached)'} → {base}")
                )
        else:
            steps.append(SyncStep("switch to base", True, f"already on {base}"))

        # 4. Fast-forward pull + prune (skipped without a remote).
        remote = pick_remote(config)
        if remote is None:
            steps.append(SyncStep("pull --ff-only", True, "no remote (skipped)"))
        elif dry_run:
            steps.append(SyncStep("pull --ff-only", True, f"would pull {remote}/{base}"))
        else:
            pull = _git(config, "pull", "--ff-only", remote, base)
            if pull.returncode != 0:
                return fail("pull --ff-only", pull.stderr.strip()[:200])
            steps.append(SyncStep("pull --ff-only", True, f"{remote}/{base}"))
            _git(config, "fetch", "--prune", remote)

        # 5. Delete merged managed branches, local then remote. Merged-only —
        # an unmerged branch is never touched, and deletion uses -d semantics
        # via an explicit ancestor check (never force).
        local = _managed_branches(config, "refs/heads")
        merged_local = [b for b in local if b != base and _is_merged(config, b, base)]
        kept_local = [b for b in local if b not in merged_local]
        if dry_run:
            detail = f"would delete: {', '.join(merged_local) or '(none)'}"
        else:
            for b in merged_local:
                _git(config, "branch", "-d", b)
            detail = f"deleted: {', '.join(merged_local) or '(none)'}"
        if kept_local:
            detail += f"; kept (unmerged): {', '.join(kept_local)}"
        steps.append(SyncStep("local managed branches", True, detail))

        if remote is not None:
            remote_branches = [
                b.removeprefix(f"{remote}/")
                for b in _managed_branches(config, f"refs/remotes/{remote}")
            ]
            merged_remote = [
                b for b in remote_branches if _is_merged(config, f"{remote}/{b}", base)
            ]
            kept_remote = [b for b in remote_branches if b not in merged_remote]
            if dry_run:
                detail = f"would delete: {', '.join(merged_remote) or '(none)'}"
            else:
                for b in merged_remote:
                    push = _git(config, "push", remote, "--delete", b)
                    if push.returncode != 0:
                        logger.warning(
                            "Could not delete remote branch",
                            branch=b,
                            stderr=push.stderr.strip()[:120],
                        )
                detail = f"deleted: {', '.join(merged_remote) or '(none)'}"
            if kept_remote:
                detail += f"; kept (unmerged): {', '.join(kept_remote)}"
            steps.append(SyncStep("remote managed branches", True, detail))

        # 6. Executor state sanity + close the PR loop.
        with ExecutorState(config) as state:
            running = [ts.task_id for ts in state.tasks.values() if ts.status == "running"]
            detail = "no tasks stuck in running" if not running else f"running: {running}"
            steps.append(SyncStep("state sanity", not running, detail))
            pr_url = state.get_meta(PR_URL_META_KEY)
            if pr_url:
                if not dry_run:
                    state.set_meta(PR_URL_META_KEY, "")
                steps.append(SyncStep("pr loop", True, f"cleared awaiting-merge marker ({pr_url})"))
    finally:
        lock.release()

    return steps


def cmd_sync(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """`spec-runner sync` — post-merge closer for the integration-PR loop."""
    dry_run = getattr(args, "dry_run", False)
    steps = run_sync(config, dry_run=dry_run)
    for step in steps:
        icon = "✓" if step.ok else "✗"
        line = f" {icon} {step.name}"
        if step.detail:
            line += f" — {step.detail}"
        print(line)
    ok = all(s.ok for s in steps)
    if dry_run:
        print("(dry-run: nothing was changed)")
    print("sync OK — ready for the next run" if ok else "sync FAILED")
    raise SystemExit(0 if ok else 1)
