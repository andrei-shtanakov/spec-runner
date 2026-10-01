"""#603 B2a Task 7: the probe's collect mode and the inventory it yields (probe/1).

Two halves. The fast half fakes only the product's pytest process (a manifest
written where the orchestrator asked for it) and exercises the orchestrator's
rules against a real git checkout: validation, the error order, relativisation,
the exclusion filter, the mutation check and the reset. The slow half runs the
real probe inside a real product environment built by `sync_environment`
(offline, from the local uv cache).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from spec_runner import criteria_inventory, criteria_process
from spec_runner.criteria_config import ProductCriteria
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import (
    PLUGINS_ESTABLISHED,
    Excluded,
    Inventory,
    TestItem,
    collect,
    deploy_probe,
    plugin_args,
)
from spec_runner.criteria_process import Deadline, Finished
from spec_runner.criteria_protocol import MANIFEST_ENV, MODE_ENV, PARENT_ENV, PROBE_MODULE
from spec_runner.criteria_workspace import Environment, sync_environment

REAL_RUN = criteria_process.run_bounded
FAKE_PID = 777001
PROBE_SOURCE = Path(__file__).parent.parent / "src" / "spec_runner" / "criteria_probe.py"


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


def _dl() -> Deadline:
    return Deadline(300)


def _kind(call: Callable[[], object]) -> CriteriaError:
    with pytest.raises(CriteriaError) as raised:
        call()
    return raised.value


# --------------------------------------------------------------------------- fast


def _fake_env() -> Environment:
    return Environment(Path("/fake/bin/python"), "CPython", "3.12.0", "0" * 64, None, ())


def _function(checkout: Path, node_id: str, file: str, qualname: str, line: int) -> dict[str, Any]:
    module = node_id.split("::", 1)[0]
    return {
        "node_id": node_id,
        "function": True,
        "module": str(checkout / module),
        "definition": {"file": str(checkout / file), "qualname": qualname, "line": line},
    }


def _manifest(checkout: Path, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "probe": 1,
        "mode": "collect",
        "pid": FAKE_PID,
        "python": {"implementation": "CPython", "version": "3.12.0"},
        "rootpath": str(checkout),
        "inipath": str(checkout / "pyproject.toml"),
        "plugins": ["pytest-9.1.1"],
        "xdist_active": False,
        "rerunfailures_active": False,
        "rerunfailures_force_reruns": False,
        "conftests": [str(checkout / "tests" / "conftest.py")],
        "items": [_function(checkout, "tests/test_a.py::test_x", "tests/test_a.py", "test_x", 1)],
        "errors": [],
        "excluded": [],
        "exitstatus": 0,
        "complete": True,
    }
    data.update(overrides)
    return data


class FakePytest:
    """Stands in for the product's pytest; every other process runs for real."""

    def __init__(
        self,
        manifest: Callable[[Path], dict[str, Any] | None],
        *,
        rc: int = 0,
        timed_out: str | None = None,
        stderr: bytes = b"",
        touch: Callable[[Path], None] | None = None,
    ) -> None:
        self.manifest, self.rc, self.timed_out = manifest, rc, timed_out
        self.stderr, self.touch = stderr, touch
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Finished:
        if list(argv[1:4]) != ["-P", "-m", "pytest"]:
            return REAL_RUN(argv, **kwargs)
        env, cwd = kwargs["env"], kwargs["cwd"]
        self.calls.append(
            {"argv": list(argv), **kwargs, "existed": Path(env[MANIFEST_ENV]).exists()}
        )
        if self.touch is not None:
            self.touch(cwd)
        data = self.manifest(cwd)
        if data is not None:
            Path(env[MANIFEST_ENV]).write_text(json.dumps(data))
        rc = None if self.timed_out else self.rc
        return Finished(rc, b"", self.stderr, FAKE_PID, self.timed_out)  # type: ignore[arg-type]


BASE = {
    "pyproject.toml": "[tool.pytest.ini_options]\n",
    "tests/conftest.py": "",
    "tests/test_a.py": "def test_x():\n    pass\n",
    "tests/helpers.py": "class Base:\n    def test_i(self):\n        pass\n",
    "tests/test_d.py": "def test_d():\n    pass\n",
    "tests/data/fixture.txt": "tracked\n",
    "pkg/mod.py": "VALUE = 1\n",
}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """`fake(manifest_fn, **opts)` → (collect thunk, FakePytest, checkout, sha)."""
    checkout, sha = _repo(tmp_path / "co", BASE)

    def make(manifest: Callable[[Path], dict[str, Any] | None], **opts: Any) -> Any:
        deadline = opts.pop("deadline", None) or _dl()
        runner = FakePytest(manifest, **opts)
        monkeypatch.setattr(criteria_process, "run_bounded", runner)

        def run() -> Inventory:
            return collect(
                _fake_env(),
                checkout,
                sha,
                tmp_path / "probe",
                tmp_path / "work",
                deadline,
                60.0,
            )

        return run, runner, checkout, sha

    return make


