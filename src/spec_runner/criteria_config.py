"""The product's criteria config, read and resolved at product_sha (#603 design §3.3, §3.4).

Everything here reads `git show`/`git ls-tree` at the given commit, never the working
tree, and every git call goes through `run_bounded` under the measurement `Deadline`.
The declaration is a trust boundary: the overlap check catches known test
infrastructure, it cannot prove the rest is product.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import Deadline, Finished, run_bounded

_CONFIG_LOCATIONS = ("spec-runner.config.yaml", "spec/executor.config.yaml")
_REGULAR_MODES = frozenset({"100644", "100755"})
_SYMLINK_MODE = "120000"
_NAME_RE = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
_SEPARATORS_RE = re.compile(r"[-_.]+")
_ENV_KEYS = frozenset({"groups", "extras"})
_GIT_STEP_LIMIT = 30.0


@dataclass(frozen=True)
class ProductCriteria:
    """What the product declares: roots, and the uv environment to build."""

    roots: tuple[str, ...]  # normalised, sorted, unique
    groups: tuple[str, ...] | None  # None = undeclared (uv's default groups)
    extras: tuple[str, ...]  # () when undeclared


def normalise_name(name: str) -> str:
    """PEP 503-style name: lower-case, each run of `-`, `_`, `.` becomes one `-`."""
    return _SEPARATORS_RE.sub("-", name.lower())


def read_product_criteria(checkout: Path, sha: str, deadline: Deadline) -> ProductCriteria:
    """`criteria.*` from the product's spec-runner config at `sha`."""
    location, text = _read_config(checkout, sha, deadline)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{location}: {exc}") from None
    section = data.get("executor", data) if isinstance(data, dict) else {}
    criteria = section.get("criteria") if isinstance(section, dict) else None
    if not isinstance(criteria, dict) or "product_roots" not in criteria:
        raise CriteriaError(
            ErrorKind.PRODUCT_ROOTS_UNDECLARED, f"no criteria.product_roots in {location}"
        )
    roots = _parse_roots(criteria["product_roots"])
    groups, extras = _parse_environment(criteria.get("environment"))
    return ProductCriteria(roots=roots, groups=groups, extras=extras)


_ABSENT_MARKERS = ("does not exist in", "exists on disk, but not in")


def _git_failure(what: str, done: Finished) -> CriteriaError:
    """TIMEOUT for the global limit, CLONE_FAILED for anything else that went wrong."""
    if done.timed_out == "global":
        return CriteriaError(ErrorKind.TIMEOUT, f"{what}: the measurement deadline expired")
    if done.timed_out == "local":
        return CriteriaError(ErrorKind.CLONE_FAILED, f"{what}: no answer within the step limit")
    tail = done.stderr.decode("utf-8", errors="replace").strip()[-500:]
    return CriteriaError(ErrorKind.CLONE_FAILED, f"{what}: exit {done.returncode}: {tail}")


def _show(checkout: Path, sha: str, path: str, deadline: Deadline) -> bytes | None:
    """The bytes of `path` at `sha`; None only when the commit has no such path."""
    what = f"git show {sha[:12]}:{path}"
    done = run_bounded(
        ["git", "show", f"{sha}:{path}"],
        cwd=checkout,
        env=None,
        deadline=deadline,
        local_timeout=_GIT_STEP_LIMIT,
    )
    if done.timed_out is None and done.returncode == 0:
        return done.stdout
    stderr = done.stderr.decode("utf-8", errors="replace")
    if done.timed_out is None and any(marker in stderr for marker in _ABSENT_MARKERS):
        return None
    raise _git_failure(what, done)


def _read_config(checkout: Path, sha: str, deadline: Deadline) -> tuple[str, str]:
    for location in _CONFIG_LOCATIONS:
        raw = _show(checkout, sha, location, deadline)
        if raw is None:
            continue
        try:
            return location, raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CriteriaError(
                ErrorKind.PRODUCT_ROOTS_INVALID, f"{location} is not UTF-8"
            ) from None
    raise CriteriaError(ErrorKind.PRODUCT_ROOTS_UNDECLARED, "no spec-runner config at product_sha")


def _parse_roots(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, "criteria.product_roots is not a list")
    if not raw:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_EMPTY, "criteria.product_roots is empty")
    normalised: list[str] = []
    for entry in raw:
        root = _normalise_root(entry)
        if root in normalised:
            raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} is declared twice")
        normalised.append(root)
    return tuple(sorted(normalised))


def _normalise_root(entry: object) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} is not a path")
    path = PurePosixPath(entry.strip())
    if path.is_absolute() or ".." in path.parts:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} escapes the checkout")
    parts = [part for part in path.parts if part not in (".", "")]
    if not parts:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} is the repository root")
    return "/".join(parts)


