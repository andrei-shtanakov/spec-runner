"""BEH selectors from the collection inventory, and content_sha256 (#603 §3.5, §6.1).

A collected item's definition must be one the AST at product_sha holds at the
same (qualname, line): otherwise pytest ran something the static view cannot
name (another if/else branch, generated code) and no selector set could be
claimed complete — `definition-unresolved`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence

from .criteria_contract import CriteriaError, ErrorKind
from .criteria_inventory import Excluded, TestItem
from .criteria_tokens import owned_definitions

PYPROJECT = "pyproject.toml"


def _owned_tokens(file: str, blobs: Mapping[str, bytes]) -> dict[tuple[str, int], tuple[str, ...]]:
    blob = blobs.get(file)
    if blob is None:
        raise CriteriaError(ErrorKind.DEFINITION_UNRESOLVED, f"{file} has no blob at product_sha")
    try:
        definitions = owned_definitions(blob.decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise CriteriaError(
            ErrorKind.DEFINITION_UNRESOLVED, f"{file} cannot be parsed: {exc}"
        ) from None
    return {(d.qualname, d.line): d.tokens for d in definitions}


def select(
    items: Sequence[TestItem], beh_ids: Sequence[str], blobs: Mapping[str, bytes]
) -> dict[str, list[TestItem]]:
    """Every collected item whose definition owns each BEH's token, in inventory order."""
    owned_by_file: dict[str, dict[tuple[str, int], tuple[str, ...]]] = {}
    chosen: dict[str, list[TestItem]] = {beh: [] for beh in beh_ids}
    for item in items:
        if item.file not in owned_by_file:
            owned_by_file[item.file] = _owned_tokens(item.file, blobs)
        tokens = owned_by_file[item.file].get((item.qualname, item.line))
        if tokens is None:
            raise CriteriaError(
                ErrorKind.DEFINITION_UNRESOLVED,
                f"{item.node_id}: {item.qualname} at {item.file}:{item.line} is not a definition "
                "the file's AST holds at product_sha",
            )
        for beh in beh_ids:
            if beh in tokens:
                chosen[beh].append(item)
    return chosen


def _under(path: str, excluded_path: str) -> bool:
    return path == excluded_path or path.startswith(excluded_path + "/")


def digest_paths(
    product_files: Iterable[str],
    test_files: Iterable[str],
    excluded: Iterable[Excluded],
    tracked_py: Iterable[str],
) -> list[str]:
    """The `files` set of §6.1: product, collected test files, pyproject, excluded `.py`.

    Tracked `.py` paths at or under every skipped/ignored exclusion join the set (a
    skipped or ignored module can still be imported by a collected one); `deselected`
    adds nothing — its module is already among the test files. Sorted by UTF-8 bytes.
    """
    roots = [e.path for e in excluded if e.how in ("skipped", "ignored") and e.path is not None]
    paths = {*product_files, *test_files, PYPROJECT}
    paths.update(p for p in tracked_py if any(_under(p, root) for root in roots))
    return sorted(paths, key=lambda p: p.encode("utf-8"))


def content_sha256(
    roots: Sequence[str],
    lock_sha256: str,
    groups: Iterable[str] | None,
    extras: Iterable[str],
    files: Mapping[str, bytes],
) -> str:
    """The G6 key of design §6.1: declaration, lock, environment and file contents."""
    obj = {
        "v": 1,
        "product_roots": sorted(roots),
        "lock": lock_sha256,
        "environment": {
            "groups": None if groups is None else sorted(groups),
            "extras": sorted(extras),
        },
        "files": [
            [path, hashlib.sha256(files[path]).hexdigest()]
            for path in sorted(files, key=lambda p: p.encode("utf-8"))
        ],
    }
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()