class TestInvocation:
    def test_argv_env_and_fresh_paths(self, fake, tmp_path: Path) -> None:
        run, runner, checkout, _ = fake(lambda co: _manifest(co))
        run()
        run()
        first, second = runner.calls
        assert first["argv"] == [
            "/fake/bin/python",
            "-P",
            "-m",
            "pytest",
            "-p",
            PROBE_MODULE,
            "--collect-only",
            "-q",
        ]
        assert first["cwd"] == checkout and first["local_timeout"] == 60.0
        env = first["env"]
        assert env[PARENT_ENV] == str(os.getpid()) and env[MODE_ENV] == "collect"
        assert env["PYTHONPATH"] == str(tmp_path / "probe") and env["PYTHONNOUSERSITE"] == "1"
        assert Path(env[MANIFEST_ENV]).is_absolute() and not first["existed"]
        assert env[MANIFEST_ENV] != second["env"][MANIFEST_ENV]
        assert Path(env["TMPDIR"]).is_relative_to(tmp_path / "work")
        assert env["TMPDIR"] != second["env"]["TMPDIR"]

    def test_a_read_only_directory_under_tmpdir_is_removed(
        self, fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A conftest leaving a 0o500 directory in collection's TMPDIR leaves no residue."""
        run, runner, _, _ = fake(lambda co: _manifest(co))
        tmpdirs: list[Path] = []

        def locking(argv: list[str], **kwargs: Any) -> Finished:
            if list(argv[1:4]) == ["-P", "-m", "pytest"]:
                tmpdirs.append(Path(kwargs["env"]["TMPDIR"]))
                locked = tmpdirs[-1] / "locked"
                locked.mkdir()
                (locked / "f").write_text("x")
                locked.chmod(0o500)
            return runner(argv, **kwargs)

        monkeypatch.setattr(criteria_process, "run_bounded", locking)
        run()
        assert tmpdirs and not tmpdirs[0].exists()

    def test_collection_output_is_bounded(self, fake) -> None:
        """R-B20: pytest's output is read only as a tail — the one bounded capture."""
        run, runner, _, _ = fake(lambda co: _manifest(co))
        run()
        assert runner.calls[0]["max_output"] == criteria_process.DEFAULT_MAX_OUTPUT

    def test_collect_carries_no_distribution_flags(self, fake) -> None:
        manifest = {
            "xdist_active": True,
            "rerunfailures_active": True,
            "rerunfailures_force_reruns": True,
        }
        run, runner, _, _ = fake(lambda co: _manifest(co, **manifest))
        run()
        assert runner.calls[0]["argv"][6:] == ["--collect-only", "-q"]
        assert "-n" not in runner.calls[0]["argv"]


class TestPluginArgs:
    """The per-run flags that neutralise plugins whose own process model hides the call."""

    @pytest.mark.parametrize(
        ("xdist", "rerun", "force", "flags"),
        [
            (False, False, False, []),
            (True, False, False, ["-n", "0", "--dist", "no"]),
            (False, True, False, ["--reruns", "0"]),
            (False, True, True, ["--reruns", "0", "--force-reruns", "0"]),
            (True, True, True, ["-n", "0", "--dist", "no", "--reruns", "0", "--force-reruns", "0"]),
        ],
    )
    def test_flags_follow_the_registered_plugins(
        self, xdist: bool, rerun: bool, force: bool, flags: list[str]
    ) -> None:
        inventory = Inventory((), (), None, (), xdist, rerun, force, (), (), "/r")
        assert plugin_args(inventory) == flags


class TestManifestToInventory:
    @pytest.mark.parametrize("active", [True, False])
    def test_xdist_active_comes_from_the_manifest(self, fake, active: bool) -> None:
        run, _, _, _ = fake(lambda co: _manifest(co, xdist_active=active))
        assert run().xdist_active is active

    @pytest.mark.parametrize("active", [True, False])
    def test_rerunfailures_active_comes_from_the_manifest(self, fake, active: bool) -> None:
        run, _, _, _ = fake(lambda co: _manifest(co, rerunfailures_active=active))
        assert run().rerunfailures_active is active

    @pytest.mark.parametrize("force", [True, False])
    def test_force_reruns_comes_from_the_manifest(self, fake, force: bool) -> None:
        manifest = {"rerunfailures_active": True, "rerunfailures_force_reruns": force}
        run, _, _, _ = fake(lambda co: _manifest(co, **manifest))
        assert run().rerunfailures_force_reruns is force

    def test_items_files_config_and_plugins(self, fake) -> None:
        def manifest(co: Path) -> dict[str, Any]:
            doctest = {
                "node_id": "pkg/mod.py::pkg.mod",
                "function": False,
                "module": str(co / "pkg/mod.py"),
                "definition": None,
            }
            items = [
                _function(co, "tests/test_a.py::test_x", "tests/test_a.py", "test_x", 1),
                _function(co, "tests/test_a.py::T::test_i", "tests/helpers.py", "Base.test_i", 2),
                doctest,
            ]
            return _manifest(co, items=items)

        run, _, checkout, _ = fake(manifest)
        inv = run()
        assert inv.items == (
            TestItem("tests/test_a.py::test_x", "tests/test_a.py", "test_x", 1),
            TestItem("tests/test_a.py::T::test_i", "tests/helpers.py", "Base.test_i", 2),
        )
        assert inv.test_files == (
            "pyproject.toml",
            "tests/conftest.py",
            "tests/helpers.py",
            "tests/test_a.py",
        )
        assert inv.inipath == "pyproject.toml" and inv.plugins == ("pytest-9.1.1",)
        assert inv.rootpath == str(checkout)  # R-B15: the runs reuse it
        assert inv.excluded == ()
        assert inv.non_function == ("pkg/mod.py::pkg.mod",)  # R20: counted, not dropped

    def test_zero_items_exit_5_is_an_empty_inventory(self, fake) -> None:
        run, *_ = fake(lambda co: _manifest(co, items=[], exitstatus=5, inipath=None), rc=5)
        inv = run()
        assert inv.items == () and inv.inipath is None


CONFTEST_ERR = (
    b"ImportError while loading conftest '/co/tests/conftest.py'.\n"
    b"tests/conftest.py:1: in <module>\n    import no_such_mod_xyz\n"
    b"E   ModuleNotFoundError: No module named 'no_such_mod_xyz'\n"
)


class TestFailures:
    @pytest.mark.parametrize(
        "manifest",
        [
            pytest.param(lambda co: None, id="no manifest"),
            pytest.param(lambda co: {**_manifest(co), "pid": FAKE_PID + 1}, id="another pid"),
            pytest.param(lambda co: _manifest(co, items=[]), id="items empty, exit 0"),
            pytest.param(
                lambda co: {k: v for k, v in _manifest(co).items() if k != "items"},
                id="items key missing",
            ),
            pytest.param(lambda co: {**_manifest(co), "complete": False}, id="incomplete"),
        ],
    )
    def test_invalid_manifest_is_collection_failed(self, fake, manifest) -> None:
        run, *_ = fake(manifest, stderr=b"line one\nboom at the end\n")
        error = _kind(run)
        assert error.kind is ErrorKind.COLLECTION_FAILED
        assert "exit 0" in error.detail and "boom at the end" in error.detail

    def test_no_manifest_establishes_no_plugins(self, fake) -> None:
        run, *_ = fake(lambda co: None, rc=1)
        assert _kind(run).established == {}

    def test_local_timeout_is_collection_failed(self, fake) -> None:
        run, *_ = fake(lambda co: None, timed_out="local")
        assert _kind(run).kind is ErrorKind.COLLECTION_FAILED

    def test_global_timeout_is_timeout(self, fake) -> None:
        run, *_ = fake(lambda co: None, timed_out="global")
        assert _kind(run).kind is ErrorKind.TIMEOUT

    def test_errors_are_collection_error_and_win_over_mutation(self, fake) -> None:
        errors = [{"node_id": "tests/test_b.py", "message": "ImportError: no_such_module"}]
        run, _, checkout, _ = fake(
            lambda co: _manifest(co, errors=errors, exitstatus=2),
            rc=2,
            touch=lambda co: (co / "pkg/mod.py").write_text("changed\n"),
        )
        error = _kind(run)
        assert error.kind is ErrorKind.COLLECTION_ERROR and "tests/test_b.py" in error.detail
        assert (checkout / "pkg/mod.py").read_text() == "VALUE = 1\n"  # reset on the error path
        assert error.established == {PLUGINS_ESTABLISHED: ["pytest-9.1.1"]}

    def test_config_outside_the_checkout(self, fake, tmp_path: Path) -> None:
        run, *_ = fake(lambda co: _manifest(co, inipath=str(tmp_path / "pytest.ini")))
        error = _kind(run)
        assert error.kind is ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT
        assert "pytest.ini" in error.detail

    @pytest.mark.parametrize("where", ["null", "outside", "<string>", "untracked"])
    def test_unresolvable_definition(self, fake, tmp_path: Path, where: str) -> None:
        def manifest(co: Path) -> dict[str, Any]:
            item = _function(co, "tests/test_a.py::test_x", "tests/test_a.py", "test_x", 1)
            if where == "null":
                item["definition"] = None
            elif where == "outside":
                item["definition"]["file"] = str(tmp_path / "elsewhere.py")
            elif where == "<string>":  # R19: exec-made code, realpath'd into the checkout
                item["definition"]["file"] = str(co / "<string>")
            else:  # R19: inside the checkout, but not a file at sha
                (co / "tests/generated.py").write_text("def test_x():\n    pass\n")
                item["definition"]["file"] = str(co / "tests/generated.py")
            return _manifest(co, items=[item])

        run, *_ = fake(manifest)
        error = _kind(run)
        assert error.kind is ErrorKind.DEFINITION_UNRESOLVED
        assert "tests/test_a.py::test_x" in error.detail

    def test_mutated_checkout_names_paths_and_is_restored(self, fake) -> None:
        def touch(co: Path) -> None:
            (co / "pkg/mod.py").write_text("changed\n")
            (co / "tests/data/fixture.txt").unlink()
            (co / "untracked.txt").write_text("made by collection\n")

        run, _, checkout, _ = fake(lambda co: _manifest(co), touch=touch)
        error = _kind(run)
        assert error.kind is ErrorKind.COLLECTION_MUTATED_CHECKOUT
        assert "pkg/mod.py" in error.detail and "tests/data/fixture.txt" in error.detail
        assert "untracked.txt" not in error.detail
        assert error.established == {PLUGINS_ESTABLISHED: ["pytest-9.1.1"]}  # the manifest's
        assert (checkout / "pkg/mod.py").read_text() == "VALUE = 1\n"
        assert (checkout / "tests/data/fixture.txt").exists()
        assert not (checkout / "untracked.txt").exists()

    def test_a_commit_during_collection_is_a_mutation(self, fake) -> None:
        def commit(co: Path) -> None:  # HEAD moves with the change: `git status` is clean
            (co / "pkg/mod.py").write_text("changed\n")
            _git(co, "commit", "-qam", "made by collection")

        run, _, checkout, sha = fake(lambda co: _manifest(co), touch=commit)
        error = _kind(run)
        assert error.kind is ErrorKind.COLLECTION_MUTATED_CHECKOUT and "pkg/mod.py" in error.detail
        assert _git(checkout, "rev-parse", "HEAD") == sha
        assert (checkout / "pkg/mod.py").read_text() == "VALUE = 1\n"

    def test_a_reset_failure_does_not_hide_the_error(self, fake, monkeypatch) -> None:
        def broken_reset(*args: Any) -> None:
            raise OSError("reset exploded")

        monkeypatch.setattr(criteria_inventory, "reset_checkout", broken_reset)
        run, *_ = fake(lambda co: None, rc=3)
        assert _kind(run).kind is ErrorKind.COLLECTION_FAILED

    @pytest.mark.parametrize(
        ("rc", "stderr", "kind"),
        [
            pytest.param(4, CONFTEST_ERR, ErrorKind.COLLECTION_ERROR, id="initial conftest"),
            pytest.param(4, b"ERROR: usage\n", ErrorKind.COLLECTION_FAILED, id="4 without it"),
            pytest.param(1, CONFTEST_ERR, ErrorKind.COLLECTION_FAILED, id="marker, not 4"),
        ],
    )
    def test_initial_conftest_failure_without_manifest(self, fake, rc, stderr, kind) -> None:
        run, *_ = fake(lambda co: None, rc=rc, stderr=stderr)  # R21
        error = _kind(run)
        assert error.kind is kind
        if kind is ErrorKind.COLLECTION_ERROR:
            assert "no_such_mod_xyz" in error.detail

    def test_success_also_resets(self, fake) -> None:
        run, _, checkout, _ = fake(
            lambda co: _manifest(co), touch=lambda co: (co / ".pytest_cache").mkdir()
        )
        run()
        assert not (checkout / ".pytest_cache").exists()


class TestExcluded:
    def test_filtered_relativised_and_sorted(self, fake, tmp_path: Path) -> None:
        outside = str(tmp_path / "elsewhere")

        def manifest(co: Path) -> dict[str, Any]:
            inside_def = {"file": str(co / "tests/test_d.py"), "qualname": "test_d", "line": 3}
            outside_def = {"file": outside + "/x.py", "qualname": "test_o", "line": 1}
            untracked_def = {"file": str(co / "tests/gen.py"), "qualname": "test_u", "line": 1}
            excluded = [
                {"how": "skipped", "path": str(co / "tests/test_skip.py"), "reason": "Skipped: no"},
                {"how": "skipped", "path": outside + "/test_s.py", "reason": "r"},
                {"how": "ignored", "path": str(co / "tests/test_a.py")},  # tracked file
                {"how": "ignored", "path": str(co / "tests/data")},  # dir with tracked files
                {"how": "ignored", "path": str(co / ".venv")},  # untracked
                {"how": "ignored", "path": str(co / "tests/__pycache__")},  # untracked
                {"how": "ignored", "path": outside},
                {"how": "deselected", "node_id": "tests/test_d.py::test_z", "definition": None},
                {
                    "how": "deselected",
                    "node_id": "tests/test_d.py::test_d[1]",
                    "definition": inside_def,
                },
                {
                    "how": "deselected",
                    "node_id": "tests/test_o.py::test_o",
                    "definition": outside_def,
                },
                {
                    "how": "deselected",
                    "node_id": "tests/test_u.py::test_u",
                    "definition": untracked_def,
                },
            ]
            return _manifest(co, excluded=excluded)

        run, *_ = fake(manifest)
        assert run().excluded == (
            Excluded(
                "deselected",
                None,
                "tests/test_d.py::test_d[1]",
                None,
                ("tests/test_d.py", "test_d", 3),
            ),
            Excluded("deselected", None, "tests/test_d.py::test_z", None, None),
            Excluded("deselected", None, "tests/test_o.py::test_o", None, None),
            Excluded("deselected", None, "tests/test_u.py::test_u", None, None),
            Excluded("ignored", "tests/data", None, None, None),
            Excluded("ignored", "tests/test_a.py", None, None, None),
            Excluded("skipped", "tests/test_skip.py", None, "Skipped: no", None),
        )

    def test_ties_are_ordered_by_every_field(self, fake) -> None:
        """Same how and path/node_id: reason, then definition decide — never set order."""

        def entries(co: Path, reverse: bool) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = [
                {
                    "how": "deselected",
                    "node_id": "tests/test_d.py::test_d",
                    "definition": {
                        "file": str(co / "tests/test_d.py"),
                        "qualname": "test_d",
                        "line": line,
                    },
                }
                for line in range(1, 7)
            ]
            out += [{"how": "deselected", "node_id": "tests/test_d.py::test_d", "definition": None}]
            out += [
                {"how": "skipped", "path": str(co / "tests/test_s.py"), "reason": reason}
                for reason in ("a", "b", "c", "d", "e", "f")
            ]
            return out[::-1] if reverse else out

        want_deselected = [None, *(("tests/test_d.py", "test_d", n) for n in range(1, 7))]
        want_reasons = ["a", "b", "c", "d", "e", "f"]
        for reverse in (False, True):
            run, *_ = fake(lambda co, r=reverse: _manifest(co, excluded=entries(co, r)))
            got = run().excluded
            assert [e.definition for e in got if e.how == "deselected"] == want_deselected
            assert [e.reason for e in got if e.how == "skipped"] == want_reasons


# --------------------------------------------------------------------------- probe source


class TestProbeSource:
    def test_imports_only_stdlib_and_pytest(self) -> None:
        tree = ast.parse(PROBE_SOURCE.read_text(encoding="utf-8"))
        roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert roots - set(sys.stdlib_module_names) == {"pytest"}

    def test_deployed_as_text(self, tmp_path: Path) -> None:
        probe = deploy_probe(tmp_path / "into")
        assert probe.parent == tmp_path / "into"
        deployed = probe / f"{PROBE_MODULE}.py"
        assert deployed.read_text(encoding="utf-8") == PROBE_SOURCE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- slow: real probe

ENV_PYPROJECT = (
    '[project]\nname = "p"\nversion = "0"\nrequires-python = ">=3.12"\ndependencies = []\n'
    '[dependency-groups]\ntest = ["pytest"]\n[tool.uv]\npackage = false\n'
)


@pytest.fixture(scope="module")
def product_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Environment]:
    """One real product environment (pytest from the uv cache) shared by the slow tests."""
    root = tmp_path_factory.mktemp("envproject")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("UV_OFFLINE", "1")
        _repo(root, {"pyproject.toml": ENV_PYPROJECT})
        subprocess.run(["uv", "lock", "-q", "--offline"], cwd=root, check=True)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "lock")
        sha = _git(root, "rev-parse", "HEAD")
        criteria = ProductCriteria(("p",), ("test",), ())
        yield sync_environment(root, sha, tmp_path_factory.mktemp("env"), criteria, Deadline(300))


