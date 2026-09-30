"""spec-runner criteria-closure probe, protocol probe/1 (#603 design §3.5-3.6).

Loaded into the PRODUCT's pytest with `-p _spec_runner_criteria_probe` from a
temp directory on PYTHONPATH; written there as text by
`criteria_inventory.deploy_probe`. spec-runner itself never imports this module
(it does not need pytest at runtime). Standard library and pytest only — the
product's environment need not have spec-runner installed.

The interface — invocation, environment variables, ownership, manifest — is
probe/1, whose orchestrator side is `criteria_protocol.py`. The constants below
are literal copies of that module's; a test parses this source and asserts they
agree. Collect mode only here; run mode arrives with B2b.
"""

from __future__ import annotations

import inspect
import json
import os
import platform
from typing import Any

import pytest

PROTOCOL = 1
PROBE_MODULE = "_spec_runner_criteria_probe"
PARENT_ENV = "SPEC_RUNNER_PROBE_PARENT"
MODE_ENV = "SPEC_RUNNER_PROBE_MODE"
MANIFEST_ENV = "SPEC_RUNNER_PROBE_MANIFEST"
PRODUCT_FILES_ENV = "SPEC_RUNNER_PROBE_PRODUCT_FILES"

_MESSAGE_LIMIT = 2000

#: Owner iff the orchestrator is our direct parent; fixed at import (design §3.6).
OWNER_PID: int | None = os.getpid() if str(os.getppid()) == os.environ.get(PARENT_ENV) else None
MODE = os.environ.get(MODE_ENV, "")

_manifest: dict[str, Any] = {
    "probe": PROTOCOL,
    "mode": MODE,
    "pid": os.getpid(),
    "python": {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    },
    "rootpath": None,
    "inipath": None,
    "plugins": [],
    "conftests": [],
    "items": [],
    "errors": [],
    "excluded": [],
}
_ignored: set[str] = set()


def _owner() -> bool:
    """Record only in the designated process, and never in a fork of it."""
    return OWNER_PID is not None and os.getpid() == OWNER_PID


def pytest_configure(config: Any) -> None:
    if not _owner():
        return
    inipath = getattr(config, "inipath", None)
    _manifest["inipath"] = os.path.realpath(str(inipath)) if inipath else None
    _manifest["rootpath"] = os.path.realpath(str(config.rootpath))
    distributions = {
        f"{dist.project_name}-{dist.version}"
        for _, dist in config.pluginmanager.list_plugin_distinfo()
    }
    _manifest["plugins"] = sorted(distributions | {f"pytest-{pytest.__version__}"})


def pytest_plugin_registered(plugin: Any, manager: Any) -> None:
    if not _owner():
        return
    path = getattr(plugin, "__file__", None) or ""
    if os.path.basename(path) != "conftest.py":
        return
    real = os.path.realpath(path)
    if real not in _manifest["conftests"]:
        _manifest["conftests"].append(real)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_ignore_collect(collection_path: Any, config: Any) -> Any:
    """Every path some implementation refused: collect_ignore(_glob), --ignore, norecursedirs."""
    ignored = yield
    if ignored and _owner():
        _ignored.add(os.path.abspath(str(collection_path)))
    return ignored


def pytest_collectreport(report: Any) -> None:
    if not _owner():
        return
    if report.failed:
        message = str(report.longrepr)[-_MESSAGE_LIMIT:]
        _manifest["errors"].append({"node_id": report.nodeid, "message": message})
    elif report.skipped and report.nodeid:
        _manifest["excluded"].append(
            {"how": "skipped", "path": _node_path(report.nodeid), "reason": _reason(report)}
        )


def _node_path(node_id: str) -> str:
    rootpath = _manifest["rootpath"] or os.getcwd()
    return os.path.abspath(os.path.join(rootpath, node_id.split("::", 1)[0]))


def _reason(report: Any) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)[-_MESSAGE_LIMIT:]


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(session: Any, config: Any, items: list[Any]) -> Any:
    """Every item removed here, whether or not `pytest_deselected` was called."""
    before = list(items)
    result = yield
    if _owner():
        kept = {item.nodeid for item in items}
        for item in before:
            if item.nodeid not in kept:
                _manifest["excluded"].append(
                    {"how": "deselected", "node_id": item.nodeid, "definition": _function_def(item)}
                )
    return result


def pytest_collection_finish(session: Any) -> None:
    if not _owner():
        return
    _record_items(session)


def _record_items(session: Any) -> None:
    for item in session.items:
        _manifest["items"].append(
            {
                "node_id": item.nodeid,
                "function": isinstance(item, pytest.Function),
                "module": os.path.realpath(str(item.path)),
                "definition": _function_def(item),
            }
        )


def _function_def(item: Any) -> dict[str, Any] | None:
    """The unwrapped function's source file, qualname and first line; None if not one."""
    if not isinstance(item, pytest.Function):
        return None
    try:
        function = inspect.unwrap(item.function)
        code = function.__code__
        source = inspect.getsourcefile(function) or code.co_filename
        return {
            "file": os.path.realpath(source),
            "qualname": function.__qualname__,
            "line": code.co_firstlineno,
        }
    except Exception:  # noqa: BLE001 — an unresolvable definition is reported as null
        return None


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not _owner() or MODE != "collect":
        return
    path = os.environ.get(MANIFEST_ENV)
    if not path:
        return
    _manifest["excluded"].extend({"how": "ignored", "path": p} for p in sorted(_ignored))
    _manifest["exitstatus"] = int(exitstatus)
    _manifest["complete"] = True
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(_manifest, handle)
    os.replace(tmp, path)
