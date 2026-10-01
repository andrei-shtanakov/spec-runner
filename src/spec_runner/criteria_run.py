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
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from spec_runner import criteria_process
from spec_runner.criteria_aggregate import run_outcome
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import Deadline, Finished
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
from spec_runner.criteria_workspace import Environment, child_env, reset_checkout

_TAIL_LINES = 20
MUTATED = "measured-files-mutated"


def run_selector(
    env: Environment,
    checkout: Path,
    sha: str,
    probe_dir: Path,
    work: Path,
    node_id: str,
    product_files: Sequence[str],
    measured: Mapping[str, bytes],
    blobs: Mapping[str, bytes],
    deadline: Deadline,
    selector_timeout: float,
    *,
    distribution: Sequence[str] = (),
) -> dict[str, object]:
    """One fresh pytest process for `node_id` → a `complete_run` or an `error_run` object.

    `product_files` are repository-relative; `measured` holds the bytes at `sha`
    of every measured file (product ∪ test files) and `blobs` the product files'
    — both from `read_blobs`, never re-read from the checkout. `distribution` is
    appended before `-q` (`distribution_args(inventory.xdist_active)`). Raises
    `CriteriaError` with TIMEOUT, SELECTOR_ABSENT or DISTRIBUTED_EXECUTION.
    """
    reset_checkout(checkout, sha, deadline)
    work.mkdir(parents=True, exist_ok=True)
    invocation = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    try:
        done, manifest_path = _launch(
            env, checkout, probe_dir, invocation, node_id, product_files, deadline,
            selector_timeout, distribution,
        )  # fmt: skip
        return _read_run(done, manifest_path, checkout, node_id, measured, blobs, selector_timeout)
    finally:
        shutil.rmtree(invocation, ignore_errors=True)


def _launch(
    env: Environment,
    checkout: Path,
    probe_dir: Path,
    invocation: Path,
    node_id: str,
    product_files: Sequence[str],
    deadline: Deadline,
    selector_timeout: float,
    distribution: Sequence[str],
) -> tuple[Finished, Path]:
    manifest_path = invocation / "manifest.json"
    files_path = invocation / "product-files.json"
    files_path.write_text(json.dumps([str(checkout / p) for p in product_files]), "utf-8")
    tmp = invocation / "tmp"
    tmp.mkdir()
    argv = [
        str(env.python), "-P", "-m", "pytest", "-p", PROBE_MODULE,
        *distribution, "-q", node_id,
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
    )
    return done, manifest_path


def _read_run(
    done: Finished,
    manifest_path: Path,
    checkout: Path,
    node_id: str,
    measured: Mapping[str, bytes],
    blobs: Mapping[str, bytes],
    selector_timeout: float,
) -> dict[str, object]:
    """Global timeout, local timeout, mutated files, manifest validity — in that order."""
    if done.timed_out == "global":
        raise CriteriaError(ErrorKind.TIMEOUT, f"{node_id}: the measurement deadline expired")
    if done.timed_out == "local" or done.returncode is None:
        return _error(f"{node_id} exceeded {selector_timeout:g}s", timed_out=True)
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
    return _from_manifest(manifest, checkout, node_id, blobs, done.returncode)


def _from_manifest(
    manifest: dict[str, Any],
    checkout: Path,
    node_id: str,
    blobs: Mapping[str, bytes],
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
    lines = _product_lines(manifest["product_lines"], checkout, blobs)
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
        "process_operations": manifest["process_operations"],
    }


def _product_lines(
    observed: Mapping[str, list[int]], checkout: Path, blobs: Mapping[str, bytes]
) -> list[dict[str, Any]] | str:
    """Lines in function bodies per product file, sorted; a string names what failed."""
    entries: list[dict[str, Any]] = []
    for path, numbers in observed.items():
        rel = _relative(path, checkout)
        if rel is None or rel not in blobs:
            return f"the probe reported lines of an undeclared product file: {path}"
        try:
            bodies = function_body_lines(importlib.util.decode_source(blobs[rel]))
        except (SyntaxError, ValueError) as exc:
            return f"{rel} at product_sha does not parse: {exc}"
        kept = sorted(set(numbers) & bodies)
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
    return {"result": "error", "reason": "runner", "detail": detail, **observed}
