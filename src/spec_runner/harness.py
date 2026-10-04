"""Harness-mutation tripwire (#64).

The verification harness (test/lint configuration, dependency manifests, CI
workflows) lives inside the agent's write scope: an agent that sees a failing
gate can create a bridge — or neuter the oracle outright — and the run
reports success. Observed in the field: a pytest bridge (`pyproject.toml` +
`tests/test_mix_suite.py`) invented on an Elixir repo to satisfy the default
Python test command.

The guard snapshots the harness surface before the agent runs and compares
after: created/modified/deleted files are violations. Modes
(``harness_guard`` config key):

- ``warn`` (default) — log the mutations, keep going. Legitimate flows
  (``uv add`` touching pyproject/uv.lock) produce a provenance line, not a
  failure.
- ``strict`` — fail the attempt; the error message feeds the retry prompt
  so the next attempt knows not to touch the harness. Operators opting in
  can exempt paths via ``harness_allow``.
- ``off`` — no snapshotting at all.

Where it looks (`guard_error`): after the RED/verify-first passes and
before GREEN; after GREEN, before the gates; after the reviewer, before
the re-run gates — the review call itself withholds the commit of fixes
that touch the harness; after the `post_review` plugins, before the DONE
flip; and per `review-pr` fix, before its gates. Every site but GREEN
undoes a refused step (`refuse_and_restore`): a refused edit left in the
tree would be committed next, or — under ``create_git_branch: false`` —
read by the next task's baseline as the oracle. GREEN's edit is left for
the next attempt to revert, as the refusal asks.

The spec-runner config itself (`CONTROL_PLANE`) is always on the surface and
never exempt: it is the policy the attempt is judged by.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import CONFIG_FILE, LEGACY_CONFIG_FILE, ExecutorConfig
from .logging import get_logger

logger = get_logger("harness")

# Oracle-defining files across common stacks, relative to the project root.
# Directories are scanned recursively. Extended via `harness_files` config.
HARNESS_CANDIDATES = (
    "pyproject.toml",
    "uv.lock",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "conftest.py",
    "package.json",
    "mix.exs",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    ".github/workflows",
)

# The candidates that are directories — known without the tree, where a
# declared scope may name one before it exists.
_DIR_CANDIDATES = frozenset({".github/workflows"})

# The policy an attempt is judged by — `review_policy`, the budget,
# `execution_mode`, this guard's own mode (harness-guard-companions #1). Watched
# in both config locations and never exempt: `harness_allow` is global, so a
# glob written for one task would open the policy to every task after it.
CONTROL_PLANE = (CONFIG_FILE.as_posix(), LEGACY_CONFIG_FILE.as_posix())


def _surface_key(config: ExecutorConfig, path: Path) -> str:
    """Project-root-relative when inside the root, absolute otherwise."""
    try:
        return str(path.relative_to(config.project_root))
    except ValueError:
        return path.as_posix()


def _control_plane_keys(config: ExecutorConfig) -> set[str]:
    """Both config locations under the root, plus the file actually loaded.

    The loaded file can sit outside `project_root` — `paths.root` in the YAML
    moves the root away from the directory the config was read from — and it
    is that file, not its namesake under the root, that the next run loads.
    """
    keys = set(CONTROL_PLANE)
    if config.config_path is not None:
        keys.add(_surface_key(config, config.config_path))
    return keys


def is_control_plane(config: ExecutorConfig, violation: str) -> bool:
    """Whether a ``"<kind> <path>"`` violation line names the config."""
    return violation.split(" ", 1)[1] in _control_plane_keys(config)


def _covers(outer: PurePosixPath, inner: PurePosixPath) -> bool:
    return outer == inner or outer in inner.parents


@dataclass(frozen=True)
class TouchConflicts:
    """What a declared write scope does to the harness surface.

    ``definite`` — the guard would refuse it: the task names a harness file
    (or a path inside a harness directory) that no `harness_allow` pattern
    exempts. ``possible`` — it cannot be told from the declaration: a declared
    directory *contains* a harness path the task may never write, or a
    harness directory is declared while exemptions depend on file names.
    """

    definite: list[str]
    possible: list[str]


def touch_conflicts(config: ExecutorConfig, touches: list[PurePosixPath]) -> TouchConflicts:
    """Compare a declared scope with the harness surface the guard watches.

    The exemption is matched, like the guard's, against concrete file paths;
    it never reaches the config (`CONTROL_PLANE`).
    """
    control = _control_plane_keys(config)
    surface = [PurePosixPath(p) for p in (*HARNESS_CANDIDATES, *control, *config.harness_files)]
    definite: list[str] = []
    possible: list[str] = []

    def exempt(path: PurePosixPath) -> bool:
        return str(path) not in control and any(
            Path(path).match(pattern) for pattern in config.harness_allow
        )

    for touched in touches:
        for harness in surface:
            if _covers(harness, touched):
                names_a_dir = (config.project_root / touched).is_dir() or (
                    touched == harness and str(harness) in _DIR_CANDIDATES
                )
                if names_a_dir and str(harness) not in control:
                    # Which files land under it decides the exemptions.
                    bucket = possible if config.harness_allow else definite
                    bucket.append(str(touched))
                elif not exempt(touched):
                    definite.append(str(touched))
            elif _covers(touched, harness) and not exempt(harness):
                possible.append(str(harness))
    return TouchConflicts(
        definite=list(dict.fromkeys(definite)),
        possible=[p for p in dict.fromkeys(possible) if p not in definite],
    )


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if root.is_dir():
        return sorted(p for p in root.rglob("*") if p.is_file())
    return []


def snapshot_harness(config: ExecutorConfig) -> dict[str, str] | None:
    """Hash every harness file. Returns None when the guard is off.

    Keys are project-root-relative paths (absolute for a loaded config outside
    the root); values are content hashes.
    A file absent from the snapshot did not exist at snapshot time.
    """
    if config.harness_guard == "off":
        return None
    snapshot: dict[str, str] = {}
    candidates = [*HARNESS_CANDIDATES, *_control_plane_keys(config), *config.harness_files]
    for rel in candidates:
        for f in _iter_files(config.project_root / rel):
            try:
                digest = _hash_file(f)
            except OSError:
                # An unreadable file must still exist in the snapshot —
                # otherwise creating a chmod-000 file would bypass the
                # guard entirely (Copilot finding on #90).
                digest = "unreadable"
            snapshot[_surface_key(config, f)] = digest
    return snapshot


def snapshot_contents(config: ExecutorConfig) -> dict[str, bytes | None] | None:
    """The harness surface's bytes, for undoing a refused edit (`restore_surface`).

    Keyed like `snapshot_harness`; an unreadable file maps to None. Returns
    None when the guard is off.
    """
    if config.harness_guard == "off":
        return None
    contents: dict[str, bytes | None] = {}
    candidates = [*HARNESS_CANDIDATES, *_control_plane_keys(config), *config.harness_files]
    for rel in candidates:
        for f in _iter_files(config.project_root / rel):
            try:
                contents[_surface_key(config, f)] = f.read_bytes()
            except OSError:
                contents[_surface_key(config, f)] = None
    return contents


def content_hashes(contents: dict[str, bytes | None] | None) -> dict[str, str] | None:
    """`snapshot_contents` in `snapshot_harness`'s shape, for `harness_violations`."""
    if contents is None:
        return None
    return {
        key: "unreadable" if data is None else hashlib.sha256(data).hexdigest()
        for key, data in contents.items()
    }