REAL_BASE = {
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
    "tests/conftest.py": "",
    "tests/helpers.py": "class Base:\n    def test_inherited(self):\n        pass\n",
    "tests/test_a.py": (
        "import pytest\n"
        "from helpers import Base\n\n\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_p(x):\n    pass\n\n\n"
        "class TestSub(Base):\n    pass\n"
    ),
    "pkg/mod.py": "VALUE = 1\n",
}


def _real(
    env: Environment,
    tmp_path: Path,
    files: dict[str, str],
    *,
    local_timeout: float = 120.0,
    probe: Callable[[Path], None] | None = None,
    after_commit: Callable[[Path], None] | None = None,
) -> Inventory:
    checkout, sha = _repo(tmp_path / "case" / "checkout", files)
    if after_commit is not None:
        after_commit(checkout)
    probe_dir = deploy_probe(tmp_path / "probe")
    if probe is not None:
        probe(probe_dir / f"{PROBE_MODULE}.py")
    return collect(env, checkout, sha, probe_dir, tmp_path / "work", _dl(), local_timeout)


@pytest.mark.slow
class TestRealInventory:
    def test_items_with_real_definitions_and_test_files(self, product_env, tmp_path) -> None:
        inv = _real(product_env, tmp_path, REAL_BASE)
        by_id = {i.node_id: i for i in inv.items}
        assert set(by_id) == {
            "tests/test_a.py::test_p[1]",
            "tests/test_a.py::test_p[2]",
            "tests/test_a.py::TestSub::test_inherited",
        }
        assert (
            by_id["tests/test_a.py::test_p[1]"].qualname,
            by_id["tests/test_a.py::test_p[1]"].line,
        ) == ("test_p", 5)  # the decorator line
        inherited = by_id["tests/test_a.py::TestSub::test_inherited"]
        assert (inherited.file, inherited.qualname, inherited.line) == (
            "tests/helpers.py",
            "Base.test_inherited",
            2,
        )
        assert inv.test_files == (
            "pyproject.toml",
            "tests/conftest.py",
            "tests/helpers.py",
            "tests/test_a.py",
        )
        assert inv.inipath == "pyproject.toml"
        assert any(p.startswith("pytest-") for p in inv.plugins)
        assert inv.excluded == ()

    def test_zero_tests_is_a_valid_empty_inventory(self, product_env, tmp_path) -> None:
        files = {"pyproject.toml": "[tool.pytest.ini_options]\n", "tests/test_none.py": "x = 1\n"}
        assert _real(product_env, tmp_path, files).items == ()

    def test_import_error_is_collection_error(self, product_env, tmp_path) -> None:
        files = {**REAL_BASE, "tests/test_b.py": "import no_such_module\n"}
        error = _kind(lambda: _real(product_env, tmp_path, files))
        assert error.kind is ErrorKind.COLLECTION_ERROR and "tests/test_b.py" in error.detail

    def test_parent_pythonpath_does_not_reach_the_product(
        self, product_env, tmp_path, monkeypatch
    ) -> None:  # Review Focus 4
        extra = tmp_path / "parent-only"
        extra.mkdir()
        (extra / "parent_only_mod.py").write_text("X = 1\n")
        monkeypatch.setenv("PYTHONPATH", str(extra))
        files = {**REAL_BASE, "tests/test_b.py": "import parent_only_mod\n"}
        error = _kind(lambda: _real(product_env, tmp_path, files))
        assert error.kind is ErrorKind.COLLECTION_ERROR and "tests/test_b.py" in error.detail

    def test_a_hook_environment_does_not_reach_the_products_git(
        self, product_env, tmp_path, monkeypatch
    ) -> None:
        """A conftest running git sees the temp checkout, not the hook's GIT_DIR."""
        decoy = tmp_path / "decoy"
        decoy_root, _ = _repo(decoy, {"d.py": "x = 1\n"})
        report = tmp_path / "toplevel.txt"
        conftest = (
            "import pathlib, subprocess\n"
            "out = subprocess.run(['git', 'rev-parse', '--show-toplevel'],\n"
            "                     capture_output=True, text=True).stdout.strip()\n"
            f"pathlib.Path({str(report)!r}).write_text(out)\n"
        )
        files = {**REAL_BASE, "tests/conftest.py": conftest}

        def hook_env(checkout: Path) -> None:
            monkeypatch.setenv("GIT_DIR", str(decoy_root / ".git"))
            monkeypatch.setenv("GIT_WORK_TREE", str(decoy_root))
            monkeypatch.setenv("GIT_INDEX_FILE", str(decoy_root / ".git" / "index"))

        _real(product_env, tmp_path, files, after_commit=hook_env)
        checkout = (tmp_path / "case" / "checkout").resolve()
        assert Path(report.read_text()).resolve() == checkout

    def test_local_timeout_is_collection_failed(self, product_env, tmp_path) -> None:
        files = {"tests/test_slow.py": "import time\ntime.sleep(30)\n"}
        error = _kind(lambda: _real(product_env, tmp_path, files, local_timeout=2))
        assert error.kind is ErrorKind.COLLECTION_FAILED

    def test_config_outside_the_checkout(self, product_env, tmp_path) -> None:
        (tmp_path / "case").mkdir()
        (tmp_path / "case" / "pytest.ini").write_text("[pytest]\n")
        files = {"tests/test_a.py": "def test_x():\n    pass\n"}
        error = _kind(lambda: _real(product_env, tmp_path, files))
        assert error.kind is ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT

    def test_mutation_at_import_is_reported_and_restored(self, product_env, tmp_path) -> None:
        mutate = (
            "from pathlib import Path\n"
            "(Path(__file__).parent.parent / 'pkg' / 'mod.py').write_text('VALUE = 2\\n')\n\n"
            "def test_m():\n    pass\n"
        )
        files = {**REAL_BASE, "tests/test_mut.py": mutate}
        error = _kind(lambda: _real(product_env, tmp_path, files))
        assert error.kind is ErrorKind.COLLECTION_MUTATED_CHECKOUT and "pkg/mod.py" in error.detail
        checkout = tmp_path / "case" / "checkout"
        assert (checkout / "pkg" / "mod.py").read_text() == "VALUE = 1\n"

    def test_exec_generated_test_is_definition_unresolved(self, product_env, tmp_path) -> None:
        generated = "exec('def test_generated():\\n    pass\\n')\n"  # R19: no source file
        files = {**REAL_BASE, "tests/test_gen.py": generated}
        error = _kind(lambda: _real(product_env, tmp_path, files))
        assert error.kind is ErrorKind.DEFINITION_UNRESOLVED
        assert "tests/test_gen.py::test_generated" in error.detail

    def test_doctest_items_are_counted_as_non_function(self, product_env, tmp_path) -> None:
        pyproject = (
            "[tool.pytest.ini_options]\ntestpaths = ['tests', 'pkg']\n"
            "addopts = '--doctest-modules'\n"
        )
        module = '"""Doc.\n\n>>> 1 + 1\n2\n"""\nVALUE = 1\n'
        files = {**REAL_BASE, "pyproject.toml": pyproject, "pkg/mod.py": module}
        inv = _real(product_env, tmp_path, files)
        assert inv.non_function == ("pkg/mod.py::mod",)  # R20
        assert all(i.node_id.startswith("tests/") for i in inv.items)

    @pytest.mark.parametrize(
        "conftest",
        [
            pytest.param("import no_such_mod_xyz\n", id="import error"),
            pytest.param("def broken(:\n    pass\n", id="syntax error"),
        ],
    )
    def test_initial_conftest_failure_is_collection_error(
        self, product_env, tmp_path, conftest
    ) -> None:  # R21: pytest exits 4 before any session, so no manifest
        files = {**REAL_BASE, "tests/conftest.py": conftest}
        error = _kind(lambda: _real(product_env, tmp_path, files))
        assert error.kind is ErrorKind.COLLECTION_ERROR
        assert "while loading conftest" in error.detail

    def test_probe_without_its_items_write_is_collection_failed(
        self, product_env, tmp_path
    ) -> None:  # Review Focus 1
        def strip_items(path: Path) -> None:
            text = path.read_text(encoding="utf-8")
            assert text.count("    _record_items(session)\n") == 1
            path.write_text(text.replace("    _record_items(session)\n", "    pass\n"))

        error = _kind(lambda: _real(product_env, tmp_path, REAL_BASE, probe=strip_items))
        assert error.kind is ErrorKind.COLLECTION_FAILED


