"""criteria-closure/v1 — error kinds, the kind → retryable → exit table, the request (#603).

The schemas under `schemas/criteria-closure/v1/` are the contract devtools
vendors; `jsonschema` is a dev dependency, so the request is validated here by
hand, mirroring `request.schema.json` (pinned by `tests/test_criteria_contract.py`),
plus the two rules the schema cannot express: every id carries the request's
`code`, and no id repeats.

Design: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §3.2, §4
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

PROTOCOL = 1

_OWNER_REPO = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+$")
_CODE = re.compile(r"^[A-Z]{2,6}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_BEH_ID = re.compile(r"^([A-Z]{2,6}):BEH-[0-9]+[a-z]?$")
_REQUEST_KEYS = frozenset(
    {"protocol", "owner_repo", "workstream", "code", "bundle_pin", "product_sha", "test_criteria"}
)
_REMOTE = re.compile(
    r"^(?:[a-z+]+://)?(?:[^@/]+@)?[^:/]+[:/](?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


class ErrorKind(str, Enum):
    """Response-level error kinds (design §3.2). The kind fixes retryable and exit."""

    REQUEST_INVALID = "request-invalid"
    OWNER_REPO_MISMATCH = "owner-repo-mismatch"
    PRODUCT_SHA_ABSENT = "product-sha-absent"
    CLONE_FAILED = "clone-failed"
    ENVIRONMENT_SYNC_FAILED = "environment-sync-failed"
    UNSUPPORTED_RUNTIME = "unsupported-runtime"
    COLLECTION_CONFIG_OUTSIDE_CHECKOUT = "collection-config-outside-checkout"
    COLLECTION_FAILED = "collection-failed"
    TIMEOUT = "timeout"
    LOCK_NOT_CURRENT = "lock-not-current"
    ENVIRONMENT_SELECTION_INVALID = "environment-selection-invalid"
    COLLECTION_ERROR = "collection-error"
    COLLECTION_MUTATED_CHECKOUT = "collection-mutated-checkout"
    PRODUCT_ROOTS_UNDECLARED = "product-roots-undeclared"
    PRODUCT_ROOTS_EMPTY = "product-roots-empty"
    PRODUCT_ROOTS_INVALID = "product-roots-invalid"
    PRODUCT_ROOTS_NO_PYTHON = "product-roots-no-python"
    PRODUCT_ROOTS_OVERLAP_TESTS = "product-roots-overlap-tests"
    DEFINITION_UNRESOLVED = "definition-unresolved"
    SELECTOR_ABSENT = "selector-absent"
    DISTRIBUTED_EXECUTION = "distributed-execution"

    @property
    def retryable(self) -> bool:
        """True exactly for the machine- or network-dependent kinds (exit 2)."""
        return self in _RETRYABLE

    @property
    def exit_code(self) -> int:
        """2 for a retryable kind, 3 for a property of the product at product_sha."""
        return 2 if self.retryable else 3


_RETRYABLE = frozenset(
    {
        ErrorKind.REQUEST_INVALID,
        ErrorKind.OWNER_REPO_MISMATCH,
        ErrorKind.PRODUCT_SHA_ABSENT,
        ErrorKind.CLONE_FAILED,
        ErrorKind.ENVIRONMENT_SYNC_FAILED,
        ErrorKind.UNSUPPORTED_RUNTIME,
        ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT,
        ErrorKind.COLLECTION_FAILED,
        ErrorKind.TIMEOUT,
    }
)


class CriteriaError(Exception):
    """A response-level failure: its kind, a detail, and the fields established so far."""

    def __init__(self, kind: ErrorKind, detail: str, **established: object) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail
        self.established: dict[str, object] = dict(established)


@dataclass(frozen=True)
class Request:
    """A parsed request; `raw` is echoed verbatim in every response."""

    raw: dict[str, object]
    owner_repo: str
    code: str
    product_sha: str
    beh_ids: tuple[str, ...] = field(default=())


def _refuse(why: str) -> CriteriaError:
    return CriteriaError(ErrorKind.REQUEST_INVALID, why)


def parse_request(data: object) -> Request:
    """Validate `data` against the v1 request contract, or raise REQUEST_INVALID."""
    if not isinstance(data, dict):
        raise _refuse("the request is not a JSON object")
    keys = set(data)
    if keys != _REQUEST_KEYS:
        missing, extra = sorted(_REQUEST_KEYS - keys), sorted(keys - _REQUEST_KEYS)
        raise _refuse(f"request keys: missing {missing}, unexpected {extra}")
    protocol = data["protocol"]
    if isinstance(protocol, bool) or protocol != PROTOCOL:
        raise _refuse(f"protocol must be {PROTOCOL}, got {protocol!r}")
    checks = (
        ("owner_repo", _OWNER_REPO),
        ("code", _CODE),
        ("bundle_pin", _SHA),
        ("product_sha", _SHA),
    )
    for key, pattern in checks:
        value = data[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise _refuse(f"{key} {value!r} does not match {pattern.pattern}")
    if not isinstance(data["workstream"], str) or not data["workstream"]:
        raise _refuse("workstream must be a non-empty string")
    return Request(
        raw=data,
        owner_repo=data["owner_repo"],
        code=data["code"],
        product_sha=data["product_sha"],
        beh_ids=_criteria_ids(data["test_criteria"], data["code"]),
    )


def _criteria_ids(criteria: object, code: str) -> tuple[str, ...]:
    """The BEH ids of `test_criteria`, each carrying `code`, none repeated."""
    if not isinstance(criteria, list):
        raise _refuse("test_criteria must be a list")
    ids: list[str] = []
    for entry in criteria:
        if not isinstance(entry, dict) or set(entry) != {"id", "verify_task"}:
            raise _refuse(f"test_criteria entry {entry!r} is not {{id, verify_task}}")
        beh_id = entry["id"]
        match = _BEH_ID.fullmatch(beh_id) if isinstance(beh_id, str) else None
        if match is None or match.group(1) != code:
            raise _refuse(f"test_criteria id {beh_id!r} is not {code}:BEH-NN")
        if not isinstance(entry["verify_task"], bool):
            raise _refuse(f"verify_task of {beh_id} is not a boolean")
        if beh_id in ids:
            raise _refuse(f"test_criteria repeats {beh_id}")
        ids.append(beh_id)
    return tuple(ids)


def normalise_remote(url: str) -> tuple[str, str] | None:
    """`(owner, name)` of a hosted remote URL, lower-cased; None for a local path."""
    if url.startswith(("/", ".", "file:")):
        return None
    match = _REMOTE.match(url.strip())
    if match is None:
        return None
    return match.group("owner").lower(), match.group("name").lower()


def owner_matches(requested: str, origin_url: str) -> bool:
    """Whether `owner_repo` names the repo `origin_url` points at.

    `owner/name` checks both. A bare name checks the name only — a repository of
    that name under any owner matches (a named boundary, design §4).
    """
    remote = normalise_remote(origin_url)
    if remote is None:
        return False
    owner, name = remote
    wanted_owner, _, wanted_name = requested.lower().rpartition("/")
    return wanted_name == name and (not wanted_owner or wanted_owner == owner)
