"""One isolated, bounded, validated selector run (#603 §3.6, §4).

`run_selector` resets the checkout to `product_sha`, starts one fresh pytest
process with the probe for exactly one node id, and reads the result:

- a global deadline expiry is `CriteriaError(TIMEOUT)` for the whole measurement;
- a local `--selector-timeout` expiry, measured files that no longer hold their
  bytes at `product_sha`, a manifest that fails probe/1 validation, or a probe
  that could not monitor are an **error run** — a manifest that fails validation
  is not evidence, so nothing of it is reported;
- a valid manifest saying the call left the designated process is
  `DISTRIBUTED_EXECUTION`, one that collected nothing is `SELECTOR_ABSENT`;
- otherwise a **complete run**, its product lines narrowed to function bodies
  by devtools' AST rule over the bytes at `product_sha`.

Never imports pytest: the probe runs inside the product's environment.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spec_runner import criteria_process
from spec_runner.criteria_aggregate import run_outcome
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import DEFAULT_MAX_OUTPUT, Deadline, Finished
from spec_runner.criteria_protocol import (
    MANIFEST_ENV,
    MODE_ENV,
    PARENT_ENV,
    PROBE_MODULE,
    PRODUCT_FILES_ENV,
    read_manifest,
    valid_run,
)
from spec_runner.criteria_tokens import function_body_lines
from spec_runner.criteria_workspace import (
    Environment,
    child_env,
    remove_tree_quietly,
    reset_checkout,
)

_TAIL_LINES = 20
MUTATED = "measured-files-mutated"
#: An error run's `detail` keeps at most this many characters (its end).
MAX_DETAIL = 4000
_PARSE_FAILURES = (SyntaxError, ValueError, RecursionError, MemoryError)


def product_body_lines(
    product_files: Sequence[str], blobs: Mapping[str, bytes], *, product_version: str
) -> dict[str, frozenset[int]]:
    """Function-body lines of every product file at `product_sha`, computed once before any run.

    A committed product file this interpreter cannot parse (R-B16, refining R-B11) is
    UNSUPPORTED_RUNTIME only when the orchestrator's Python is older than the product
    environment's (`product_version`, e.g. "3.12.13" — its grammar may be beyond ours,
    PEP 695 under 3.11), compared as (major, minor, micro) so 3.12.0 is older than
    3.12.13; otherwise the file is the product's own fault:
    PRODUCT_ROOTS_INVALID. Either detail names the file and both versions.
    """
    lines: dict[str, frozenset[int]] = {}
    for path in product_files:
        try:
            lines[path] = function_body_lines(importlib.util.decode_source(blobs[path]))
        except _PARSE_FAILURES as exc:
            raise _unparseable(path, product_version, exc) from None
    return lines


def _unparseable(path: str, product_version: str, exc: BaseException) -> CriteriaError:
    product = _version_triple(product_version)
    older = product is not None and _orchestrator_version() < product
    kind = ErrorKind.UNSUPPORTED_RUNTIME if older else ErrorKind.PRODUCT_ROOTS_INVALID
    return CriteriaError(
        kind,
        f"{path} at product_sha cannot be parsed by orchestrator Python "
        f"{sys.version.split()[0]} (product Python {product_version}): "
        f"{type(exc).__name__}: {exc}",
    )


def _orchestrator_version() -> tuple[int, int, int]:
    return sys.version_info[0], sys.version_info[1], sys.version_info[2]


def _version_triple(version: str) -> tuple[int, int, int] | None:
    """`(major, minor, micro)` of "3.12.13" / "3.13.0rc1" (micro = its leading digits).

    A missing micro ("3.12") reads as 0; None when the version does not start with
    `major.minor` — the caller then blames the product, as before.
    """
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3] or 0)


def run_selector(
    env: Environment,
    checkout: Path,
    sha: str,
    probe_dir: Path,
    work: Path,
    node_id: str,
    product_files: Sequence[str],
    measured: Mapping[str, bytes],
    body_lines: Mapping[str, frozenset[int]],
    deadline: Deadline,
    selector_timeout: float,
    *,
    rootpath: str,
    inipath: str | None,
    plugin_args: Sequence[str] = (),
) -> dict[str, object]:
    """One fresh pytest process for `node_id` → a `complete_run` or an `error_run` object.

    `product_files` are repository-relative; `measured` holds the bytes at `sha`
    of every measured file (product ∪ test files, from `read_blobs`, never re-read
    from the checkout) and `body_lines` is `product_body_lines(product_files, …)`. `plugin_args`
    are appended before `-q` (`criteria_inventory.plugin_args(inventory)`). `rootpath` and
    `inipath` (absolute) are the collection's resolution, reproduced by every run as
    `--rootdir rootpath -c inipath` — or `-c` an empty ini in the invocation dir when
    the collection read no config (R-B15). Raises
    `CriteriaError` with TIMEOUT, SELECTOR_ABSENT or DISTRIBUTED_EXECUTION.
    """
    reset_checkout(checkout, sha, deadline)
    work.mkdir(parents=True, exist_ok=True)
    invocation = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    try:
        config = _config_args(invocation, rootpath, inipath)
        done, manifest_path = _launch(
            env, checkout, probe_dir, invocation, node_id, product_files, deadline,
            selector_timeout, [*config, *plugin_args],
        )  # fmt: skip
        return _read_run(
            done, manifest_path, checkout, node_id, product_files, measured, body_lines,
            selector_timeout,
        )  # fmt: skip
    finally:
        remove_tree_quietly(invocation)


def _config_args(invocation: Path, rootpath: str, inipath: str | None) -> list[str]:
    """The collection's rootdir and config file; an empty ini when it read none (R-B15)."""
    if inipath is None:
        empty = invocation / "pytest.ini"
        empty.write_text("", encoding="utf-8")
        inipath = str(empty)
    return ["--rootdir", rootpath, "-c", inipath]


