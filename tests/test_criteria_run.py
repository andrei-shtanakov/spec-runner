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

from spec_runner import criteria_process, criteria_run
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import collect, deploy_probe
from spec_runner.criteria_process import Deadline, Finished
from spec_runner.criteria_protocol import MANIFEST_ENV, PROBE_MODULE, PRODUCT_FILES_ENV
from spec_runner.criteria_run import product_body_lines, run_selector
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
    """A committed product, its deployed probe, and the `measured` bytes and body lines at sha."""

    def __init__(self, tmp_path: Path, tests: dict[str, str]) -> None:
        self.tmp_path = tmp_path
        self.checkout, self.sha = _repo(tmp_path / "checkout", {**PRODUCT, **tests})
        self.probe_dir = deploy_probe(tmp_path / "probe")
        self.work = tmp_path / "work"
        measured = [*PRODUCT_FILES, "conftest.py", *tests]
        self.measured = read_blobs(self.checkout, self.sha, measured, Deadline(60))
        self.body_lines = product_body_lines(
            PRODUCT_FILES, {p: self.measured[p] for p in PRODUCT_FILES}
        )

    def run(
        self,
        node_id: str,
        *,
        deadline: Deadline | None = None,
        selector_timeout: float = 120.0,
        distribution: Sequence[str] = (),
        rootpath: str | None = None,
        inipath: str | None = None,
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
            self.body_lines,
            deadline or Deadline(300),
            selector_timeout,
            distribution=distribution,
            rootpath=rootpath or str(self.checkout),
            inipath=inipath,
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
    returncode: int | None = 0,
    pid: int = 4242,
    stderr: bytes = b"pytest said no\n",
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
        return Finished(returncode, b"", stderr, pid, None)

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
        (argv,) = seen
        ini = argv[9]
        assert argv == [sys.executable, "-P", "-m", "pytest", "-p", PROBE_MODULE] + [
            "--rootdir", str(case.checkout), "-c", ini, "-n", "0", "--dist", "no", "-q", NODE
        ]  # fmt: skip
        assert run["result"] == "error" and run["reason"] == "runner"
        assert "no run manifest" in str(run["detail"])

    def test_no_distribution_flags_by_default(self, tmp_path, monkeypatch) -> None:
        seen = _fake_pytest(monkeypatch, lambda argv, env: None)
        case = Case(tmp_path, TESTS)
        case.run(NODE)
        (argv,) = seen
        assert argv[4:] == ["-p", PROBE_MODULE, "--rootdir", str(case.checkout), "-c", argv[9]] + [
            "-q", NODE
        ]  # fmt: skip

    def test_no_collected_config_runs_with_an_empty_ini(self, tmp_path, monkeypatch) -> None:
        """R-B15: the collection read no config, so neither may a run (tests/pytest.ini)."""
        inis: list[tuple[Path, str]] = []

        def manifest(argv: list[str], env: dict[str, str]) -> None:
            ini = Path(argv[argv.index("-c") + 1])
            assert ini.parent == Path(env[MANIFEST_ENV]).parent  # the invocation dir
            inis.append((ini, ini.read_text()))

        seen = _fake_pytest(monkeypatch, manifest)
        case = Case(tmp_path, TESTS)
        case.run(NODE, rootpath="/collected/root")
        assert seen[0][seen[0].index("--rootdir") + 1] == "/collected/root"
        assert [(ini.name, text) for ini, text in inis] == [("pytest.ini", "")]

    def test_the_collections_config_file_is_passed(self, tmp_path, monkeypatch) -> None:
        seen = _fake_pytest(monkeypatch, lambda argv, env: None)
        case = Case(tmp_path, TESTS)
        ini = str(case.checkout / "pyproject.toml")
        case.run(NODE, inipath=ini)
        assert seen[0][seen[0].index("-c") + 1] == ini

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

    def test_a_test_file_is_never_a_product_file(self, tmp_path, monkeypatch) -> None:
        def manifest(argv: list[str], env: dict[str, str]) -> dict[str, Any]:
            return _manifest(NODE, product_lines={str(case.checkout / "tests/test_a.py"): [5]})

        _fake_pytest(monkeypatch, manifest)
        case = Case(tmp_path, TESTS)
        run = case.run(NODE)
        assert run["result"] == "error" and "undeclared product file" in str(run["detail"])
        assert "tests/test_a.py" in str(run["detail"])

    def test_the_detail_is_bounded_and_keeps_the_end(self, tmp_path, monkeypatch) -> None:
        stderr = b"".join(b"line %d %s\n" % (i, b"x" * 400) for i in range(200)) + b"THE END\n"
        _fake_pytest(monkeypatch, lambda argv, env: None, stderr=stderr)
        detail = str(Case(tmp_path, TESTS).run(NODE)["detail"])
        assert len(detail) <= criteria_run.MAX_DETAIL
        assert detail.startswith("[truncated ") and detail.endswith("THE END")

    def test_a_short_detail_is_not_marked(self, tmp_path, monkeypatch) -> None:
        _fake_pytest(monkeypatch, lambda argv, env: _manifest(NODE, monitoring_error="why"))
        assert Case(tmp_path, TESTS).run(NODE)["detail"] == "why"

    def test_no_exit_status_without_a_timeout_is_not_called_a_timeout(
        self, tmp_path, monkeypatch
    ) -> None:
        _fake_pytest(monkeypatch, lambda argv, env: None, returncode=None)
        run = Case(tmp_path, TESTS).run(NODE)
        assert run == {"result": "error", "reason": "runner", "detail": f"{NODE}: no exit status"}

    def test_run_selector_uses_the_precomputed_body_lines(self, tmp_path, monkeypatch) -> None:
        def manifest(argv: list[str], env: dict[str, str]) -> dict[str, Any]:
            return _manifest(NODE, product_lines={str(case.checkout / "pkg/mod.py"): [1, 2]})

        _fake_pytest(monkeypatch, manifest)
        case = Case(tmp_path, TESTS)
        case.body_lines = {"pkg/__init__.py": frozenset(), "pkg/mod.py": frozenset({1})}
        monkeypatch.setattr(criteria_run, "function_body_lines", _never)
        run = case.run(NODE)
        assert run["product_lines"] == [{"file": "pkg/mod.py", "lines": [1]}]


def _never(source: str) -> frozenset[int]:
    raise AssertionError("body lines are computed once, before the runs")


class TestProductBodyLines:
    def test_body_lines_per_product_file(self) -> None:
        blobs = {"a.py": b"X = 1\ndef f():\n    return 1\n", "b.py": b""}
        assert product_body_lines(["a.py", "b.py"], blobs) == {
            "a.py": frozenset({3}),
            "b.py": frozenset(),
        }

    def test_unparseable_grammar_is_unsupported_runtime(self) -> None:
        blobs = {"pkg/new.py": b"def f(:\n    pass\n"}
        with pytest.raises(CriteriaError) as raised:
            product_body_lines(["pkg/new.py"], blobs)
        assert raised.value.kind is ErrorKind.UNSUPPORTED_RUNTIME
        assert "pkg/new.py" in raised.value.detail
        assert f"orchestrator Python {sys.version.split()[0]}" in raised.value.detail

    def test_undecodable_bytes_are_unsupported_runtime(self) -> None:
        with pytest.raises(CriteriaError) as raised:
            product_body_lines(["a.py"], {"a.py": b"x = '\xff'\n"})
        assert raised.value.kind is ErrorKind.UNSUPPORTED_RUNTIME

    @pytest.mark.parametrize("exc", [RecursionError, MemoryError])
    def test_parser_exhaustion_is_unsupported_runtime(self, monkeypatch, exc) -> None:
        def explode(source: str) -> frozenset[int]:
            raise exc("too deep")

        monkeypatch.setattr(criteria_run, "function_body_lines", explode)
        with pytest.raises(CriteriaError) as raised:
            product_body_lines(["a.py"], {"a.py": b"x = 1\n"})
        assert raised.value.kind is ErrorKind.UNSUPPORTED_RUNTIME


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

    def test_a_nested_pytest_ini_runs_with_the_collections_resolution(self, tmp_path) -> None:
        """R-B15: collection (no path args) reads no config; `<node_id>` alone would find
        tests/pytest.ini, root pytest at tests/ and name the test `test_a.py::test_a`."""
        case = Case(tmp_path, {**TESTS, "tests/pytest.ini": "[pytest]\n"})
        inventory = collect(
            _env(), case.checkout, case.sha, case.probe_dir, tmp_path / "collect", Deadline(120),
            120.0,
        )  # fmt: skip
        assert inventory.inipath is None and inventory.rootpath == str(case.checkout)
        assert [item.node_id for item in inventory.items] == [NODE]
        run = case.run(NODE, rootpath=inventory.rootpath, inipath=None)
        assert run["result"] == "complete" and run["collected"] == [NODE]
        assert run["outcome"] == "passed" and run["product_line_count"] == 1

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

    def test_a_rewritten_test_file_is_mutated(self, tmp_path) -> None:
        tests = {
            "tests/test_a.py": (
                "import pathlib\n\n\ndef test_a():\n"
                "    pathlib.Path('tests/test_a.py').write_text('# gone\\n')\n"
            )
        }
        run = Case(tmp_path, tests).run(NODE)
        assert run["detail"] == "measured-files-mutated"
        assert run["mutated_paths"] == ["tests/test_a.py"]

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