def _invalid_env(detail: str) -> CriteriaError:
    return CriteriaError(ErrorKind.ENVIRONMENT_SELECTION_INVALID, detail)


def _parse_environment(raw: object) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    if raw is None:
        return None, ()
    if not isinstance(raw, dict) or not set(raw) <= _ENV_KEYS:
        raise _invalid_env("criteria.environment may hold only `groups` and `extras`")
    groups = _parse_names(raw["groups"], "groups") if "groups" in raw else None
    extras = _parse_names(raw["extras"], "extras") if "extras" in raw else ()
    return groups, extras


def _parse_names(raw: object, what: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise _invalid_env(f"criteria.environment.{what} is not a list")
    names: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not _NAME_RE.fullmatch(entry):
            raise _invalid_env(f"criteria.environment.{what}: {entry!r} is not a valid name")
        name = normalise_name(entry)
        if name in names:
            raise _invalid_env(f"criteria.environment.{what}: {name!r} is declared twice")
        names.append(name)
    return tuple(sorted(names))


def check_selection(
    checkout: Path, sha: str, criteria: ProductCriteria, deadline: Deadline
) -> None:
    """Refuse a declared group/extra that `pyproject.toml` at `sha` does not define."""
    if criteria.groups is None and not criteria.extras:
        return
    pyproject = _load_pyproject(checkout, sha, deadline)
    groups_table = _table(pyproject, "dependency-groups")
    available_groups = {normalise_name(k) for k in groups_table}
    if "dev-dependencies" in _table(_table(pyproject, "tool"), "uv"):
        available_groups.add("dev")
    extras_table = _table(_table(pyproject, "project"), "optional-dependencies")
    available_extras = {normalise_name(k) for k in extras_table}
    missing = [f"group {g}" for g in criteria.groups or () if g not in available_groups]
    missing += [f"extra {e}" for e in criteria.extras if e not in available_extras]
    if missing:
        raise _invalid_env(f"not defined in pyproject.toml at {sha[:12]}: {', '.join(missing)}")


def _table(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """`parent[key]` as a table; absent is empty, a non-table is an invalid selection."""
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise _invalid_env(f"pyproject.toml: `{key}` is not a table")
    return value


def _load_pyproject(checkout: Path, sha: str, deadline: Deadline) -> dict[str, Any]:
    raw = _show(checkout, sha, "pyproject.toml", deadline)
    if raw is None:
        raise _invalid_env(
            f"an environment is declared but there is no pyproject.toml at {sha[:12]}"
        )
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid_env(f"pyproject.toml at {sha[:12]} is unreadable: {exc}") from None


def resolve_roots(checkout: Path, sha: str, roots: Sequence[str], deadline: Deadline) -> list[str]:
    """Every tracked regular `.py` file under the declared roots at `sha`, sorted."""
    files: set[str] = set()
    for root in roots:
        entries = _ls_tree(checkout, sha, root, deadline)
        if not entries:
            raise CriteriaError(
                ErrorKind.PRODUCT_ROOTS_INVALID, f"{root} is not tracked at {sha[:12]}"
            )
        for entry in entries:
            meta, path = entry.split("\t", 1)
            mode = meta.split(" ", 1)[0]
            if mode == _SYMLINK_MODE:
                raise CriteriaError(
                    ErrorKind.PRODUCT_ROOTS_INVALID,
                    f"{path} is a symlink; a root may not reach outside",
                )
            if mode in _REGULAR_MODES and path.endswith(".py"):
                files.add(path)
    if not files:
        raise CriteriaError(
            ErrorKind.PRODUCT_ROOTS_NO_PYTHON, f"{', '.join(roots)} hold no Python file"
        )
    return sorted(files)


def _ls_tree(checkout: Path, sha: str, root: str, deadline: Deadline) -> list[str]:
    done = run_bounded(
        ["git", "ls-tree", "-r", "-z", sha, "--", root],
        cwd=checkout,
        env=None,
        deadline=deadline,
        local_timeout=_GIT_STEP_LIMIT,
    )
    if done.timed_out is not None or done.returncode != 0:
        raise _git_failure(f"git ls-tree {sha[:12]} -- {root}", done)
    return [e for e in done.stdout.decode("utf-8", errors="replace").split("\0") if e]


def check_overlap(files: Sequence[str], test_files: Sequence[str]) -> None:
    """Refuse a product file that is also a collected test module or loaded conftest/config."""
    overlap = sorted(set(files) & set(test_files))
    if overlap:
        raise CriteriaError(
            ErrorKind.PRODUCT_ROOTS_OVERLAP_TESTS,
            f"declared product roots include test files: {', '.join(overlap)}",
        )
