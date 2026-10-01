"""The measurement pipeline of `verify --criteria`, under one deadline (#603 §3.2-3.7, §4).

`measure` runs every step in order inside a temporary workspace and returns the
exit code with the one response document: the answer (exit 0), or an error
document carrying only the fields established before the failure — never `beh`.
Only `CriteriaError` becomes a document; any other exception is a bug and
propagates. The workspace is removed after every process group is gone
(`run_bounded` kills each group before it returns).

Never imports pytest: the product's pytest runs in the product's environment.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from spec_runner.criteria_aggregate import beh_status, selector_status
from spec_runner.criteria_config import (
    check_overlap,
    read_product_criteria,
    resolve_roots,
)
from spec_runner.criteria_contract import (
    PROTOCOL,
    CriteriaError,
    ErrorKind,
    Request,
    parse_request,
)
from spec_runner.criteria_inventory import (
    PLUGINS_ESTABLISHED,
    Excluded,
    TestItem,
    collect,
    deploy_probe,
)
from spec_runner.criteria_process import Deadline
from spec_runner.criteria_run import product_body_lines, run_selector
from spec_runner.criteria_select import content_sha256, digest_paths, select
from spec_runner.criteria_workspace import (
    Environment,
    check_origin,
    clone_at,
    distribution_args,
    read_blobs,
    sync_environment,
    tracked_files,
)

Document = dict[str, object]


def measure(
    project_root: Path,
    request_data: object,
    *,
    selector_timeout: float,
    timeout: float,
    version: str,
) -> tuple[int, Document]:
    """The exit code and the one response document (answer or error)."""
    deadline = Deadline(timeout)
    head: Document = {"protocol": PROTOCOL, "spec_runner_version": version}
    established: Document = {}
    try:
        request = parse_request(request_data)
        head["request"] = request.raw
        with tempfile.TemporaryDirectory(
            prefix="spec-runner-criteria-", ignore_cleanup_errors=True
        ) as tmp:
            beh = _measure(
                project_root, request, Path(tmp), deadline, selector_timeout, established
            )
    except CriteriaError as exc:
        return _error_document(exc, head, established)
    return 0, {**head, **established, "beh": beh}


def request_invalid(detail: str, *, version: str) -> tuple[int, Document]:
    """The error document for a request that could not even be read (no `request` echo)."""
    exc = CriteriaError(ErrorKind.REQUEST_INVALID, detail)
    return _error_document(exc, {"protocol": PROTOCOL, "spec_runner_version": version}, {})


def _error_document(
    exc: CriteriaError, head: Document, established: Document
) -> tuple[int, Document]:
    extra = {k: v for k, v in exc.established.items() if k != PLUGINS_ESTABLISHED}
    error = {"kind": exc.kind.value, "retryable": exc.kind.retryable, "detail": exc.detail}
    return exc.kind.exit_code, {**head, "error": error, **extra, **established}


def _measure(
    project_root: Path,
    request: Request,
    tmp: Path,
    deadline: Deadline,
    selector_timeout: float,
    established: Document,
) -> list[Document]:
    """Steps 2-11; each step's output joins `established` as soon as it is true."""
    sha = request.product_sha
    check_origin(project_root, request.owner_repo, deadline)
    checkout = clone_at(project_root, sha, tmp / "clone", deadline)
    criteria = read_product_criteria(checkout, sha, deadline)
    env = sync_environment(checkout, sha, tmp / "env", criteria, deadline)
    probe_dir = deploy_probe(tmp)
    work = tmp / "work"
    try:
        # A collection run is one pytest process, like a selector run: same local bound.
        inventory = collect(env, checkout, sha, probe_dir, work, deadline, selector_timeout)
    except CriteriaError as exc:
        plugins = exc.established.get(PLUGINS_ESTABLISHED)
        if isinstance(plugins, list):
            established["environment"] = _environment(env, plugins)
        raise
    established["environment"] = _environment(env, inventory.plugins)
    established["test_files"] = list(inventory.test_files)
    established["test_items"] = [_test_item(item) for item in inventory.items]
    established["collection_excluded"] = [_excluded(e) for e in inventory.excluded]

    files = resolve_roots(checkout, sha, criteria.roots, deadline)
    check_overlap(files, inventory.test_files)
    established["product_roots"] = {"declared": list(criteria.roots), "files": list(files)}

    tracked_py = [p for p in tracked_files(checkout, sha, deadline) if p.endswith(".py")]
    paths = digest_paths(files, inventory.test_files, inventory.excluded, tracked_py)
    blobs = read_blobs(checkout, sha, paths, deadline)
    established["content_sha256"] = content_sha256(
        criteria.roots, env.lock_sha256, env.groups, env.extras, blobs
    )

    chosen = select(inventory.items, request.beh_ids, blobs)
    # Once, before any run (R-B11); the product's Python decides the error kind (R-B16).
    body_lines = product_body_lines(files, blobs, product_version=env.version)
    measured = {p: blobs[p] for p in (*files, *inventory.test_files)}
    distribution = distribution_args(inventory.xdist_active)
    # R-B15: a run passes a node id where collection passed nothing, so pytest could
    # resolve another config (tests/pytest.ini) and root — every run reuses collection's.
    inipath = None if inventory.inipath is None else str(checkout / inventory.inipath)

    def run(node_id: str) -> Document:
        deadline.check()  # an exhausted budget between runs is TIMEOUT, never a partial beh
        return run_selector(
            env, checkout, sha, probe_dir, work, node_id, files, measured, body_lines,
            deadline, selector_timeout, distribution=distribution,
            rootpath=inventory.rootpath, inipath=inipath,
        )  # fmt: skip

    selectors = _run_selectors(chosen, inventory.items, run)
    return [_beh(beh_id, [selectors[i.node_id] for i in chosen[beh_id]]) for beh_id in chosen]