@pytest.mark.slow
class TestRealExclusions:
    def test_module_level_importorskip_is_skipped(self, product_env, tmp_path) -> None:
        skip = "import pytest\npytest.importorskip('nonexistent_xyz')\n\ndef test_s():\n    pass\n"
        inv = _real(product_env, tmp_path, {**REAL_BASE, "tests/test_skip.py": skip})
        (entry,) = inv.excluded
        assert (entry.how, entry.path) == ("skipped", "tests/test_skip.py")
        assert entry.reason is not None and "nonexistent_xyz" in entry.reason

    def test_collect_ignore_glob_and_ignore_option(self, product_env, tmp_path) -> None:
        files = {
            **REAL_BASE,
            "pyproject.toml": (
                "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
                "addopts = '--ignore=tests/test_cli_ignored.py'\n"
            ),
            "tests/conftest.py": (
                "collect_ignore = ['test_ignored.py']\ncollect_ignore_glob = ['test_glob_*.py']\n"
            ),
            "tests/test_ignored.py": "def test_i():\n    pass\n",
            "tests/test_glob_one.py": "def test_g():\n    pass\n",
            "tests/test_cli_ignored.py": "def test_c():\n    pass\n",
        }

        def venv_and_cache(checkout: Path) -> None:
            (checkout / ".venv" / "lib").mkdir(parents=True)
            (checkout / ".venv" / "lib" / "test_v.py").write_text("def test_v():\n    pass\n")
            (checkout / "tests" / "__pycache__").mkdir()

        inv = _real(product_env, tmp_path, files, after_commit=venv_and_cache)
        assert [(e.how, e.path) for e in inv.excluded] == [
            ("ignored", "tests/test_cli_ignored.py"),
            ("ignored", "tests/test_glob_one.py"),
            ("ignored", "tests/test_ignored.py"),
        ]
        assert {i.node_id.split("::")[0] for i in inv.items} == {"tests/test_a.py"}

    def test_venv_and_pycache_are_never_reported(self, product_env, tmp_path) -> None:
        files = {**REAL_BASE, "pyproject.toml": "[tool.pytest.ini_options]\n"}  # no testpaths

        def venv_and_cache(checkout: Path) -> None:
            (checkout / ".venv" / "lib").mkdir(parents=True)
            (checkout / ".venv" / "lib" / "test_v.py").write_text("def test_v():\n    pass\n")
            (checkout / "tests" / "__pycache__").mkdir()

        inv = _real(product_env, tmp_path, files, after_commit=venv_and_cache)
        assert all(
            ".venv" not in (e.path or "") and "__pycache__" not in (e.path or "")
            for e in inv.excluded
        )

    def test_conftest_removal_without_deselected_hook(self, product_env, tmp_path) -> None:
        conftest = (
            "def pytest_collection_modifyitems(items):\n"
            "    items[:] = [i for i in items if 'test_p[2]' not in i.nodeid]\n"
        )
        inv = _real(product_env, tmp_path, {**REAL_BASE, "tests/conftest.py": conftest})
        assert inv.excluded == (
            Excluded(
                "deselected",
                None,
                "tests/test_a.py::test_p[2]",
                None,
                ("tests/test_a.py", "test_p", 5),
            ),
        )
        assert "tests/test_a.py::test_p[2]" not in {i.node_id for i in inv.items}

    def test_outer_wrapper_removal_is_deselected(self, product_env, tmp_path) -> None:
        conftest = (  # R22: runs outside every other wrapper, after its yield
            "import pytest\n\n"
            "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
            "def pytest_collection_modifyitems(items):\n"
            "    result = yield\n"
            "    items[:] = [i for i in items if 'test_p[1]' not in i.nodeid]\n"
            "    return result\n"
        )
        inv = _real(product_env, tmp_path, {**REAL_BASE, "tests/conftest.py": conftest})
        assert inv.excluded == (
            Excluded(
                "deselected", None, "tests/test_a.py::test_p[1]", None,
                ("tests/test_a.py", "test_p", 5),
            ),
        )  # fmt: skip

    def test_k_in_addopts_is_deselected(self, product_env, tmp_path) -> None:
        pyproject = (
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\naddopts = '-k \"not inherited\"'\n"
        )
        inv = _real(product_env, tmp_path, {**REAL_BASE, "pyproject.toml": pyproject})
        assert inv.excluded == (
            Excluded(
                "deselected",
                None,
                "tests/test_a.py::TestSub::test_inherited",
                None,
                ("tests/helpers.py", "Base.test_inherited", 2),
            ),
        )


