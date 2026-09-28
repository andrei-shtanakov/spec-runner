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
- ``strict`` — fail the attempt before the gates run; the error message
  feeds the retry prompt so the next attempt knows not to touch the
  harness. Operators opting in can exempt paths via ``harness_allow``.
- ``off`` — no snapshotting at all.

The spec-runner config itself (`CONTROL_PLANE`) is always on the surface and
never exempt: it is the policy the attempt is judged by.
"""

import hashlib
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


def touch_conflicts(config: ExecutorConfig, touches: list[PurePosixPath]) -> list[str]:
    """The harness paths a declared write scope would change, not exempt.

    Overlap in either direction: a touched directory holding a harness file,
    or a touched file inside a harness directory. The exemption is matched,
    like the guard's, against the more specific of the two paths, and never
    reaches the config (`CONTROL_PLANE`).
    """
    control = _control_plane_keys(config)
    surface = [PurePosixPath(p) for p in (*HARNESS_CANDIDATES, *control, *config.harness_files)]
    conflicts: list[str] = []
    for touched in touches:
        for harness in surface:
            if _covers(harness, touched):
                specific = touched
            elif _covers(touched, harness):
                specific = harness
            else:
                continue
            exempt = str(harness) not in control and any(
                Path(specific).match(pattern) for pattern in config.harness_allow
            )
            if not exempt and str(specific) not in conflicts:
                conflicts.append(str(specific))
    return conflicts


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
