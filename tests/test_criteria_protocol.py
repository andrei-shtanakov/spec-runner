"""probe/1 constants and strict manifest validation (#603, B2a Task 6)."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from spec_runner import criteria_protocol as proto

PID = 4242
CONSTANT_NAMES = (
    "PROTOCOL",
    "PROBE_MODULE",
    "PARENT_ENV",
    "MODE_ENV",
    "MANIFEST_ENV",
    "PRODUCT_FILES_ENV",
)
PROBE_SOURCE = Path(proto.__file__).with_name("criteria_probe.py")


def collect_manifest() -> dict[str, Any]:
    return {
        "probe": 1,
        "mode": "collect",
        "pid": PID,
        "python": {"implementation": "CPython", "version": "3.12.13"},
        "rootpath": "/abs/checkout",
        "inipath": "/abs/checkout/pyproject.toml",
        "plugins": ["pytest-9.0.2"],
        "conftests": ["/abs/checkout/tests/conftest.py"],
        "items": [
            {
                "node_id": "tests/test_a.py::test_x[1]",
                "function": True,
                "module": "/abs/checkout/tests/test_a.py",
                "definition": {
                    "file": "/abs/checkout/tests/test_a.py",
                    "qualname": "test_x",
                    "line": 8,
                },
            }
        ],
        "errors": [],
        "excluded": [],
        "exitstatus": 0,
        "complete": True,
    }


def run_manifest() -> dict[str, Any]:
    return {
        "probe": 1,
        "mode": "run",
        "pid": PID,
        "collected": ["tests/test_a.py::test_x[1]"],
        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        "call_in_owner": True,
        "distributed": False,
        "product_lines": {"/abs/checkout/pkg/mod.py": [12, 13]},
        "process_operations": ["subprocess.Popen"],
        "exitstatus": 0,
        "complete": True,
    }


def collect_ok(data: object, rc: int = 0) -> dict[str, Any] | None:
    return proto.valid_collect(data, child_pid=PID, returncode=rc)


def run_ok(data: object, rc: int = 0) -> dict[str, Any] | None:
    return proto.valid_run(data, child_pid=PID, returncode=rc)


def mutated(base: dict[str, Any], edit: Any) -> dict[str, Any]:
    data = copy.deepcopy(base)
    edit(data)
    return data


def set_key(key: str, value: object) -> Any:
    def edit(d: dict[str, Any]) -> None:
        d[key] = value

    return edit


def drop_key(key: str) -> Any:
    def edit(d: dict[str, Any]) -> None:
        del d[key]

    return edit


def set_item(key: str, value: object) -> Any:
    def edit(d: dict[str, Any]) -> None:
        d["items"][0][key] = value

    return edit


def set_phase(**phases: str) -> Any:
    def edit(d: dict[str, Any]) -> None:
        d["phases"].update(phases)

    return edit


class TestConstants:
    def test_values(self) -> None:
        assert proto.PROTOCOL == 1
        assert proto.PROBE_MODULE == "_spec_runner_criteria_probe"
        assert proto.PARENT_ENV == "SPEC_RUNNER_PROBE_PARENT"
        assert proto.MODE_ENV == "SPEC_RUNNER_PROBE_MODE"
        assert proto.MANIFEST_ENV == "SPEC_RUNNER_PROBE_MANIFEST"
        assert proto.PRODUCT_FILES_ENV == "SPEC_RUNNER_PROBE_PRODUCT_FILES"

    def test_probe_source_agrees(self) -> None:
        tree = ast.parse(PROBE_SOURCE.read_text(encoding="utf-8"))
        found: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target = node.target
            else:
                continue
            if isinstance(target, ast.Name) and target.id in CONSTANT_NAMES:
                found[target.id] = ast.literal_eval(node.value)  # type: ignore[arg-type]
        assert set(found) == set(CONSTANT_NAMES)
        for name in CONSTANT_NAMES:
            assert found[name] == getattr(proto, name), name

    def test_no_pytest_import(self) -> None:
        tree = ast.parse(Path(proto.__file__).read_text(encoding="utf-8"))
        names = [
            a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
            for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])
        ]
        assert "pytest" not in names


class TestReadManifest:
    def test_reads_json(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"a": 1}))
        assert proto.read_manifest(p) == {"a": 1}

    def test_missing_invalid_and_directory_are_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{nope")
        binary = tmp_path / "bin.json"
        binary.write_bytes(b"\xff\xfe\x00")
        assert proto.read_manifest(tmp_path / "absent.json") is None
        assert proto.read_manifest(bad) is None
        assert proto.read_manifest(binary) is None
        assert proto.read_manifest(tmp_path) is None


def excluded_edit(entry: object) -> Any:
    return set_key("excluded", [entry])


COLLECT_ACCEPTED = {
    "valid": collect_manifest(),
    "inipath null": mutated(collect_manifest(), set_key("inipath", None)),
    "no plugins/conftests": mutated(
        mutated(collect_manifest(), set_key("plugins", [])), set_key("conftests", [])
    ),
    "non-function item": mutated(
        collect_manifest(),
        lambda d: d["items"][0].update({"function": False, "definition": None}),
    ),
    "function unwrap failed": mutated(collect_manifest(), set_item("definition", None)),
    "one excluded of each form": mutated(
        collect_manifest(),
        set_key(
            "excluded",
            [
                {"how": "skipped", "path": "/abs/a.py", "reason": "why"},
                {"how": "ignored", "path": "/abs/b.py"},
                {
                    "how": "deselected",
                    "node_id": "a.py::t",
                    "definition": {"file": "/abs/a.py", "qualname": "t", "line": 1},
                },
                {"how": "deselected", "node_id": "a.py::u", "definition": None},
            ],
        ),
    ),
}


class TestValidCollectAccepts:
    @pytest.mark.parametrize("name", list(COLLECT_ACCEPTED))
    def test_accepts(self, name: str) -> None:
        data = COLLECT_ACCEPTED[name]
        assert collect_ok(data) is data

    def test_no_tests_exit_5(self) -> None:
        data = mutated(collect_manifest(), set_key("items", []))
        data["exitstatus"] = 5
        assert collect_ok(data, rc=5) is data

    @pytest.mark.parametrize("rc", [1, 2])
    def test_errors_exit_1_or_2(self, rc: int) -> None:
        data = collect_manifest()
        data["errors"] = [{"node_id": "tests/test_b.py", "message": "boom"}]
        data["exitstatus"] = rc
        assert collect_ok(data, rc=rc) is data


def _errors(d: dict[str, Any]) -> None:
    d["errors"] = [{"node_id": "x", "message": "m"}]


COLLECT_REJECTED: dict[str, tuple[Any, int]] = {
    "not a dict": ([], 0),
    "probe 2": (mutated(collect_manifest(), set_key("probe", 2)), 0),
    "probe True": (mutated(collect_manifest(), set_key("probe", True)), 0),
    "wrong mode": (mutated(collect_manifest(), set_key("mode", "run")), 0),
    "pid off by one": (mutated(collect_manifest(), set_key("pid", PID + 1)), 0),
    "complete false": (mutated(collect_manifest(), set_key("complete", False)), 0),
    "complete 1": (mutated(collect_manifest(), set_key("complete", 1)), 0),
    "exitstatus != rc": (collect_manifest(), 1),
    "exitstatus bool": (mutated(collect_manifest(), set_key("exitstatus", False)), 0),
    "items empty with exit 0": (mutated(collect_manifest(), set_key("items", [])), 0),
    "items with exit 5": (
        mutated(collect_manifest(), set_key("exitstatus", 5)),
        5,
    ),
    "errors with exit 0": (mutated(collect_manifest(), _errors), 0),
    "errors with exit 5": (
        mutated(mutated(collect_manifest(), _errors), set_key("exitstatus", 5)),
        5,
    ),
    "no errors with exit 1": (mutated(collect_manifest(), set_key("exitstatus", 1)), 1),
    "definition with function false": (
        mutated(collect_manifest(), set_item("function", False)),
        0,
    ),
    "extra key": (mutated(collect_manifest(), set_key("extra", 1)), 0),
    "python not dict": (mutated(collect_manifest(), set_key("python", "3.12")), 0),
    "python missing key": (
        mutated(collect_manifest(), set_key("python", {"implementation": "CPython"})),
        0,
    ),
    "python version int": (
        mutated(
            collect_manifest(),
            set_key("python", {"implementation": "CPython", "version": 3}),
        ),
        0,
    ),
    "rootpath relative": (mutated(collect_manifest(), set_key("rootpath", "rel")), 0),
    "rootpath int": (mutated(collect_manifest(), set_key("rootpath", 1)), 0),
    "inipath relative": (mutated(collect_manifest(), set_key("inipath", "pyproject.toml")), 0),
    "inipath int": (mutated(collect_manifest(), set_key("inipath", 3)), 0),
    "plugins not list": (mutated(collect_manifest(), set_key("plugins", "x")), 0),
    "plugins non-str": (mutated(collect_manifest(), set_key("plugins", [1])), 0),
    "conftests not list": (mutated(collect_manifest(), set_key("conftests", None)), 0),
    "conftest relative": (mutated(collect_manifest(), set_key("conftests", ["c.py"])), 0),
    "items not list": (mutated(collect_manifest(), set_key("items", {"a": 1})), 0),
    "item not dict": (mutated(collect_manifest(), set_key("items", ["x"])), 0),
    "item node_id empty": (mutated(collect_manifest(), set_item("node_id", "")), 0),
    "item node_id int": (mutated(collect_manifest(), set_item("node_id", 1)), 0),
    "item function not bool": (mutated(collect_manifest(), set_item("function", 1)), 0),
    "item module relative": (mutated(collect_manifest(), set_item("module", "a.py")), 0),
    "item extra key": (mutated(collect_manifest(), set_item("extra", 1)), 0),
    "item missing key": (
        mutated(collect_manifest(), lambda d: d["items"][0].pop("module")),
        0,
    ),
    "definition not dict": (mutated(collect_manifest(), set_item("definition", "x")), 0),
    "definition file relative": (
        mutated(
            collect_manifest(),
            set_item("definition", {"file": "a.py", "qualname": "t", "line": 1}),
        ),
        0,
    ),
    "definition qualname empty": (
        mutated(
            collect_manifest(),
            set_item("definition", {"file": "/a.py", "qualname": "", "line": 1}),
        ),
        0,
    ),
    "definition line 0": (
        mutated(
            collect_manifest(),
            set_item("definition", {"file": "/a.py", "qualname": "t", "line": 0}),
        ),
        0,
    ),
    "definition line bool": (
        mutated(
            collect_manifest(),
            set_item("definition", {"file": "/a.py", "qualname": "t", "line": True}),
        ),
        0,
    ),
    "definition extra key": (
        mutated(
            collect_manifest(),
            set_item("definition", {"file": "/a.py", "qualname": "t", "line": 1, "x": 1}),
        ),
        0,
    ),
    "errors not list": (mutated(collect_manifest(), set_key("errors", "x")), 0),
    "error not dict": (
        mutated(
            mutated(collect_manifest(), set_key("errors", ["x"])),
            set_key("exitstatus", 1),
        ),
        1,
    ),
    "error message int": (
        mutated(
            mutated(collect_manifest(), set_key("errors", [{"node_id": "a", "message": 1}])),
            set_key("exitstatus", 1),
        ),
        1,
    ),
    "error extra key": (
        mutated(
            mutated(
                collect_manifest(),
                set_key("errors", [{"node_id": "a", "message": "m", "x": 1}]),
            ),
            set_key("exitstatus", 1),
        ),
        1,
    ),
    "excluded not list": (mutated(collect_manifest(), set_key("excluded", None)), 0),
    "excluded entry not dict": (mutated(collect_manifest(), excluded_edit("x")), 0),
    "excluded wrong how": (
        mutated(collect_manifest(), excluded_edit({"how": "xfail", "path": "/a"})),
        0,
    ),
    "excluded no how": (mutated(collect_manifest(), excluded_edit({"path": "/a"})), 0),
    "skipped no reason": (
        mutated(collect_manifest(), excluded_edit({"how": "skipped", "path": "/a"})),
        0,
    ),
    "skipped reason int": (
        mutated(
            collect_manifest(),
            excluded_edit({"how": "skipped", "path": "/a", "reason": 1}),
        ),
        0,
    ),
    "skipped relative path": (
        mutated(
            collect_manifest(),
            excluded_edit({"how": "skipped", "path": "a.py", "reason": "r"}),
        ),
        0,
    ),
    "skipped extra key": (
        mutated(
            collect_manifest(),
            excluded_edit({"how": "skipped", "path": "/a", "reason": "r", "x": 1}),
        ),
        0,
    ),
    "ignored relative path": (
        mutated(collect_manifest(), excluded_edit({"how": "ignored", "path": "a.py"})),
        0,
    ),
    "ignored path int": (
        mutated(collect_manifest(), excluded_edit({"how": "ignored", "path": 1})),
        0,
    ),
    "ignored with reason": (
        mutated(
            collect_manifest(),
            excluded_edit({"how": "ignored", "path": "/a", "reason": "r"}),
        ),
        0,
    ),
    "ignored missing path": (mutated(collect_manifest(), excluded_edit({"how": "ignored"})), 0),
    "deselected node_id empty": (
        mutated(
            collect_manifest(),
            excluded_edit({"how": "deselected", "node_id": "", "definition": None}),
        ),
        0,
    ),
    "deselected missing definition": (
        mutated(collect_manifest(), excluded_edit({"how": "deselected", "node_id": "a::t"})),
        0,
    ),
    "deselected definition relative": (
        mutated(
            collect_manifest(),
            excluded_edit(
                {
                    "how": "deselected",
                    "node_id": "a::t",
                    "definition": {"file": "a.py", "qualname": "t", "line": 1},
                }
            ),
        ),
        0,
    ),
    "deselected definition line 0": (
        mutated(
            collect_manifest(),
            excluded_edit(
                {
                    "how": "deselected",
                    "node_id": "a::t",
                    "definition": {"file": "/a.py", "qualname": "t", "line": 0},
                }
            ),
        ),
        0,
    ),
    "deselected extra key": (
        mutated(
            collect_manifest(),
            excluded_edit(
                {"how": "deselected", "node_id": "a::t", "definition": None, "path": "/a"}
            ),
        ),
        0,
    ),
}
for _key in (
    "probe",
    "mode",
    "pid",
    "python",
    "rootpath",
    "inipath",
    "plugins",
    "conftests",
    "items",
    "errors",
    "excluded",
    "exitstatus",
    "complete",
):
    COLLECT_REJECTED[f"missing {_key}"] = (mutated(collect_manifest(), drop_key(_key)), 0)


class TestValidCollectRejects:
    def test_pid_bool_is_not_an_int(self) -> None:
        data = mutated(collect_manifest(), set_key("pid", True))
        assert proto.valid_collect(data, child_pid=1, returncode=0) is None
        data["pid"] = 1
        assert proto.valid_collect(data, child_pid=1, returncode=0) is data

    @pytest.mark.parametrize("name", list(COLLECT_REJECTED))
    def test_rejects(self, name: str) -> None:
        data, rc = COLLECT_REJECTED[name]
        assert collect_ok(data, rc) is None

    def test_does_not_mutate_or_fill_defaults(self) -> None:
        data = collect_manifest()
        before = copy.deepcopy(data)
        assert collect_ok(data) is data
        assert data == before

    def test_run_manifest_is_not_a_collect_manifest(self) -> None:
        assert collect_ok(run_manifest()) is None


def not_run(d: dict[str, Any]) -> None:
    d["phases"] = {"setup": "failed", "call": "not-reached", "teardown": "passed"}
    d["exitstatus"] = 1


def absent_selector(d: dict[str, Any]) -> None:
    d["collected"] = []
    d["phases"] = {}
    d["exitstatus"] = 4


RUN_ACCEPTED: dict[str, tuple[dict[str, Any], int]] = {
    "valid": (run_manifest(), 0),
    "teardown skipped": (mutated(run_manifest(), set_phase(teardown="skipped")), 0),
    "call skipped": (mutated(run_manifest(), set_phase(call="skipped")), 0),
    "call failed": (
        mutated(mutated(run_manifest(), set_phase(call="failed")), set_key("exitstatus", 1)),
        1,
    ),
    "setup failed, call not reached": (mutated(run_manifest(), not_run), 1),
    "setup skipped, call not reached": (
        mutated(run_manifest(), set_phase(setup="skipped", call="not-reached")),
        0,
    ),
    "selector absent exit 4": (mutated(run_manifest(), absent_selector), 4),
    "selector absent exit 5": (
        mutated(run_manifest(), lambda d: (absent_selector(d), d.update(exitstatus=5))),
        5,
    ),
    "monitoring_error": (mutated(run_manifest(), set_key("monitoring_error", "why")), 0),
    "distributed": (mutated(run_manifest(), set_key("distributed", True)), 0),
    "no product lines or operations": (
        mutated(
            mutated(run_manifest(), set_key("product_lines", {})),
            set_key("process_operations", []),
        ),
        0,
    ),
}


def failed_teardown(d: dict[str, Any]) -> None:
    d["phases"]["teardown"] = "failed"


RUN_REJECTED: dict[str, tuple[Any, int]] = {
    "not a dict": ("x", 0),
    "probe 2": (mutated(run_manifest(), set_key("probe", 2)), 0),
    "wrong mode": (mutated(run_manifest(), set_key("mode", "collect")), 0),
    "pid off by one": (mutated(run_manifest(), set_key("pid", PID - 1)), 0),
    "complete false": (mutated(run_manifest(), set_key("complete", False)), 0),
    "exitstatus != rc": (run_manifest(), 1),
    "extra key": (mutated(run_manifest(), set_key("extra", 1)), 0),
    "call not-reached with setup passed": (
        mutated(run_manifest(), set_phase(call="not-reached")),
        0,
    ),
    "setup failed with call passed": (
        mutated(mutated(run_manifest(), set_phase(setup="failed")), set_key("exitstatus", 1)),
        1,
    ),
    "collected empty with phases": (
        mutated(run_manifest(), set_key("collected", [])),
        0,
    ),
    "collected empty with phases exit 4": (
        mutated(mutated(run_manifest(), set_key("collected", [])), set_key("exitstatus", 4)),
        4,
    ),
    "collected empty exit 0": (
        mutated(mutated(run_manifest(), set_key("collected", [])), set_key("phases", {})),
        0,
    ),
    "collected present phases empty": (
        mutated(mutated(run_manifest(), set_key("phases", {})), set_key("exitstatus", 4)),
        4,
    ),
    "exit 0 with failed teardown": (mutated(run_manifest(), failed_teardown), 0),
    "exit 1 with no failed phase": (mutated(run_manifest(), set_key("exitstatus", 1)), 1),
    "exit 4 with phases": (mutated(run_manifest(), set_key("exitstatus", 4)), 4),
    "phase unknown value": (mutated(run_manifest(), set_phase(teardown="ok")), 0),
    "teardown not-reached": (mutated(run_manifest(), set_phase(teardown="not-reached")), 0),
    "setup not-reached": (
        mutated(run_manifest(), set_phase(setup="not-reached", call="not-reached")),
        0,
    ),
    "phases extra key": (mutated(run_manifest(), set_phase(extra="passed")), 0),
    "phases missing key": (
        mutated(run_manifest(), lambda d: d["phases"].pop("teardown")),
        0,
    ),
    "phases not dict": (mutated(run_manifest(), set_key("phases", "passed")), 0),
    "collected not list": (mutated(run_manifest(), set_key("collected", "x")), 0),
    "collected non-str": (mutated(run_manifest(), set_key("collected", [1])), 0),
    "call_in_owner not bool": (mutated(run_manifest(), set_key("call_in_owner", 1)), 0),
    "distributed not bool": (mutated(run_manifest(), set_key("distributed", "no")), 0),
    "product_lines not dict": (mutated(run_manifest(), set_key("product_lines", [])), 0),
    "product_lines relative path": (
        mutated(run_manifest(), set_key("product_lines", {"pkg/mod.py": [1]})),
        0,
    ),
    "product_lines line 0": (
        mutated(run_manifest(), set_key("product_lines", {"/a.py": [0]})),
        0,
    ),
    "product_lines line bool": (
        mutated(run_manifest(), set_key("product_lines", {"/a.py": [True]})),
        0,
    ),
    "product_lines value not list": (
        mutated(run_manifest(), set_key("product_lines", {"/a.py": 1})),
        0,
    ),
    "process_operations non-str": (
        mutated(run_manifest(), set_key("process_operations", [1])),
        0,
    ),
    "process_operations not list": (
        mutated(run_manifest(), set_key("process_operations", "x")),
        0,
    ),
    "monitoring_error not str": (mutated(run_manifest(), set_key("monitoring_error", 1)), 0),
}
for _key in (
    "probe",
    "mode",
    "pid",
    "collected",
    "phases",
    "call_in_owner",
    "distributed",
    "product_lines",
    "process_operations",
    "exitstatus",
    "complete",
):
    RUN_REJECTED[f"missing {_key}"] = (mutated(run_manifest(), drop_key(_key)), 0)


class TestValidRun:
    @pytest.mark.parametrize("name", list(RUN_ACCEPTED))
    def test_accepts(self, name: str) -> None:
        data, rc = RUN_ACCEPTED[name]
        assert run_ok(data, rc) is data

    @pytest.mark.parametrize("name", list(RUN_REJECTED))
    def test_rejects(self, name: str) -> None:
        data, rc = RUN_REJECTED[name]
        assert run_ok(data, rc) is None

    def test_collect_manifest_is_not_a_run_manifest(self) -> None:
        assert run_ok(collect_manifest()) is None