def _plugin_env(factory: pytest.TempPathFactory, plugin: str) -> Iterator[Environment]:
    """A product environment whose lock carries `plugin` (offline, from the uv cache)."""
    root = factory.mktemp(f"{plugin}project")
    pyproject = ENV_PYPROJECT.replace('test = ["pytest"]', f'test = ["pytest", "{plugin}"]')
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("UV_OFFLINE", "1")
        _repo(root, {"pyproject.toml": pyproject})
        subprocess.run(["uv", "lock", "-q", "--offline"], cwd=root, check=True)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "lock")
        sha = _git(root, "rev-parse", "HEAD")
        criteria = ProductCriteria(("p",), ("test",), ())
        yield sync_environment(root, sha, factory.mktemp("env"), criteria, Deadline(300))


@pytest.fixture(scope="module")
def xdist_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Environment]:
    yield from _plugin_env(tmp_path_factory, "pytest-xdist")


@pytest.fixture(scope="module")
def rerunfailures_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Environment]:
    yield from _plugin_env(tmp_path_factory, "pytest-rerunfailures")


@pytest.mark.slow
class TestRealXdistActive:
    @staticmethod
    def _files(addopts: str) -> dict[str, str]:
        ini = "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        return {
            **REAL_BASE,
            "pyproject.toml": ini + (f"addopts = '{addopts}'\n" if addopts else ""),
        }

    def test_blocked_xdist_is_not_active_though_its_distribution_is_listed(
        self, xdist_env, tmp_path
    ) -> None:
        inv = _real(xdist_env, tmp_path, self._files("-p no:xdist"))
        assert "tests/test_a.py::test_p[1]" in {i.node_id for i in inv.items}
        assert inv.xdist_active is False
        # measured: the distribution is still listed (its looponfail entry point registers),
        # which is why the plugin list cannot decide the flags
        assert any(name.startswith("pytest-xdist-") for name in inv.plugins)

    @pytest.mark.parametrize("addopts", ["", "-n 2"])
    def test_registered_xdist_is_active(self, xdist_env, tmp_path, addopts) -> None:
        inv = _real(xdist_env, tmp_path, self._files(addopts))
        assert "tests/test_a.py::test_p[1]" in {i.node_id for i in inv.items}
        assert inv.xdist_active is True

    def test_xdist_loaded_by_module_without_autoload_is_active(self, xdist_env, tmp_path) -> None:
        """Measured: `-p xdist.plugin` under `--disable-plugin-autoload` registers as
        `xdist.plugin`, not `xdist` — and `-n` exists."""
        inv = _real(xdist_env, tmp_path, self._files("--disable-plugin-autoload -p xdist.plugin"))
        assert inv.xdist_active is True

    def test_no_xdist_installed_is_inactive(self, product_env, tmp_path) -> None:
        assert _real(product_env, tmp_path, REAL_BASE).xdist_active is False


