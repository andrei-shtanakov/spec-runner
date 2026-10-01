"""spec-runner criteria-closure probe, protocol probe/1 (#603 design §3.5-3.6).

Loaded into the PRODUCT's pytest with `-p _spec_runner_criteria_probe` from a
temp directory on PYTHONPATH; written there as text by
`criteria_inventory.deploy_probe`. spec-runner itself never imports this module
(it does not need pytest at runtime). Standard library and pytest only — the
product's environment need not have spec-runner installed.

The interface — invocation, environment variables, ownership, manifest — is
probe/1, whose orchestrator side is `criteria_protocol.py`. The constants below
are literal copies of that module's; a test parses this source and asserts they
agree. Collect mode records the inventory; run mode records what a selector's call
phase executed (lines in declared product files, process operations).
"""

from __future__ import annotations

import inspect
import json
import os
import platform
import sys
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
    "xdist_active": False,
    "rerunfailures_active": False,
    "conftests": [],
    "items": [],
    "errors": [],
    "excluded": [],
}
_ignored: set[str] = set()
_collected: list[Any] = []

_AUDITED = frozenset(
    {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system", "os.fork", "os.forkpty"}
)
_RUN: dict[str, Any] = {
    "collected": [],
    "phases": {},
    "call_ran": set(),
    "call_reports": [],
    "lines": {},
    "ops": [],
}
_tracing = False
_tool: int | None = None
_real_cache: dict[str, str] = {}


def _owner() -> bool:
    """Record only in the designated process, and never in a fork of it."""
    return OWNER_PID is not None and os.getpid() == OWNER_PID


_monitoring_error: str | None = None


def _product_files() -> frozenset[str]:
    """The declared product files; a missing or unreadable list is reported, not guessed."""
    global _monitoring_error
    path = os.environ.get(PRODUCT_FILES_ENV)
    if not path:
        _monitoring_error = "product files not given"
        return frozenset()
    try:
        with open(path, encoding="utf-8") as handle:
            return frozenset(os.path.realpath(p) for p in json.load(handle))
    except (OSError, ValueError, TypeError):
        _monitoring_error = "product files unreadable"
        return frozenset()


def _real(path: str) -> str:
    cached = _real_cache.get(path)
    if cached is None:
        cached = _real_cache[path] = os.path.realpath(path)
    return cached


_PRODUCT: frozenset[str] = (
    _product_files() if MODE == "run" and OWNER_PID is not None else frozenset()
)


def _on_line(code: Any, line: int) -> Any:
    if _tracing and _owner() and code.co_flags & inspect.CO_OPTIMIZED:
        path = _real(code.co_filename)
        if path in _PRODUCT:
            _RUN["lines"].setdefault(path, set()).add(line)
    if sys.version_info >= (3, 12):
        return sys.monitoring.DISABLE
    return None


def _audit(event: str, args: Any) -> None:
    if _tracing and _owner() and event in _AUDITED:
        _RUN["ops"].append(event)


if MODE == "run" and OWNER_PID is not None:
    sys.addaudithook(_audit)
    if sys.version_info >= (3, 12):
        for _candidate in (3, 4, sys.monitoring.COVERAGE_ID, sys.monitoring.PROFILER_ID):
            if sys.monitoring.get_tool(_candidate) is None:
                sys.monitoring.use_tool_id(_candidate, "spec-runner-criteria")
                sys.monitoring.register_callback(_candidate, sys.monitoring.events.LINE, _on_line)
                _tool = _candidate
                break
        else:
            _monitoring_error = _monitoring_error or "no free sys.monitoring tool id"
    else:
        _monitoring_error = _monitoring_error or "sys.monitoring needs CPython >= 3.12"


def _set_tracing(on: bool) -> None:
    global _tracing
    _tracing = on
    if _tool is not None and sys.version_info >= (3, 12):
        sys.monitoring.set_events(_tool, sys.monitoring.events.LINE if on else 0)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: Any) -> Any:
    owner = _owner() and MODE == "run"
    if owner:
        _RUN["call_ran"].add(item.nodeid)
        _set_tracing(True)
    try:
        yield
    finally:
        if owner:
            _set_tracing(False)


