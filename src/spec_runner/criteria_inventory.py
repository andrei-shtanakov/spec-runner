"""Deploy the probe, run its collect mode, and read a validated inventory (#603 §3.5).

The probe's manifest counts only after full probe/1 validation
(`criteria_protocol.valid_collect`); anything else — no manifest, another
process's manifest, an inconsistent one, a local timeout — is
`collection-failed`, never an empty inventory. Then, in order: collection
errors (`collection-error`), a pytest config read from outside the checkout,
a `pytest.Function` whose definition is not a file tracked at `sha`
(`definition-unresolved`), and only then whether collection changed a tracked
file (`collection-mutated-checkout`). The checkout is reset to `sha` in every
case before `collect` returns or raises.

spec-runner never imports the probe (nor pytest): `deploy_probe` reads the
probe's source as text and writes it next to nothing else.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from spec_runner import criteria_process
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import Deadline, Finished
from spec_runner.criteria_protocol import (
    MANIFEST_ENV,
    MODE_ENV,
    PARENT_ENV,
    PROBE_MODULE,
    read_manifest,
    valid_collect,
)
from spec_runner.criteria_workspace import (
    Environment,
    changed_since,
    child_env,
    reset_checkout,
    tracked_files,
)

_TAIL_LINES = 20
# pytest's wording for an initial conftest that fails to import (exit 4, before any
# session, so no manifest) — measured on pytest 9.1.1 for import and syntax errors.
_CONFTEST_FAILURE = "while loading conftest"
_USAGE_ERROR = 4


@dataclass(frozen=True)
class TestItem:
    """A collected `pytest.Function` and where it is defined (repository-relative)."""

    __test__ = False

    node_id: str
    file: str
    qualname: str
    line: int


@dataclass(frozen=True)
class Excluded:
    """Something collection left out (§3.5): `skipped`, `ignored` or `deselected`.

    `path` for skipped/ignored, `node_id` (and `definition` as `(file, qualname,
    line)` when its file is tracked at sha) for deselected; `reason` for skipped.
    """

    how: str
    path: str | None
    node_id: str | None
    reason: str | None
    definition: tuple[str, str, int] | None


@dataclass(frozen=True)
class Inventory:
    """What pytest collects at product_sha, as the probe saw it."""

    items: tuple[TestItem, ...]
    test_files: tuple[str, ...]
    inipath: str | None
    plugins: tuple[str, ...]
    excluded: tuple[Excluded, ...]
    non_function: tuple[str, ...]  # node ids of collected items that are not functions


def deploy_probe(into: Path) -> Path:
    """Write the probe's source into a fresh directory under `into`; return that directory."""
    into.mkdir(parents=True, exist_ok=True)
    probe_dir = Path(tempfile.mkdtemp(prefix="probe-", dir=into))
    source = resources.files("spec_runner").joinpath("criteria_probe.py").read_text("utf-8")
    (probe_dir / f"{PROBE_MODULE}.py").write_text(source, encoding="utf-8")
    return probe_dir


def collect(
    env: Environment,
    checkout: Path,
    sha: str,
    probe_dir: Path,
    work: Path,
    deadline: Deadline,
    local_timeout: float,
) -> Inventory:
    """Run the probe's collect mode in `checkout` (at `sha`) and return its inventory."""
    try:
        inventory = _collect(env, checkout, sha, probe_dir, work, deadline, local_timeout)
    except BaseException:
        # The original failure is the answer; a reset that cannot run (an exhausted
        # deadline) must not replace it.
        with contextlib.suppress(Exception):
            reset_checkout(checkout, sha, deadline)
        raise
    reset_checkout(checkout, sha, deadline)
    return inventory


def _collect(
    env: Environment,
    checkout: Path,
    sha: str,
    probe_dir: Path,
    work: Path,
    deadline: Deadline,
    local_timeout: float,
) -> Inventory:
    manifest = _run_probe(env, checkout, probe_dir, work, deadline, local_timeout)
    if manifest["errors"]:
        where = ", ".join(str(e["node_id"]) or "<session>" for e in manifest["errors"])
        raise CriteriaError(ErrorKind.COLLECTION_ERROR, f"pytest could not collect: {where}")
    inventory = _inventory(manifest, checkout, sha, deadline)
    changed = changed_since(checkout, sha, deadline)
    if changed:
        raise CriteriaError(
            ErrorKind.COLLECTION_MUTATED_CHECKOUT,
            f"collection changed tracked files: {', '.join(changed)}",
        )
    return inventory