def _launch(
    env: Environment,
    checkout: Path,
    probe_dir: Path,
    invocation: Path,
    node_id: str,
    product_files: Sequence[str],
    deadline: Deadline,
    selector_timeout: float,
    extra: Sequence[str],
) -> tuple[Finished, Path]:
    manifest_path = invocation / "manifest.json"
    files_path = invocation / "product-files.json"
    files_path.write_text(json.dumps([str(checkout / p) for p in product_files]), "utf-8")
    tmp = invocation / "tmp"
    tmp.mkdir()
    argv = [
        str(env.python), "-P", "-m", "pytest", "-p", PROBE_MODULE,
        *extra, "-q", node_id,
    ]  # fmt: skip
    variables = {
        PARENT_ENV: str(os.getpid()),
        MODE_ENV: "run",
        MANIFEST_ENV: str(manifest_path),
        PRODUCT_FILES_ENV: str(files_path),
        "TMPDIR": str(tmp),
    }
    done = criteria_process.run_bounded(
        argv,
        cwd=checkout,
        env=child_env(probe_dir, variables),
        deadline=deadline,
        local_timeout=selector_timeout,
        max_output=DEFAULT_MAX_OUTPUT,  # read only as a tail (R-B20)
    )
    return done, manifest_path


def _read_run(
    done: Finished,
    manifest_path: Path,
    checkout: Path,
    node_id: str,
    product_files: Sequence[str],
    measured: Mapping[str, bytes],
    body_lines: Mapping[str, frozenset[int]],
    selector_timeout: float,
) -> dict[str, object]:
    """Global timeout, local timeout, mutated files, manifest validity — in that order."""
    if done.timed_out == "global":
        raise CriteriaError(ErrorKind.TIMEOUT, f"{node_id}: the measurement deadline expired")
    if done.timed_out == "local":
        # No mutation check: the run is already an error run, and the next run's
        # reset_checkout restores the tree before anything is measured again.
        return _error(f"{node_id} exceeded {selector_timeout:g}s", timed_out=True)
    if done.returncode is None:  # run_bounded promises an exit status unless a limit fired
        return _error(f"{node_id}: no exit status")
    mutated = _mutated(checkout, measured)
    if mutated:
        return _error(MUTATED, mutated_paths=mutated, exit_status=done.returncode)
    raw = read_manifest(manifest_path)
    manifest = valid_run(raw, child_pid=done.pid, returncode=done.returncode)
    if manifest is None:
        what = "no run manifest" if raw is None else "an invalid run manifest"
        tail = _tail(done)
        detail = f"{node_id}: {what} (exit {done.returncode})" + (f": {tail}" if tail else "")
        return _error(detail, exit_status=done.returncode)
    declared = frozenset(product_files)
    return _from_manifest(manifest, checkout, node_id, declared, body_lines, done.returncode)


