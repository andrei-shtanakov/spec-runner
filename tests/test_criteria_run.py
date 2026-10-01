"""#603 B2b: one isolated, bounded, validated selector run (`criteria_run.run_selector`).

Real runs use the current interpreter as the product environment and a tiny git
repository per test; the fake-manifest tests stand in for the probe to pin the
orchestrator's reading of a manifest that the real probe cannot be made to write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from spec_runner import criteria_process
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import deploy_probe
from spec_runner.criteria_process import Deadline, Finished
from spec_runner.criteria_protocol import MANIFEST_ENV, PROBE_MODULE, PRODUCT_FILES_ENV
from spec_runner.criteria_run import run_selector
from spec_runner.criteria_tokens import function_body_lines
from spec_runner.criteria_workspace import Environment, read_blobs

SCHEMA = Path(__file__).resolve().parent.parent / "schemas/criteria-closure/v1"
NEEDS_312 = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="sys.monitoring needs CPython >= 3.12"
)


def _run_validator() -> Draft7Validator:
    schema = json.loads((SCHEMA / "response.schema.json").read_text())
    return Draft7Validator({**schema, "$ref": "#/definitions/run"})


def _assert_run_shape(run: dict[str, object]) -> None:
    errors = [e.message for e in _run_validator().iter_errors(run)]
    assert errors == [], errors


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    return root.resolve(), _git(root, "rev-parse", "HEAD")


def _env() -> Environment:
    version = ".".join(str(v) for v in sys.version_info[:3])
    return Environment(Path(sys.executable), "CPython", version, "0" * 64, False, None, ())


CONFTEST = "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"
PRODUCT = {
    "conftest.py": CONFTEST,
    "pkg/__init__.py": "",
    "pkg/mod.py": "def work(x):\n    return x + 1\n",
}
PRODUCT_FILES = ("pkg/__init__.py", "pkg/mod.py")


class Case:
    """A committed product, its deployed probe, and the `measured`/`blobs` maps at sha."""

    def __init__(self, tmp_path: Path, tests: dict[str, str]) -> None:
        self.tmp_path = tmp_path
        self.checkout, self.sha = _repo(tmp_path / "checkout", {**PRODUCT, **tests})
        self.probe_dir = deploy_probe(tmp_path / "probe")
        self.work = tmp_path / "work"
        measured = [*PRODUCT_FILES, "conftest.py", *tests]
        self.measured = read_blobs(self.checkout, self.sha, measured, Deadline(60))
        self.blobs = {p: self.measured[p] for p in PRODUCT_FILES}

    def run(
        self,
        node_id: str,
        *,
        deadline: Deadline | None = None,
        selector_timeout: float = 120.0,
        distribution: Sequence[str] = (),
    ) -> dict[str, object]:
        run = run_selector(
            _env(),
            self.checkout,
            self.sha,
            self.probe_dir,
            self.work,
            node_id,
            PRODUCT_FILES,
            self.measured,
            self.blobs,
            deadline or Deadline(300),
            selector_timeout,
            distribution=distribution,
        )
        _assert_run_shape(run)
        assert list(self.work.iterdir()) == []  # the invocation dir is removed
        return run


def _kind(call: Callable[[], object]) -> ErrorKind:
    with pytest.raises(CriteriaError) as raised:
        call()
    return raised.value.kind


# --------------------------------------------------------------------- function bodies


class TestFunctionBodyLines:
    def test_module_constant_and_class_body_excluded(self) -> None:
        source = "X = 1\nclass C:\n    y = 2\n    def m(self):\n        return 3\n"
        assert function_body_lines(source) == {5}

    def test_comprehension_inside_a_function_included(self) -> None:
        source = "def f(xs):\n    return [\n        x for x in xs\n    ]\n"
        assert function_body_lines(source) == {2, 3, 4}

    def test_async_def_body_included(self) -> None:
        assert function_body_lines("async def f():\n    await g()\n    return 1\n") == {2, 3}

    def test_nested_functions_counted(self) -> None:
        source = "def f():\n    def g():\n        return 1\n    return g\n"
        assert function_body_lines(source) == {2, 3, 4}

    def test_bom_tolerated(self) -> None:
        assert function_body_lines("\ufeffdef f():\n    return 1\n") == {2}


# --------------------------------------------------------------------- fake manifests


def _fake_pytest(
    monkeypatch: pytest.MonkeyPatch,
    manifest: Callable[[list[str], dict[str, str]], dict[str, Any] | None],
    returncode: int = 0,
    pid: int = 4242,
) -> list[list[str]]:
    """Stand in for the product's pytest: git still runs; pytest writes `manifest`."""
    seen: list[list[str]] = []
    real = criteria_process.run_bounded

    def fake(argv: Sequence[str], **kwargs: Any) -> Finished:
        if "pytest" not in argv:
            return real(argv, **kwargs)
        seen.append(list(argv))
        env = kwargs["env"]
        assert Path(env[PRODUCT_FILES_ENV]).is_file()
        assert Path(env["TMPDIR"]).is_dir()
        data = manifest(list(argv), env)
        if data is not None:
            Path(env[MANIFEST_ENV]).write_text(json.dumps(data))
        return Finished(returncode, b"", b"pytest said no\n", pid, None)

    monkeypatch.setattr(criteria_process, "run_bounded", fake)
    return seen


