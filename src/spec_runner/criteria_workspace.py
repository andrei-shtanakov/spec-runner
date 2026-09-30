"""The measurement's workspace: origin, a clone at product_sha, blobs, the declared env (#603).

Design §3.3 and §3.6. The clone comes from the local object store, never the
network; the environment is `uv sync --locked` with the product's own declared
selection, into a directory outside the checkout, so the checkout can be reset
between runs. Every process runs through `criteria_process.run_bounded` under the
measurement `Deadline`; git and uv run in the C locale because their stderr is
matched by wording.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from spec_runner import criteria_process
from spec_runner.criteria_config import ProductCriteria, check_selection
from spec_runner.criteria_contract import CriteriaError, ErrorKind, owner_matches
from spec_runner.criteria_process import Deadline, Finished, c_locale_env

MIN_PRODUCT_PYTHON = (3, 12)
_GIT_STEP_LIMIT = 60.0
_INTERPRETER_LIMIT = 60.0
_INTERPRETER_PROBE = (
    "import json, platform, importlib.util; "
    "print(json.dumps([platform.python_implementation(), platform.python_version(), "
    "importlib.util.find_spec('xdist') is not None]))"
)
_NO_ORIGIN = "No such remote"
# Inherited UV_* that would change the selection, the lock mode, the install mode,
# how the lock is judged current, or which project is synced (`uv sync --help`,
# uv 0.11.23). Cache, network, offline and Python-install variables are kept.
_UV_OVERRIDES = frozenset(
    {
        # lock mode
        "UV_LOCKED",
        "UV_FROZEN",
        # selection
        "UV_NO_DEV",
        "UV_NO_DEFAULT_GROUPS",
        # install mode
        "UV_NO_EDITABLE",
        "UV_NO_INSTALL_PROJECT",
        "UV_NO_INSTALL_LOCAL",
        "UV_NO_INSTALL_WORKSPACE",
        # resolution settings recorded in the lock (a mismatch reads as a stale lock)
        "UV_EXCLUDE_NEWER",
        "UV_RESOLUTION",
        "UV_PRERELEASE",
        "UV_FORK_STRATEGY",
        "UV_NO_SOURCES",
        "UV_NO_SOURCES_PACKAGE",
        # which project
        "UV_PROJECT",
        "UV_WORKING_DIR",
    }
)
_STALE_LOCK = "`--locked` was provided"
_CONFLICTING_SELECTION = "are incompatible with the conflicts"


@dataclass(frozen=True)
class Environment:
    """The provisioned product environment and the selection it was built from."""

    python: Path
    implementation: str
    version: str
    lock_sha256: str
    has_xdist: bool
    groups: tuple[str, ...] | None  # None = uv's default groups
    extras: tuple[str, ...]

    @property
    def label(self) -> str:
        """`environment.python` in the response, e.g. `CPython 3.12.13`."""
        return f"{self.implementation} {self.version}"


def _failure(kind: ErrorKind, what: str, done: Finished) -> CriteriaError:
    """TIMEOUT for the global limit, `kind` for anything else that went wrong."""
    if done.timed_out == "global":
        return CriteriaError(ErrorKind.TIMEOUT, f"{what}: the measurement deadline expired")
    if done.timed_out == "local":
        return CriteriaError(kind, f"{what}: no answer within the step limit")
    tail = done.stderr.decode("utf-8", errors="replace").strip()[-500:]
    return CriteriaError(kind, f"{what}: exit {done.returncode}: {tail}")


def _git(
    cwd: Path, args: Sequence[str], deadline: Deadline, stdin: bytes | None = None
) -> Finished:
    return criteria_process.run_bounded(
        ["git", *args],
        cwd=cwd,
        env=c_locale_env(),
        deadline=deadline,
        local_timeout=_GIT_STEP_LIMIT,
        stdin=stdin,
    )


def _git_ok(cwd: Path, args: Sequence[str], deadline: Deadline, kind: ErrorKind) -> Finished:
    done = _git(cwd, args, deadline)
    if done.timed_out is not None or done.returncode != 0:
        raise _failure(kind, f"git {args[0]}", done)
    return done


def check_origin(project_root: Path, owner_repo: str, deadline: Deadline) -> None:
    """Refuse (retryable) when the source repo's origin is not the requested repo."""
    done = _git(project_root, ["remote", "get-url", "origin"], deadline)
    no_origin = done.returncode != 0 and _NO_ORIGIN.encode() in done.stderr
    if done.timed_out is not None or (done.returncode != 0 and not no_origin):
        raise _failure(ErrorKind.CLONE_FAILED, "git remote get-url origin", done)
    url = "" if no_origin else done.stdout.decode("utf-8", errors="replace").strip()
    if not url or not owner_matches(owner_repo, url):
        raise CriteriaError(
            ErrorKind.OWNER_REPO_MISMATCH,
            f"owner_repo {owner_repo!r} is not this repository's origin ({url or 'none'})",
        )