@pytest.mark.slow
class TestRealRerunfailuresActive:
    """Registered as `rerunfailures` (entry point) or `pytest_rerunfailures` (`-p` by module
    under `--disable-plugin-autoload`) — measured on pytest-rerunfailures 16.7."""

    @staticmethod
    def _files(addopts: str) -> dict[str, str]:
        return TestRealXdistActive._files(addopts)

    @pytest.mark.parametrize("addopts", ["", "--reruns 2"])
    def test_registered_rerunfailures_is_active(self, rerunfailures_env, tmp_path, addopts) -> None:
        inv = _real(rerunfailures_env, tmp_path, self._files(addopts))
        assert inv.rerunfailures_active is True
        assert inv.rerunfailures_force_reruns is True  # 16.7 has --force-reruns

    def test_loaded_by_module_without_autoload_is_active(self, rerunfailures_env, tmp_path) -> None:
        addopts = "--disable-plugin-autoload -p pytest_rerunfailures"
        inv = _real(rerunfailures_env, tmp_path, self._files(addopts))
        assert inv.rerunfailures_active is True and inv.rerunfailures_force_reruns is True

    def test_blocked_rerunfailures_is_not_active(self, rerunfailures_env, tmp_path) -> None:
        inv = _real(rerunfailures_env, tmp_path, self._files("-p no:rerunfailures"))
        assert inv.rerunfailures_active is False and inv.rerunfailures_force_reruns is False

    def test_not_installed_is_inactive(self, product_env, tmp_path) -> None:
        assert _real(product_env, tmp_path, REAL_BASE).rerunfailures_active is False