def _manifest(node_id: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "probe": 1,
        "mode": "run",
        "pid": 4242,
        "collected": [node_id],
        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        "call_in_owner": True,
        "distributed": False,
        "product_lines": {},
        "process_operations": [],
        "exitstatus": 0,
        "complete": True,
    }
    data.update(overrides)
    return data


NODE = "tests/test_a.py::test_a"
TESTS = {
    "tests/test_a.py": "from pkg.mod import work\n\n\ndef test_a():\n    assert work(1) == 2\n"
}


class TestFakeManifests:
    def test_distribution_flags_reach_the_pytest_argv(self, tmp_path, monkeypatch) -> None:
        seen = _fake_pytest(monkeypatch, lambda argv, env: None)
        case = Case(tmp_path, TESTS)
        run = case.run(NODE, distribution=["-n", "0", "--dist", "no"])
        assert seen == [
            [sys.executable, "-P", "-m", "pytest", "-p", PROBE_MODULE]
            + ["-n", "0", "--dist", "no", "-q", NODE]
        ]
        assert run["result"] == "error" and run["reason"] == "runner"
        assert "no run manifest" in str(run["detail"])

    def test_no_distribution_flags_by_default(self, tmp_path, monkeypatch) -> None:
        seen = _fake_pytest(monkeypatch, lambda argv, env: None)
        Case(tmp_path, TESTS).run(NODE)
        assert seen[0][-4:] == ["-p", PROBE_MODULE, "-q", NODE]

    def test_monitoring_error_is_an_error_run(self, tmp_path, monkeypatch) -> None:
        _fake_pytest(
            monkeypatch, lambda argv, env: _manifest(NODE, monitoring_error="no free tool id")
        )
        run = Case(tmp_path, TESTS).run(NODE)
        assert run == {
            "result": "error",
            "reason": "runner",
            "detail": "no free tool id",
            "exit_status": 0,
        }

    def test_distributed_raises(self, tmp_path, monkeypatch) -> None:
        _fake_pytest(
            monkeypatch,
            lambda argv, env: _manifest(NODE, distributed=True, call_in_owner=False),
        )
        case = Case(tmp_path, TESTS)
        assert _kind(lambda: case.run(NODE)) is ErrorKind.DISTRIBUTED_EXECUTION
        assert list(case.work.iterdir()) == []

    def test_another_collected_node_is_an_error_run(self, tmp_path, monkeypatch) -> None:
        _fake_pytest(monkeypatch, lambda argv, env: _manifest("tests/test_a.py::test_b"))
        run = Case(tmp_path, TESTS).run(NODE)
        assert run["result"] == "error" and "test_b" in str(run["detail"])

    def test_a_manifest_from_another_pid_is_rejected(self, tmp_path, monkeypatch) -> None:
        _fake_pytest(monkeypatch, lambda argv, env: _manifest(NODE, pid=1))
        run = Case(tmp_path, TESTS).run(NODE)
        assert run["result"] == "error" and "invalid run manifest" in str(run["detail"])
        assert run["exit_status"] == 0

    def test_an_undeclared_product_file_is_an_error_run(self, tmp_path, monkeypatch) -> None:
        def manifest(argv: list[str], env: dict[str, str]) -> dict[str, Any]:
            other = str(Path(env["TMPDIR"]).resolve() / "elsewhere.py")
            return _manifest(NODE, product_lines={other: [1]})

        _fake_pytest(monkeypatch, manifest)
        run = Case(tmp_path, TESTS).run(NODE)
        assert run["result"] == "error" and "elsewhere.py" in str(run["detail"])

    def test_product_files_json_lists_absolute_paths(self, tmp_path, monkeypatch) -> None:
        listed: list[list[str]] = []

        def manifest(argv: list[str], env: dict[str, str]) -> None:
            listed.append(json.loads(Path(env[PRODUCT_FILES_ENV]).read_text()))

        _fake_pytest(monkeypatch, manifest)
        case = Case(tmp_path, TESTS)
        case.run(NODE)
        assert listed == [[str(case.checkout / p) for p in PRODUCT_FILES]]

    def test_a_launch_failure_is_an_error_run(self, tmp_path, monkeypatch) -> None:
        _fake_pytest(monkeypatch, lambda argv, env: None, returncode=127, pid=-1)
        run = Case(tmp_path, TESTS).run(NODE)
        assert run["result"] == "error" and run["exit_status"] == 127
        assert "pytest said no" in str(run["detail"])


# --------------------------------------------------------------------- real runs