def _run_probe(
    env: Environment,
    checkout: Path,
    probe_dir: Path,
    work: Path,
    deadline: Deadline,
    local_timeout: float,
) -> dict[str, Any]:
    work.mkdir(parents=True, exist_ok=True)
    invocation = Path(tempfile.mkdtemp(prefix="collect-", dir=work))
    manifest_path = invocation / "manifest.json"
    tmp = invocation / "tmp"
    tmp.mkdir()
    argv = [
        str(env.python), "-P", "-m", "pytest", "-p", PROBE_MODULE,
        "--collect-only", "-q",
    ]  # fmt: skip
    variables = {
        PARENT_ENV: str(os.getpid()),
        MODE_ENV: "collect",
        MANIFEST_ENV: str(manifest_path),
        "TMPDIR": str(tmp),
    }
    try:
        done = criteria_process.run_bounded(
            argv,
            cwd=checkout,
            env=child_env(probe_dir, variables),
            deadline=deadline,
            local_timeout=local_timeout,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if done.timed_out == "global":
        raise CriteriaError(ErrorKind.TIMEOUT, "collection: the measurement deadline expired")
    if done.timed_out == "local" or done.returncode is None:
        raise CriteriaError(
            ErrorKind.COLLECTION_FAILED, f"collection: no answer within {local_timeout:g}s"
        )
    raw = read_manifest(manifest_path)
    manifest = valid_collect(raw, child_pid=done.pid, returncode=done.returncode)
    if manifest is None:
        _raise_initial_conftest_failure(done, raw)
        what = "no collection manifest" if raw is None else "an invalid collection manifest"
        raise CriteriaError(
            ErrorKind.COLLECTION_FAILED, f"{what} (exit {done.returncode}): {_tail(done)}"
        )
    return manifest


def _raise_initial_conftest_failure(done: Finished, raw: object) -> None:
    """pytest reports a broken initial conftest only in its output (R21)."""
    output = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    if raw is None and done.returncode == _USAGE_ERROR and _CONFTEST_FAILURE in output:
        raise CriteriaError(ErrorKind.COLLECTION_ERROR, f"a conftest failed to load: {_tail(done)}")


def _tail(done: Finished) -> str:
    """The last lines of stderr (stdout when stderr is empty) — pytest reports on stdout."""
    stream = done.stderr if done.stderr.strip() else done.stdout
    lines = stream.decode("utf-8", errors="replace").strip().splitlines()
    return "\n".join(lines[-_TAIL_LINES:])


def _relative(path: str, checkout: Path) -> str | None:
    """`path` relative to the checkout as POSIX, or None when it lies outside."""
    base = checkout.resolve()
    for candidate in (Path(path).resolve(), Path(os.path.abspath(path))):
        if candidate.is_relative_to(base):
            return candidate.relative_to(base).as_posix()
    return None


def _inventory(manifest: dict[str, Any], checkout: Path, sha: str, deadline: Deadline) -> Inventory:
    inipath_abs = manifest["inipath"]
    inipath = _relative(inipath_abs, checkout) if inipath_abs is not None else None
    if inipath_abs is not None and inipath is None:
        raise CriteriaError(
            ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT,
            f"pytest read its configuration from {inipath_abs}, outside the checkout",
        )
    files: set[str] = {inipath} if inipath else set()
    for conftest in manifest["conftests"]:
        rel = _relative(conftest, checkout)
        if rel is not None:
            files.add(rel)
    tracked = frozenset(tracked_files(checkout, sha, deadline))
    items: list[TestItem] = []
    non_function: list[str] = []
    for raw in manifest["items"]:
        if not raw["function"]:
            non_function.append(raw["node_id"])  # doctests and plugin items own no tokens
            continue
        item = _item(raw, checkout, tracked)
        files.update({item.file, _relative(raw["module"], checkout) or item.file})
        items.append(item)
    return Inventory(
        items=tuple(items),
        test_files=tuple(sorted(files)),
        inipath=inipath,
        plugins=tuple(manifest["plugins"]),
        excluded=_excluded(manifest["excluded"], checkout, tracked),
        non_function=tuple(sorted(non_function)),
    )


def _item(raw: dict[str, Any], checkout: Path, tracked: frozenset[str]) -> TestItem:
    definition = _definition(raw["definition"], checkout, tracked)
    if definition is None or _relative(raw["module"], checkout) is None:
        raise CriteriaError(
            ErrorKind.DEFINITION_UNRESOLVED,
            f"{raw['node_id']}: its definition does not resolve to a file tracked at product_sha",
        )
    return TestItem(raw["node_id"], *definition)


def _definition(
    value: dict[str, Any] | None, checkout: Path, tracked: frozenset[str]
) -> tuple[str, str, int] | None:
    """`(file, qualname, line)` when the file is tracked at sha (R19), else None."""
    if value is None:
        return None
    file = _relative(value["file"], checkout)
    return (file, value["qualname"], value["line"]) if file in tracked else None


def _excluded(
    entries: list[dict[str, Any]], checkout: Path, tracked: frozenset[str]
) -> tuple[Excluded, ...]:
    found: set[Excluded] = set()
    for entry in entries:
        if entry["how"] == "deselected":
            definition = _definition(entry["definition"], checkout, tracked)
            found.add(Excluded("deselected", None, entry["node_id"], None, definition))
            continue
        path = _relative(entry["path"], checkout)
        if path is None:
            continue
        if entry["how"] == "skipped":
            found.add(Excluded("skipped", path, None, entry["reason"], None))
        elif _is_tracked(path, tracked):
            found.add(Excluded("ignored", path, None, None, None))
    return tuple(sorted(found, key=_exclusion_order))


def _exclusion_order(e: Excluded) -> tuple[str, str, str, tuple[str, str, int]]:
    """how, then path/node_id, then reason and definition (none first): never set order."""
    return (e.how, e.path or e.node_id or "", e.reason or "", e.definition or ("", "", -1))


def _is_tracked(path: str, tracked: frozenset[str]) -> bool:
    """A tracked file, or a directory holding one (the checkout root holds them all)."""
    if path == "." or path in tracked:
        return bool(tracked)
    prefix = f"{path}/"
    return any(name.startswith(prefix) for name in tracked)
