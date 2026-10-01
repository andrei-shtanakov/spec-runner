"""#603 B2b: the probe's run mode (probe/1) — lines in call only, owner-only writes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from spec_runner.criteria_inventory import deploy_probe
from spec_runner.criteria_process import Deadline, run_bounded
from spec_runner.criteria_protocol import (
    MANIFEST_ENV,
    MODE_ENV,
    PARENT_ENV,
    PROBE_MODULE,
    PRODUCT_FILES_ENV,
    valid_run,
)
from spec_runner.criteria_workspace import child_env

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="sys.monitoring needs CPython >= 3.12"
)


def _run(
    tmp_path: Path, files: dict[str, str], node_id: str, product: list[str], extra: list[str] = ()
) -> dict:
    checkout = tmp_path / "checkout"
    for rel, text in files.items():
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    probe = deploy_probe(tmp_path / "probe")
    manifest = tmp_path / "run.json"
    product_json = tmp_path / "product.json"
    product_json.write_text(json.dumps([str((checkout / p).resolve()) for p in product]))
    env = child_env(
        probe,
        {
            PARENT_ENV: str(os.getpid()),
            MODE_ENV: "run",
            MANIFEST_ENV: str(manifest),
            PRODUCT_FILES_ENV: str(product_json),
        },
    )
    finished = run_bounded(
        [sys.executable, "-P", "-m", "pytest", "-p", PROBE_MODULE, *extra, "-q", node_id],
        cwd=checkout,
        env=env,
        deadline=Deadline(120),
    )
    assert finished.timed_out is None and finished.returncode is not None
    result = json.loads(manifest.read_text())
    assert valid_run(result, child_pid=finished.pid, returncode=finished.returncode) is not None
    return result


PRODUCT = {
    "pkg/__init__.py": "",
    "pkg/mod.py": "CONST = 1\n\n\ndef work(x):\n    y = x + 1\n    return y\n",
}


class TestLinesInCallOnly:
    def test_missing_selector_has_no_phases(self, tmp_path):
        files = {"tests/test_a.py": "def test_a():\n    pass\n"}
        m = _run(tmp_path, files, "tests/test_a.py::test_missing", [])
        assert m["collected"] == [] and m["phases"] == {}
        assert m["exitstatus"] == 4
        assert m["call_in_owner"] is False and m["distributed"] is False

    def test_call_lines_recorded_setup_lines_not(self, tmp_path):
        files = {
            **PRODUCT,
            "tests/test_a.py": (
                "import pytest\nfrom pkg.mod import work\n\n\n"
                "@pytest.fixture\ndef warm():\n    work(0)\n\n\n"
                "def test_a(warm):\n    assert work(1) == 2\n"
            ),
            "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
        }
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert m["complete"] and m["mode"] == "run"
        assert m["collected"] == ["tests/test_a.py::test_a"]
        assert m["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
        lines = m["product_lines"][str((tmp_path / "checkout" / "pkg/mod.py").resolve())]
        assert lines == [5, 6]  # the body of work(); module-level CONST = 1 never counts
        assert m["call_in_owner"] is True and m["distributed"] is False

    def test_setup_failure_is_not_reached(self, tmp_path):
        files = {
            **PRODUCT,
            "tests/test_a.py": (
                "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError\n\n\n"
                "def test_a(broken):\n    pass\n"
            ),
        }
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert m["phases"]["setup"] == "failed" and m["phases"]["call"] == "not-reached"

    def test_process_operations_recorded_in_call(self, tmp_path):
        files = {
            **PRODUCT,
            "tests/test_a.py": (
                "import subprocess, sys\n\n\ndef test_a():\n"
                "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
            ),
        }
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert "subprocess.Popen" in m["process_operations"]
        assert m["product_lines"] == {}


class TestOwnership:  # Review Focus 2
    def test_fork_child_never_writes(self, tmp_path):
        files = {
            **PRODUCT,
            "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
            "tests/test_a.py": (
                "import os\nfrom pkg.mod import work\n\n\ndef test_a():\n"
                "    pid = os.fork()\n    if pid == 0:\n        work(5)\n        os._exit(0)\n"
                "    os.waitpid(pid, 0)\n"
            ),
        }
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert m["pid"] != 0 and "os.fork" in m["process_operations"]
        assert m["product_lines"] == {}  # the child ran work(); its lines are not observed

    def test_forked_distribution_is_detected(self, tmp_path):
        pytest.importorskip("pytest_forked")
        files = {**PRODUCT, "tests/test_a.py": "def test_a():\n    pass\n"}
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"], ["--forked"])
        assert m["distributed"] is True and m["call_in_owner"] is False