def _from_manifest(
    manifest: dict[str, Any],
    checkout: Path,
    node_id: str,
    declared: frozenset[str],
    body_lines: Mapping[str, frozenset[int]],
    exit_status: int,
) -> dict[str, object]:
    if "monitoring_error" in manifest:
        return _error(str(manifest["monitoring_error"]), exit_status=exit_status)
    if manifest["distributed"]:
        raise CriteriaError(
            ErrorKind.DISTRIBUTED_EXECUTION,
            f"{node_id}: its call ran outside the designated pytest process",
        )
    collected: list[str] = manifest["collected"]
    if not collected:
        raise CriteriaError(ErrorKind.SELECTOR_ABSENT, f"{node_id} collects no test")
    if collected != [node_id]:
        detail = f"{node_id}: the run collected {', '.join(collected)}"
        return _error(detail, exit_status=exit_status)
    lines = _product_lines(manifest["product_lines"], checkout, declared, body_lines)
    if isinstance(lines, str):
        return _error(lines, exit_status=exit_status)
    phases: dict[str, str] = manifest["phases"]
    return {
        "result": "complete",
        "collected": collected,
        "phases": phases,
        "outcome": run_outcome(phases),
        "product_lines": lines,
        "product_line_count": sum(len(entry["lines"]) for entry in lines),
        # One entry per operation, first-seen order: the probe records every audit event.
        "process_operations": list(dict.fromkeys(manifest["process_operations"])),
    }


def _product_lines(
    observed: Mapping[str, list[int]],
    checkout: Path,
    declared: frozenset[str],
    body_lines: Mapping[str, frozenset[int]],
) -> list[dict[str, Any]] | str:
    """Lines in function bodies per product file, sorted; a string names what failed.

    Only a declared product file counts — a test file's lines never do.
    """
    entries: list[dict[str, Any]] = []
    for path, numbers in observed.items():
        rel = _relative(path, checkout)
        if rel is None or rel not in declared:
            return f"the probe reported lines of an undeclared product file: {path}"
        kept = sorted(set(numbers) & body_lines[rel])
        if kept:
            entries.append({"file": rel, "lines": kept})
    return sorted(entries, key=lambda entry: entry["file"])


def _mutated(checkout: Path, measured: Mapping[str, bytes]) -> list[str]:
    """Measured files whose bytes in the checkout differ from those at sha (or are gone)."""
    return sorted(path for path, expected in measured.items() if _read(checkout / path) != expected)


def _read(path: Path) -> bytes | None:
    """The bytes git would hash: a symlink's target, a regular file's content."""
    try:
        if path.is_symlink():
            return os.fsencode(os.readlink(path))
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _relative(path: str, checkout: Path) -> str | None:
    """`path` relative to the checkout as POSIX, or None when it lies outside."""
    base = checkout.resolve()
    for candidate in (Path(path).resolve(), Path(os.path.abspath(path))):
        if candidate.is_relative_to(base):
            return candidate.relative_to(base).as_posix()
    return None


def _tail(done: Finished) -> str:
    """The last lines of stderr (stdout when stderr is empty) — pytest reports on stdout."""
    stream = done.stderr if done.stderr.strip() else done.stdout
    lines = stream.decode("utf-8", errors="replace").strip().splitlines()
    return "\n".join(lines[-_TAIL_LINES:])


def _error(detail: str, **observed: object) -> dict[str, object]:
    """An `error_run`: reason `runner` plus only what the orchestrator itself observed."""
    return {"result": "error", "reason": "runner", "detail": _bounded(detail), **observed}


def _bounded(detail: str) -> str:
    """At most MAX_DETAIL characters: the end is kept, the cut announced at the head."""
    if len(detail) <= MAX_DETAIL:
        return detail
    marker = f"[truncated {len(detail)} chars] "
    return marker + detail[len(detail) - (MAX_DETAIL - len(marker)) :]