def _run_selectors(
    chosen: Mapping[str, list[TestItem]],
    items: Sequence[TestItem],
    run: Callable[[str], Document],
) -> dict[str, Document]:
    """One selector document per distinct node id, each run twice, in inventory order.

    A node id shared by several BEHs is run once (two runs) and listed under each.
    """
    wanted = {item.node_id for selected in chosen.values() for item in selected}
    documents: dict[str, Document] = {}
    for item in items:
        if item.node_id in wanted:
            documents[item.node_id] = _selector(item, [run(item.node_id), run(item.node_id)])
    return documents


def _selector(item: TestItem, runs: list[Document]) -> Document:
    status, reason = selector_status(runs)
    document: Document = {"node_id": item.node_id, "definition": _definition(item)}
    return {**document, **_status(status, reason), "runs": runs}


def _beh(beh_id: str, selectors: list[Document]) -> Document:
    statuses = [(str(s["status"]), _reason(s)) for s in selectors]
    status, reason = beh_status(statuses)
    return {"id": beh_id, **_status(status, reason), "selectors": selectors}


def _reason(document: Document) -> str | None:
    reason = document.get("reason")
    return None if reason is None else str(reason)


def _status(status: str, reason: str | None) -> Document:
    return {"status": status} if reason is None else {"status": status, "reason": reason}


def _environment(env: Environment, plugins: Sequence[str]) -> Document:
    return {
        "lock_sha256": env.lock_sha256,
        "python": env.label,
        "pytest_plugins": list(plugins),
        "groups": None if env.groups is None else list(env.groups),
        "extras": list(env.extras),
    }


def _definition(item: TestItem) -> Document:
    return {"file": item.file, "qualname": item.qualname, "line": item.line}


def _test_item(item: TestItem) -> Document:
    return {"node_id": item.node_id, "definition": _definition(item)}


def _excluded(entry: Excluded) -> Document:
    """R-B5: skipped → {how, path, reason}; ignored → {how, path}; deselected → node + definition."""
    if entry.how == "skipped":
        return {"how": "skipped", "path": entry.path, "reason": entry.reason}
    if entry.how == "ignored":
        return {"how": "ignored", "path": entry.path}
    definition = entry.definition
    return {
        "how": "deselected",
        "node_id": entry.node_id,
        "definition": None
        if definition is None
        else {"file": definition[0], "qualname": definition[1], "line": definition[2]},
    }
