"""probe/1: the orchestrator <-> pytest-probe interface (#603).

The constants live twice by design: here and literally in ``criteria_probe.py``
(which runs inside the product and cannot import spec-runner); a test parses
the probe's source and asserts they agree. Stdlib only — spec-runner never
imports pytest.

A validator returns the manifest itself only when every rule holds, else
``None``: no partial acceptance, no defaults filled in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROTOCOL = 1
PROBE_MODULE = "_spec_runner_criteria_probe"
PARENT_ENV = "SPEC_RUNNER_PROBE_PARENT"
MODE_ENV = "SPEC_RUNNER_PROBE_MODE"
MANIFEST_ENV = "SPEC_RUNNER_PROBE_MANIFEST"
PRODUCT_FILES_ENV = "SPEC_RUNNER_PROBE_PRODUCT_FILES"

_SETUP_TEARDOWN = frozenset({"passed", "failed", "skipped"})
_CALL = frozenset({"passed", "failed", "skipped", "not-reached"})
_PHASE_KEYS = frozenset({"setup", "call", "teardown"})


def read_manifest(path: Path) -> object | None:
    """Parse a manifest file as JSON; ``None`` on any failure, never raises."""
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _is_abs(value: object) -> bool:
    return isinstance(value, str) and os.path.isabs(value)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_abs_list(value: object) -> bool:
    return isinstance(value, list) and all(_is_abs(v) for v in value)


def _keys(data: object, required: set[str], optional: frozenset[str] = frozenset()) -> bool:
    if not isinstance(data, dict):
        return False
    keys = set(data)
    return required <= keys and keys <= required | optional


def _header_ok(data: object, mode: str, required: set[str], child_pid: int, rc: int) -> bool:
    required = required | {"probe", "mode", "pid", "exitstatus", "complete"}
    optional = frozenset({"monitoring_error"}) if mode == "run" else frozenset()
    if not _keys(data, required, optional):
        return False
    assert isinstance(data, dict)
    return (
        _is_int(data["probe"])
        and data["probe"] == PROTOCOL
        and data["mode"] == mode
        and _is_int(data["pid"])
        and data["pid"] == child_pid
        and data["complete"] is True
        and _is_int(data["exitstatus"])
        and data["exitstatus"] == rc
    )


def _definition_ok(value: object) -> bool:
    return (
        _keys(value, {"file", "qualname", "line"})
        and isinstance(value, dict)
        and _is_abs(value["file"])
        and _is_nonempty_str(value["qualname"])
        and _is_int(value["line"])
        and value["line"] >= 1
    )


def _python_ok(value: object) -> bool:
    return (
        _keys(value, {"implementation", "version"})
        and isinstance(value, dict)
        and _is_str(value["implementation"])
        and _is_str(value["version"])
    )


def _item_ok(item: object) -> bool:
    if not _keys(item, {"node_id", "function", "module", "definition"}):
        return False
    assert isinstance(item, dict)
    if not (
        _is_nonempty_str(item["node_id"])
        and isinstance(item["function"], bool)
        and _is_abs(item["module"])
    ):
        return False
    definition = item["definition"]
    if definition is None:
        return True
    return item["function"] and _definition_ok(definition)


def _error_ok(error: object) -> bool:
    return (
        _keys(error, {"node_id", "message"})
        and isinstance(error, dict)
        and _is_str(error["node_id"])
        and _is_str(error["message"])
    )


def _excluded_ok(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    how = entry.get("how")
    if how == "skipped":
        return _keys(entry, {"how", "path", "reason"}) and (
            _is_abs(entry["path"]) and _is_str(entry["reason"])
        )
    if how == "ignored":
        return _keys(entry, {"how", "path"}) and _is_abs(entry["path"])
    if how == "deselected":
        return (
            _keys(entry, {"how", "node_id", "definition"})
            and _is_nonempty_str(entry["node_id"])
            and (entry["definition"] is None or _definition_ok(entry["definition"]))
        )
    return False


def _list_of(value: object, check: Any) -> bool:
    return isinstance(value, list) and all(check(v) for v in value)


def _collect_shape_ok(data: dict[str, Any]) -> bool:
    return (
        _python_ok(data["python"])
        and _is_abs(data["rootpath"])
        and (data["inipath"] is None or _is_abs(data["inipath"]))
        and _is_str_list(data["plugins"])
        and _is_abs_list(data["conftests"])
        and _list_of(data["items"], _item_ok)
        and _list_of(data["errors"], _error_ok)
        and _list_of(data["excluded"], _excluded_ok)
    )


def _collect_consistent(data: dict[str, Any]) -> bool:
    status = data["exitstatus"]
    if data["errors"]:
        return status in (1, 2)
    return status in (0, 5) and (not data["items"]) == (status == 5)


_COLLECT_KEYS = {
    "python",
    "rootpath",
    "inipath",
    "plugins",
    "conftests",
    "items",
    "errors",
    "excluded",
}


def valid_collect(data: object, *, child_pid: int, returncode: int) -> dict[str, Any] | None:
    """Return the collect manifest iff every probe/1 rule holds, else ``None``."""
    if not _header_ok(data, "collect", _COLLECT_KEYS, child_pid, returncode):
        return None
    assert isinstance(data, dict)
    if not _collect_shape_ok(data) or not _collect_consistent(data):
        return None
    return data


def _phases_shape_ok(phases: object) -> bool:
    if not isinstance(phases, dict):
        return False
    if not phases:
        return True
    if set(phases) != _PHASE_KEYS:
        return False
    return (
        phases["setup"] in _SETUP_TEARDOWN
        and phases["call"] in _CALL
        and phases["teardown"] in _SETUP_TEARDOWN
    )


def _product_lines_ok(value: object) -> bool:
    return isinstance(value, dict) and all(
        _is_abs(path) and _list_of(lines, lambda n: _is_int(n) and n >= 1)
        for path, lines in value.items()
    )


def _run_shape_ok(data: dict[str, Any]) -> bool:
    return (
        _is_str_list(data["collected"])
        and _phases_shape_ok(data["phases"])
        and isinstance(data["call_in_owner"], bool)
        and isinstance(data["distributed"], bool)
        and _product_lines_ok(data["product_lines"])
        and _is_str_list(data["process_operations"])
        and _is_str(data.get("monitoring_error", ""))
    )


def _phases_consistent(phases: dict[str, str]) -> bool:
    return (phases["call"] == "not-reached") == (phases["setup"] != "passed")


def _run_consistent(data: dict[str, Any]) -> bool:
    phases, status = data["phases"], data["exitstatus"]
    if not data["collected"]:
        return not phases and status in (4, 5)
    if not phases or not _phases_consistent(phases):
        return False
    failed = "failed" in phases.values()
    return bool(status == (1 if failed else 0))


_RUN_KEYS = {
    "collected",
    "phases",
    "call_in_owner",
    "distributed",
    "product_lines",
    "process_operations",
}


def valid_run(data: object, *, child_pid: int, returncode: int) -> dict[str, Any] | None:
    """Return the run manifest iff every probe/1 rule holds, else ``None``."""
    if not _header_ok(data, "run", _RUN_KEYS, child_pid, returncode):
        return None
    assert isinstance(data, dict)
    if not _run_shape_ok(data) or not _run_consistent(data):
        return None
    return data