def clone_at(project_root: Path, sha: str, into: Path, deadline: Deadline) -> Path:
    """A fresh clone of `project_root` detached at exactly `sha`."""
    present = _git(project_root, ["cat-file", "-e", f"{sha}^{{commit}}"], deadline)
    if present.timed_out is not None:
        raise _failure(ErrorKind.CLONE_FAILED, "git cat-file -e", present)
    if present.returncode != 0:
        raise CriteriaError(
            ErrorKind.PRODUCT_SHA_ABSENT,
            f"{sha[:12]} is not in the local object store — fetch and retry",
        )
    checkout = into / "src"
    into.mkdir(parents=True, exist_ok=True)
    clone = ["clone", "-q", "--no-local", "--no-checkout", str(project_root), str(checkout)]
    _git_ok(into, clone, deadline, ErrorKind.CLONE_FAILED)
    _git_ok(checkout, ["checkout", "-q", "--detach", sha], deadline, ErrorKind.CLONE_FAILED)
    head = _git_ok(checkout, ["rev-parse", "HEAD"], deadline, ErrorKind.CLONE_FAILED)
    resolved = head.stdout.decode("ascii", errors="replace").strip()
    if resolved != sha:
        raise CriteriaError(ErrorKind.CLONE_FAILED, f"checkout resolved to {resolved}, not {sha}")
    return checkout


def read_blobs(
    checkout: Path, sha: str, paths: Sequence[str], deadline: Deadline
) -> dict[str, bytes]:
    """The exact committed bytes of `paths` at `sha`, from one `git cat-file --batch`."""
    if not paths:
        return {}
    bad = [p for p in paths if "\n" in p or "\r" in p]
    if bad:
        raise CriteriaError(ErrorKind.CLONE_FAILED, f"unreadable in a batch request: {bad[0]!r}")
    request = b"".join(f"{sha}:{path}\n".encode() for path in paths)
    done = criteria_process.run_bounded(
        ["git", "cat-file", "--batch"],
        cwd=checkout,
        env=c_locale_env(),
        deadline=deadline,
        local_timeout=_GIT_STEP_LIMIT,
        stdin=request,
    )
    if done.timed_out is not None or done.returncode != 0:
        raise _failure(ErrorKind.CLONE_FAILED, "git cat-file --batch", done)
    return _parse_batch(done.stdout, paths, sha)


def _parse_batch(out: bytes, paths: Sequence[str], sha: str) -> dict[str, bytes]:
    """Frame `git cat-file --batch` output by its headers' declared sizes, byte for byte."""
    blobs: dict[str, bytes] = {}
    pos = 0
    for path in paths:
        end = out.find(b"\n", pos)
        if end < 0:
            raise _malformed(path, "no header")
        header = out[pos:end]
        if header.endswith(b" missing"):
            raise CriteriaError(ErrorKind.CLONE_FAILED, f"{path} is not in {sha[:12]}")
        fields = header.split(b" ")
        if len(fields) != 3 or not fields[2].isdigit():
            raise _malformed(path, f"header {header[:120]!r}")
        if fields[1] != b"blob":
            raise CriteriaError(
                ErrorKind.CLONE_FAILED,
                f"{path} at {sha[:12]} is a {fields[1].decode('ascii', errors='replace')}",
            )
        start = end + 1
        stop = start + int(fields[2])
        if len(out) < stop + 1 or out[stop : stop + 1] != b"\n":
            raise _malformed(path, "truncated payload")
        blobs[path] = out[start:stop]
        pos = stop + 1
    if pos != len(out):
        raise _malformed(paths[-1], f"{len(out) - pos} unexpected trailing bytes")
    return blobs


def _malformed(path: str, detail: str) -> CriteriaError:
    return CriteriaError(ErrorKind.CLONE_FAILED, f"git cat-file --batch at {path}: {detail}")


def reset_checkout(checkout: Path, sha: str, deadline: Deadline) -> None:
    """`reset --hard <sha>` and `clean -ffdx`: the checkout is `sha` and nothing else (§3.6)."""
    _git_ok(checkout, ["reset", "-q", "--hard", sha], deadline, ErrorKind.CLONE_FAILED)
    _git_ok(checkout, ["clean", "-q", "-ffdx"], deadline, ErrorKind.CLONE_FAILED)


def tracked_changes(checkout: Path, deadline: Deadline) -> list[str]:
    """Tracked paths whose working-tree state differs from HEAD, sorted."""
    args = ["status", "--porcelain", "--untracked-files=no", "-z"]
    done = _git_ok(checkout, args, deadline, ErrorKind.CLONE_FAILED)
    entries = done.stdout.split(b"\0")
    changed: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        changed.add(entry[3:].decode("utf-8", errors="surrogateescape"))
        if entry[:1] in (b"R", b"C"):
            index += 1  # the rename/copy source follows as its own entry
    return sorted(changed)


def selection_args(criteria: ProductCriteria) -> list[str]:
    """uv's flags for the product's declared selection (Global Constraints)."""
    args: list[str] = []
    if criteria.groups is not None:
        args.append("--no-default-groups")
        for group in criteria.groups:
            args += ["--group", group]
    for extra in criteria.extras:
        args += ["--extra", extra]
    return args