def _restore_target(config: ExecutorConfig, key: str) -> Path | None:
    """Where `restore_surface` may write `key`, or None if it may not.

    The step being undone controlled the tree: a directory on the way to a
    harness file may now be a symlink, and following it would unlink or
    overwrite a file outside the project. The nearest existing ancestor must
    resolve to exactly where it sits under the (resolved) root. A key outside
    the root — a loaded config elsewhere — is never written: nothing here
    can tell its directories from ones a step replaced, and the refusal
    stands either way.
    """
    if Path(key).is_absolute():
        return None
    root = config.project_root.resolve()
    path = root / key
    ancestor = path.parent
    while not ancestor.exists() and ancestor != root:
        ancestor = ancestor.parent
    return path if ancestor.resolve() == ancestor else None


def restore_surface(config: ExecutorConfig, before: dict[str, bytes | None] | None) -> list[str]:
    """Undo every violation relative to `before`; return the paths it could not.

    What a refused step wrote must not outlive the refusal: left in the tree,
    it is committed by whatever commits next, and under
    ``create_git_branch: false`` the next task's baseline is taken from it —
    the refused edit becomes the oracle. Exempt (`harness_allow`) paths are
    not violations and are left alone.
    """
    unrestored: list[str] = []
    for violation in harness_violations(config, content_hashes(before)):
        kind, key = violation.split(" ", 1)
        path = _restore_target(config, key)
        if path is None:
            logger.error("Harness path cannot be restored safely", path=key)
            unrestored.append(key)
            continue
        try:
            if kind == "created":
                path.unlink(missing_ok=True)
                continue
            data = (before or {}).get(key)
            if data is None:
                unrestored.append(key)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                # Written through, it would land wherever the link points.
                path.unlink()
            path.write_bytes(data)
        except OSError as exc:
            logger.error("Could not restore harness file", path=key, error=str(exc))
            unrestored.append(key)
    return unrestored