RERUN_ERROR = "test was rerun (pytest-rerunfailures); a retried attempt cannot be measured"


def pytest_runtest_logreport(report: Any) -> None:
    global _monitoring_error
    if not (_owner() and MODE == "run"):
        return
    if report.outcome == "rerun":  # measured, pytest-rerunfailures 16.7: setup or call retried
        _monitoring_error = _monitoring_error or RERUN_ERROR
        return
    # last write wins: exact for one item; the orchestrator checks collected == [node_id]
    _RUN["phases"][report.when] = report.outcome
    if report.when == "call":
        _RUN["call_reports"].append(report.nodeid)


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
    # the registered plugin, not the installed distribution: `-p no:xdist` leaves the
    # distribution listed (looponfail still registers) but removes `-n`
    _manifest["xdist_active"] = bool(config.pluginmanager.has_plugin("xdist"))
    # registered as "rerunfailures" (measured on 16.7); its `flaky` marker reruns even
    # under `--reruns 0`, so run mode also refuses any rerun it observes
    _manifest["rerunfailures_active"] = bool(config.pluginmanager.has_plugin("rerunfailures"))
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
    # A skip below module level (a class skipped at collection) names its module's path.
    rootpath = _manifest["rootpath"] or os.getcwd()
    return os.path.abspath(os.path.join(rootpath, node_id.split("::", 1)[0]))


def _reason(report: Any) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)[-_MESSAGE_LIMIT:]


def pytest_itemcollected(item: Any) -> None:
    if _owner():
        _collected.append(item)


def pytest_collection_finish(session: Any) -> None:
    if not _owner():
        return
    if MODE == "run":
        _RUN["collected"] = [item.nodeid for item in session.items]
        return
    _record_items(session)
    _record_deselected(session)


def _record_deselected(session: Any) -> None:
    """Every collected item missing from the final `session.items` (design §3.5).

    Compared at `collection_finish`, after every `modifyitems` implementation and
    wrapper, whether or not any of them called `pytest_deselected`.
    """
    kept = {item.nodeid for item in session.items}
    for item in _collected:
        if item.nodeid not in kept:
            _manifest["excluded"].append(
                {"how": "deselected", "node_id": item.nodeid, "definition": _function_def(item)}
            )


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
        source = inspect.getsourcefile(function)
        if source is None:  # exec/compile-made code: no file holds its source
            return None
        return {
            "file": os.path.realpath(source),
            "qualname": function.__qualname__,
            "line": code.co_firstlineno,
        }
    except Exception:  # noqa: BLE001 — an unresolvable definition is reported as null
        return None


def _run_manifest(exitstatus: int) -> dict[str, Any]:
    """The probe/1 run manifest: exactly the keys `valid_run` admits."""
    phases = dict(_RUN["phases"])
    if phases.get("setup") in {"failed", "skipped"}:
        phases["call"] = "not-reached"
    reports = _RUN["call_reports"]
    in_owner = [n in _RUN["call_ran"] for n in reports]
    manifest: dict[str, Any] = {
        "probe": PROTOCOL,
        "mode": "run",
        "pid": os.getpid(),
        "collected": _RUN["collected"],
        "phases": phases,
        "call_in_owner": bool(reports) and all(in_owner),
        "distributed": not all(in_owner),
        "product_lines": {p: sorted(ls) for p, ls in _RUN["lines"].items()},
        "process_operations": list(_RUN["ops"]),
        "exitstatus": int(exitstatus),
        "complete": True,
    }
    if _monitoring_error is not None:
        manifest["monitoring_error"] = _monitoring_error
    return manifest


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not _owner() or MODE not in {"collect", "run"}:
        return
    path = os.environ.get(MANIFEST_ENV)
    if not path:
        return
    if MODE == "run":
        manifest = _run_manifest(exitstatus)
    else:
        _manifest["excluded"].extend({"how": "ignored", "path": p} for p in sorted(_ignored))
        _manifest["exitstatus"] = int(exitstatus)
        _manifest["complete"] = True
        manifest = _manifest
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    os.replace(tmp, path)