def sync_environment(
    checkout: Path, sha: str, env_dir: Path, criteria: ProductCriteria, deadline: Deadline
) -> Environment:
    """`uv sync --locked` with the declared selection into `env_dir`, then read the interpreter."""
    check_selection(checkout, sha, criteria, deadline)
    lock = checkout / "uv.lock"
    if not lock.is_file():
        raise CriteriaError(ErrorKind.LOCK_NOT_CURRENT, "no uv.lock at product_sha")
    lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
    _uv_sync(checkout, env_dir, criteria, deadline)
    python = env_dir / "bin" / "python"
    with tempfile.TemporaryDirectory(prefix="criteria-empty-") as empty:
        implementation, version, has_xdist = _interpreter(python, checkout, Path(empty), deadline)
        _require_pytest(python, checkout, Path(empty), deadline)
    return Environment(
        python=python,
        implementation=implementation,
        version=version,
        lock_sha256=lock_sha256,
        has_xdist=has_xdist,
        groups=criteria.groups,
        extras=criteria.extras,
    )


def _uv_sync(checkout: Path, env_dir: Path, criteria: ProductCriteria, deadline: Deadline) -> None:
    env = {
        k: v
        for k, v in c_locale_env().items()
        if not k.startswith("PYTEST_") and k not in _UV_OVERRIDES and k != "VIRTUAL_ENV"
    }
    env["UV_PROJECT_ENVIRONMENT"] = str(env_dir)
    # No --quiet: measured (uv 0.11.23) to suppress the "`--locked` was provided"
    # line this classification reads, turning a stale lock into a retryable failure.
    argv = ["uv", "sync", "--locked", *selection_args(criteria)]
    done = criteria_process.run_bounded(argv, cwd=checkout, env=env, deadline=deadline)
    if done.timed_out is None and done.returncode == 0:
        return
    said = done.stderr.decode("utf-8", errors="replace")
    if done.timed_out is None and _STALE_LOCK in said:
        raise CriteriaError(ErrorKind.LOCK_NOT_CURRENT, f"uv sync --locked: {said.strip()}")
    if done.timed_out is None and _CONFLICTING_SELECTION in said:
        raise CriteriaError(
            ErrorKind.ENVIRONMENT_SELECTION_INVALID, f"uv sync --locked: {said.strip()}"
        )
    raise _failure(ErrorKind.ENVIRONMENT_SYNC_FAILED, "uv sync --locked", done)


def _run_python(
    python: Path, code: str, checkout: Path, empty: Path, deadline: Deadline
) -> Finished:
    return criteria_process.run_bounded(
        [str(python), "-P", "-c", code],
        cwd=checkout,
        env=child_env(empty, {}),
        deadline=deadline,
        local_timeout=_INTERPRETER_LIMIT,
    )


def _interpreter(
    python: Path, checkout: Path, empty: Path, deadline: Deadline
) -> tuple[str, str, bool]:
    done = _run_python(python, _INTERPRETER_PROBE, checkout, empty, deadline)
    if done.timed_out is not None or done.returncode != 0:
        raise _failure(ErrorKind.ENVIRONMENT_SYNC_FAILED, "interpreter check", done)
    try:
        implementation, version, has_xdist = json.loads(done.stdout)
        major_minor = tuple(int(part) for part in str(version).split(".")[:2])
    except (ValueError, TypeError):
        raise CriteriaError(
            ErrorKind.ENVIRONMENT_SYNC_FAILED,
            f"interpreter check: unreadable answer {done.stdout[:200]!r}",
        ) from None
    if implementation != "CPython" or major_minor < MIN_PRODUCT_PYTHON:
        raise CriteriaError(
            ErrorKind.UNSUPPORTED_RUNTIME,
            f"the product environment is {implementation} {version}; CPython >= 3.12 is required",
        )
    return str(implementation), str(version), bool(has_xdist)


def _require_pytest(python: Path, checkout: Path, empty: Path, deadline: Deadline) -> None:
    done = _run_python(python, "import pytest", checkout, empty, deadline)
    if done.timed_out is not None:
        raise _failure(ErrorKind.ENVIRONMENT_SYNC_FAILED, "pytest import check", done)
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise CriteriaError(
            ErrorKind.ENVIRONMENT_SELECTION_INVALID,
            f"pytest is not importable in the selected environment: {tail}",
        )


def child_env(probe_dir: Path, extra: Mapping[str, str]) -> dict[str, str]:
    """The product Python's environment: no PYTHON*/PYTEST_*/VIRTUAL_ENV, the probe dir only."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTHON", "PYTEST_")) and k != "VIRTUAL_ENV"
    }
    env["PYTHONPATH"] = str(probe_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra)
    return env


def distribution_args(env: Environment) -> list[str]:
    """Keep every test in the probe's own process when xdist is importable (§3.6)."""
    return ["-n", "0", "--dist", "no"] if env.has_xdist else []