class HarnessBaseline:
    """A task's harness snapshot, captured once and reused by every attempt.

    The guard used to snapshot inside each attempt, so a forbidden edit that
    outlived a failed attempt silently became the next attempt's baseline: the
    barrier held once and was disarmed by a plain retry (#137, seen in
    production with the default ``max_retries: 3``). Binding the baseline to
    the task's lifecycle instead means a divergence blocks every attempt,
    whatever its number.

    Capture stays lazy because it must happen *after* ``pre_start_hook`` —
    ``uv sync`` legitimately rewrites `uv.lock`/`pyproject.toml`, and counting
    that as an agent mutation would make dependency sync a violation.
    """

    __slots__ = ("_snapshot", "_captured")

    def __init__(self) -> None:
        self._snapshot: dict[str, str] | None = None
        self._captured = False

    def capture(self, config: ExecutorConfig) -> dict[str, str] | None:
        """Snapshot on first call; every later call replays the same one."""
        if not self._captured:
            self._snapshot = snapshot_harness(config)
            self._captured = True
        return self._snapshot


def harness_violations(config: ExecutorConfig, before: dict[str, str] | None) -> list[str]:
    """Compare the harness surface against `before`; return violation lines.

    Each entry is ``"<created|modified|deleted> <path>"``. Paths matching a
    ``harness_allow`` glob (project-root-relative) are exempt, except the
    config (`CONTROL_PLANE`).
    """
    if before is None:
        return []
    after = snapshot_harness(config)
    assert after is not None  # guard was on when `before` was taken

    violations: list[str] = []
    for path, digest in sorted(after.items()):
        if path not in before:
            violations.append(f"created {path}")
        elif before[path] != digest:
            violations.append(f"modified {path}")
    for path in sorted(before):
        if path not in after:
            violations.append(f"deleted {path}")

    if config.harness_allow:
        violations = [
            v
            for v in violations
            if is_control_plane(config, v)
            or not any(Path(v.split(" ", 1)[1]).match(pattern) for pattern in config.harness_allow)
        ]
    return violations


def guard_error(
    config: ExecutorConfig,
    task_id: str,
    before: dict[str, str] | None,
    log_progress: Callable[[str, str], None],
    actor: str = "the agent",
) -> str | None:
    """Compare the harness with `before`; the refusal, if any.

    Returns the attempt's error under `strict` when the surface changed;
    under `warn` it logs the change and returns None, as it does when nothing
    changed or the guard is off. One answer for every site that checks.

    `before` is what the check is about. GREEN compares with the task's
    baseline. Every other site compares with a snapshot taken right before
    the step it judges — the RED/verify-first passes of this attempt, the
    reviewer, the `post_review` plugins, one `review-pr` fix. Comparing those
    with the task's baseline would blame them for the harness's own writes
    (a repo-wide `lint_fix_command` between the steps) and for an edit a
    previous attempt left behind — which the next GREEN agent is told to
    revert and must be allowed to.
    """
    violations = harness_violations(config, before)
    if not violations:
        return None
    summary = ", ".join(violations)
    if config.harness_guard != "strict":
        log_progress(f"⚠️ Harness files changed by {actor}: {summary}", task_id)
        logger.warning("Harness files mutated by agent", violations=violations)
        return None
    # The error becomes the next attempt's prompt, so it must not name the
    # exemption: that taught the author agent how to lift the barrier that
    # just stopped it. The operator's way out goes on the progress line, which
    # no prompt carries. That is the whole guarantee: the knob is no secret
    # (README documents it, and the progress file sits in the tree); keeping
    # the agent from *using* it is companion #1 (config under guard).
    policy = [v for v in violations if is_control_plane(config, v)]
    hints = []
    if policy:
        hints.append("the spec-runner config cannot be exempted; revert it")
    if len(policy) < len(violations):
        hints.append("exempt an intended change via harness_allow in the config")
    log_progress(f"⛔ Harness guard: {summary} (operator: {'; '.join(hints)})", task_id)
    logger.error("Harness files mutated by agent", violations=violations)
    return (
        f"Harness guard: {actor} modified verification files: "
        f"{summary}. These files define how the task is verified "
        "and must not be changed by the task. Revert them."
    )


def refuse_and_restore(
    config: ExecutorConfig,
    task_id: str,
    before: dict[str, bytes | None] | None,
    log_progress: Callable[[str, str], None],
    actor: str = "the agent",
) -> str | None:
    """`guard_error` for one step, the step undone on a refusal.

    For every site that judges a step against a snapshot taken right before
    it (`snapshot_contents`): what a refused step wrote must not outlive the
    refusal (`restore_surface`). GREEN is the exception — its edit is left
    for the next attempt to revert, as the refusal asks.
    """
    error = guard_error(config, task_id, content_hashes(before), log_progress, actor=actor)
    if error is None:
        return None
    unrestored = restore_surface(config, before)
    if unrestored:
        return error + f" Could not restore: {', '.join(unrestored)}."
    return error + " The harness has restored them."