@NEEDS_312
class TestRealRuns:
    def test_complete_run_with_product_lines(self, tmp_path) -> None:
        run = Case(tmp_path, TESTS).run(NODE)
        assert run == {
            "result": "complete",
            "collected": [NODE],
            "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
            "outcome": "passed",
            "product_lines": [{"file": "pkg/mod.py", "lines": [2]}],
            "product_line_count": 1,
            "process_operations": [],
        }

    def test_teardown_skip_is_a_skipped_outcome(self, tmp_path) -> None:
        tests = {
            "tests/test_a.py": (
                "import pytest\n\n\n@pytest.fixture\ndef fx():\n    yield\n"
                "    pytest.skip('in teardown')\n\n\ndef test_a(fx):\n    pass\n"
            )
        }
        run = Case(tmp_path, tests).run(NODE)
        assert run["result"] == "complete"
        assert run["phases"] == {"setup": "passed", "call": "passed", "teardown": "skipped"}
        assert run["outcome"] == "skipped"

    def test_a_rewritten_product_file_is_mutated_and_reset(self, tmp_path) -> None:
        tests = {
            "tests/test_a.py": (
                "import pathlib\n\n\ndef test_a():\n"
                "    pathlib.Path('pkg/mod.py').write_text('changed = True\\n')\n\n\n"
                "def test_reads():\n"
                f"    assert pathlib.Path('pkg/mod.py').read_text() == {PRODUCT['pkg/mod.py']!r}\n"
            )
        }
        case = Case(tmp_path, tests)
        run = case.run(NODE)
        assert run == {
            "result": "error",
            "reason": "runner",
            "detail": "measured-files-mutated",
            "mutated_paths": ["pkg/mod.py"],
            "exit_status": 0,
        }
        again = case.run("tests/test_a.py::test_reads")
        assert again["result"] == "complete" and again["outcome"] == "passed"

    def test_a_deleted_product_file_is_mutated(self, tmp_path) -> None:
        tests = {"tests/test_a.py": "import os\n\n\ndef test_a():\n    os.remove('pkg/mod.py')\n"}
        run = Case(tmp_path, tests).run(NODE)
        assert run["detail"] == "measured-files-mutated"
        assert run["mutated_paths"] == ["pkg/mod.py"]

    def test_absent_node_id_is_selector_absent(self, tmp_path) -> None:
        case = Case(tmp_path, TESTS)
        assert _kind(lambda: case.run("tests/test_a.py::nope")) is ErrorKind.SELECTOR_ABSENT
        assert list(case.work.iterdir()) == []

    def test_empty_exit_5_is_selector_absent(self, tmp_path) -> None:
        case = Case(tmp_path, {**TESTS, "tests/test_empty.py": "X = 1\n"})
        assert _kind(lambda: case.run("tests/test_empty.py")) is ErrorKind.SELECTOR_ABSENT

    def test_a_manifest_without_phases_is_an_error_run(self, tmp_path) -> None:
        case = Case(tmp_path, TESTS)
        probe = case.probe_dir / f"{PROBE_MODULE}.py"
        source = probe.read_text()
        assert '        "phases": phases,\n' in source
        probe.write_text(source.replace('        "phases": phases,\n', ""))
        run = case.run(NODE)
        assert run["result"] == "error" and run["reason"] == "runner"
        assert "invalid run manifest" in str(run["detail"])
        assert case.run("tests/test_a.py::nope")["result"] == "error"  # never selector-absent

    def test_local_timeout_is_an_error_run(self, tmp_path) -> None:
        tests = {"tests/test_a.py": "import time\n\n\ndef test_a():\n    time.sleep(60)\n"}
        started = time.monotonic()
        run = Case(tmp_path, tests).run(NODE, selector_timeout=2)
        assert time.monotonic() - started < 30
        assert run == {
            "result": "error",
            "reason": "runner",
            "detail": f"{NODE} exceeded 2s",
            "timed_out": True,
        }

    def test_global_deadline_raises_timeout(self, tmp_path) -> None:
        tests = {"tests/test_a.py": "import time\n\n\ndef test_a():\n    time.sleep(60)\n"}
        case = Case(tmp_path, tests)
        kind = _kind(lambda: case.run(NODE, deadline=Deadline(3), selector_timeout=60))
        assert kind is ErrorKind.TIMEOUT
        assert list(case.work.iterdir()) == []

    def test_a_background_process_does_not_hold_the_run(self, tmp_path) -> None:
        pid_file = tmp_path / "bg.pid"
        tests = {
            "tests/test_a.py": (
                "import subprocess\n\n\ndef test_a():\n"
                "    subprocess.run(\n"
                f"        'sleep 60 & echo $! > {pid_file}', shell=True, check=True\n"
                "    )\n"
            )
        }
        started = time.monotonic()
        run = Case(tmp_path, tests).run(NODE, selector_timeout=30)
        assert time.monotonic() - started < 20
        assert run["result"] == "complete" and run["outcome"] == "passed"
        assert run["process_operations"] != []
        pid = int(pid_file.read_text())
        assert _gone(pid)


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False
