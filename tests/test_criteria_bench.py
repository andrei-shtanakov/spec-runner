"""#603 B2b: the §8.3 bench — boundary cases of `verify --criteria`, run for real.

Each case is a tiny product committed to a git repository in `tmp_path`, run twice
through `run_selector` under this interpreter and judged by `selector_status`. The
bench needs CPython >= 3.12 (`sys.monitoring`) and runs in its own workflow
(.github/workflows/criteria-probe.yml); the xdist / forked rows need those plugins.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from spec_runner.criteria_aggregate import selector_status
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import collect, deploy_probe
from spec_runner.criteria_process import Deadline
from spec_runner.criteria_run import product_body_lines, run_selector
from spec_runner.criteria_select import select
from spec_runner.criteria_workspace import Environment, distribution_args, read_blobs

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="the bench needs CPython >= 3.12 (sys.monitoring)"
)

COUNTER_ENV = "BENCH_FLAKY_COUNTER"  # not PYTHON*/PYTEST_*/VIRTUAL_ENV/GIT_*: child_env keeps it
CONFTEST = "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"
PRODUCT_FILES = ("pkg/__init__.py", "pkg/mod.py", "pkg/__main__.py")
PRODUCT = {
    "conftest.py": CONFTEST,
    "pkg/__init__.py": "",
    "pkg/mod.py": '"""DOC."""\n\n\ndef work(x):\n    y = x + 1\n    return y\n',
    "pkg/__main__.py": "from pkg.mod import work\n\n\ndef main():\n    return work(1)\n\n\n"
    "main()\n",
}
TRACED = ("traced", None)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _env() -> Environment:
    version = ".".join(str(v) for v in sys.version_info[:3])
    return Environment(Path(sys.executable), "CPython", version, "0" * 64, False, None, ())


class Bench:
    """A committed product at one sha, its probe, and the bytes measured at that sha."""

    def __init__(
        self, tmp_path: Path, files: dict[str, str], product: Sequence[str] = PRODUCT_FILES
    ) -> None:
        self.product = tuple(product)
        self.checkout = (tmp_path / "checkout").resolve()
        self.checkout.mkdir()
        _git(self.checkout, "init", "-q")
        _git(self.checkout, "config", "user.email", "t@example.com")
        _git(self.checkout, "config", "user.name", "t")
        for rel, text in {**PRODUCT, **files}.items():
            path = self.checkout / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(self.checkout, "add", "-A")
        _git(self.checkout, "commit", "-qm", "c")
        self.sha = _git(self.checkout, "rev-parse", "HEAD")
        self.probe_dir = deploy_probe(tmp_path / "probe")
        self.work = tmp_path / "work"
        self.tests = [p for p in files if p.endswith(".py") and p != "conftest.py"]
        # What collection would read from the root (R-B15): the bench's only config file.
        self.inipath = str(self.checkout / "pytest.ini") if "pytest.ini" in files else None
        self.measured = read_blobs(
            self.checkout, self.sha, [*self.product, "conftest.py", *self.tests], Deadline(60)
        )
        self.body_lines = product_body_lines(
            self.product, {p: self.measured[p] for p in self.product}
        )

    def run(self, node_id: str, distribution: Sequence[str] = ()) -> dict[str, object]:
        return run_selector(
            _env(), self.checkout, self.sha, self.probe_dir, self.work, node_id, self.product,
            self.measured, self.body_lines, Deadline(300), 120.0, distribution=distribution,
            rootpath=str(self.checkout), inipath=self.inipath,
        )  # fmt: skip

    def runs(self, node_id: str, distribution: Sequence[str] = ()) -> list[dict[str, object]]:
        return [self.run(node_id, distribution), self.run(node_id, distribution)]

    def status(self, node_id: str) -> tuple[str, str | None]:
        return selector_status(self.runs(node_id))


def _test(name: str, body: str, header: str = "") -> dict[str, str]:
    return {"tests/test_a.py": f"{header}\n\ndef {name}():\n{body}\n"}


NODE = "tests/test_a.py::test_a"


class TestStatuses:
    def test_docstring_read_never_called(self, tmp_path):
        files = _test("test_a", "    import pkg.mod\n    assert pkg.mod.__doc__ == 'DOC.'")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "no-product-execution")

    def test_lazy_import_and_reload(self, tmp_path):
        files = _test(
            "test_a", "    import importlib\n    import pkg.mod\n    importlib.reload(pkg.mod)"
        )
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "no-product-execution")

    def test_bare_assert_true(self, tmp_path):
        files = _test("test_a", "    assert True")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "no-product-execution")

    def test_calls_product_then_asserts_true_is_the_named_boundary(self, tmp_path):
        files = _test("test_a", "    work(1)\n    assert True", "from pkg.mod import work")
        assert Bench(tmp_path, files).status(NODE) == TRACED

    def test_product_only_via_subprocess(self, tmp_path):
        body = "    subprocess.run([sys.executable, '-m', 'pkg'], check=True)"
        files = _test("test_a", body, "import subprocess, sys")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "subprocess-only")

    def test_flaky_test_is_nondeterministic(self, tmp_path, monkeypatch):
        counter = tmp_path / "counter"  # outside the checkout: checkout and TMPDIR reset per run
        monkeypatch.setenv(COUNTER_ENV, str(counter))
        body = (
            f"    path = pathlib.Path(os.environ[{COUNTER_ENV!r}])\n"
            "    first = not path.exists()\n    path.write_text('x')\n"
            "    work(1)\n    assert not first"
        )
        files = _test("test_a", body, "import os, pathlib\nfrom pkg.mod import work")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "nondeterministic")

    def test_setup_created_worker_thread_running_product_code_in_call(self, tmp_path):
        header = (
            "import threading\n\nimport pytest\nfrom pkg.mod import work\n\n\n"
            "@pytest.fixture\ndef worker():\n    go = threading.Event()\n"
            "    thread = threading.Thread(target=lambda: (go.wait(), work(1)))\n"
            "    thread.start()\n    yield go, thread\n"
        )
        files = _test("test_a", "    go, thread = worker\n    go.set()\n    thread.join()", header)
        files["tests/test_a.py"] = files["tests/test_a.py"].replace("test_a():", "test_a(worker):")
        assert Bench(tmp_path, files).status(NODE) == TRACED

    def test_async_test_via_asyncio_run(self, tmp_path):
        body = "    async def go():\n        return work(1)\n    assert asyncio.run(go()) == 2"
        files = _test("test_a", body, "import asyncio\nfrom pkg.mod import work")
        assert Bench(tmp_path, files).status(NODE) == TRACED

    def test_module_level_comprehension_called_nowhere(self, tmp_path):
        mod = "LIST = [i for i in range(3)]\nGEN = list(i for i in range(3))\n"
        files = {**_test("test_a", "    import pkg.mod"), "pkg/mod.py": mod}
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "no-product-execution")

    def test_function_with_comprehension_and_generator_called(self, tmp_path):
        mod = (
            "def work(n):\n    squares = [i * i for i in range(n)]\n"
            "    return sum(i for i in squares)\n"
        )
        files = {**_test("test_a", "    assert work(3) == 5", "from pkg.mod import work")}
        files["pkg/mod.py"] = mod
        bench = Bench(tmp_path, files)
        runs = bench.runs(NODE)
        assert selector_status(runs) == TRACED
        reported = {line for entry in runs[0]["product_lines"] for line in entry["lines"]}  # type: ignore[attr-defined,index,union-attr]
        assert reported == {2, 3}

    def test_os_fork_child_running_the_product(self, tmp_path):
        body = (
            "    pid = os.fork()\n    if pid == 0:\n        work(1)\n        os._exit(0)\n"
            "    os.waitpid(pid, 0)"
        )
        files = _test("test_a", body, "import os\nfrom pkg.mod import work")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "subprocess-only")

    def test_multiprocessing_spawn_is_a_named_boundary(self, tmp_path):
        # R-B9: spawn/forkserver go through _posixsubprocess.fork_exec, which no audited
        # event covers, so this reads no-product-execution rather than subprocess-only.
        body = (
            "    ctx = multiprocessing.get_context('spawn')\n"
            "    proc = ctx.Process(target=work, args=(1,))\n    proc.start()\n    proc.join()"
        )
        files = _test("test_a", body, "import multiprocessing\nfrom pkg.mod import work")
        files["conftest.py"] = CONFTEST
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "no-product-execution")

    def test_setup_failure_is_not_passed_with_call_not_reached(self, tmp_path):
        header = "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError"
        files = _test("test_a", "    pass", header)
        files["tests/test_a.py"] = files["tests/test_a.py"].replace("test_a():", "test_a(broken):")
        runs = Bench(tmp_path, files).runs(NODE)
        assert selector_status(runs) == ("unconfirmed", "not-passed")
        assert runs[0]["phases"]["call"] == "not-reached"  # type: ignore[index]

    def test_teardown_failure_in_both_runs_is_not_passed(self, tmp_path):
        header = (
            "import pytest\nfrom pkg.mod import work\n\n\n@pytest.fixture\ndef late():\n"
            "    yield\n    raise RuntimeError"
        )
        files = _test("test_a", "    work(1)", header)
        files["tests/test_a.py"] = files["tests/test_a.py"].replace("test_a():", "test_a(late):")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "not-passed")

    def test_teardown_failure_in_one_run_is_nondeterministic(self, tmp_path, monkeypatch):
        monkeypatch.setenv(COUNTER_ENV, str(tmp_path / "counter"))
        header = (
            "import os, pathlib\nimport pytest\nfrom pkg.mod import work\n\n\n"
            "@pytest.fixture\ndef late():\n    yield\n"
            f"    path = pathlib.Path(os.environ[{COUNTER_ENV!r}])\n"
            "    first = not path.exists()\n    path.write_text('x')\n"
            "    if first:\n        raise RuntimeError"
        )
        files = _test("test_a", "    work(1)", header)
        files["tests/test_a.py"] = files["tests/test_a.py"].replace("test_a():", "test_a(late):")
        assert Bench(tmp_path, files).status(NODE) == ("unconfirmed", "nondeterministic")


class TestResolvedDefinitions:
    def test_inherited_and_decorated_tests_resolve_to_their_definitions(self, tmp_path):
        source = (
            "import pytest\n\n\nclass Base:\n    def test_inherited(self):\n"
            '        """ENC:BEH-01"""\n\n\nclass TestSub(Base):\n    pass\n\n\n'
            "def deco(fn):\n    return fn\n\n\n@deco\n@pytest.mark.slowish\n"
            'def test_decorated():\n    """ENC:BEH-02"""\n'
        )
        bench = Bench(tmp_path, {"tests/test_a.py": source})
        inventory = collect(
            _env(), bench.checkout, bench.sha, bench.probe_dir, bench.work, Deadline(120), 60.0
        )
        chosen = select(inventory.items, ["ENC:BEH-01", "ENC:BEH-02"], bench.measured)
        inherited = chosen["ENC:BEH-01"]
        decorated = chosen["ENC:BEH-02"]
        assert [(i.node_id, i.qualname) for i in inherited] == [
            ("tests/test_a.py::TestSub::test_inherited", "Base.test_inherited")
        ]
        assert [(i.node_id, i.qualname) for i in decorated] == [
            ("tests/test_a.py::test_decorated", "test_decorated")
        ]


class TestDistribution:
    def test_xdist_in_addopts_runs_in_the_owner_and_is_traced(self, tmp_path):
        pytest.importorskip("xdist")
        files = {
            **_test("test_a", "    assert work(1) == 2", "from pkg.mod import work"),
            "pytest.ini": "[pytest]\naddopts = -n 2\n",
        }
        bench = Bench(tmp_path, files)
        inventory = collect(
            _env(), bench.checkout, bench.sha, bench.probe_dir, bench.work, Deadline(120), 60.0
        )
        assert inventory.xdist_active is True
        runs = bench.runs(NODE, distribution_args(inventory.xdist_active))
        assert selector_status(runs) == TRACED

    def test_forked_in_addopts_is_distributed_execution(self, tmp_path):
        pytest.importorskip("pytest_forked")
        files = {
            **_test("test_a", "    assert work(1) == 2", "from pkg.mod import work"),
            "pytest.ini": "[pytest]\naddopts = --forked\n",
        }
        with pytest.raises(CriteriaError) as raised:
            Bench(tmp_path, files).run(NODE)
        assert raised.value.kind is ErrorKind.DISTRIBUTED_EXECUTION
