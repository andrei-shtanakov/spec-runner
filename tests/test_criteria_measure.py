"""#603 B2b Task 4: the measurement pipeline under one deadline, and its documents.

The heavy steps (git, uv, the product's pytest) are monkeypatched on
`criteria_measure`; their real behaviour is pinned by B2a's and Task 2's tests.
Selection, the digest and the body lines run for real over in-memory blobs.
Every document produced here is validated against `response.schema.json`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from spec_runner import criteria_measure, criteria_run
from spec_runner.criteria_config import ProductCriteria
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import PLUGINS_ESTABLISHED, Excluded, Inventory, TestItem
from spec_runner.criteria_process import Deadline
from spec_runner.criteria_select import content_sha256
from spec_runner.criteria_workspace import Environment

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schemas/criteria-closure/v1/response.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
SHA = "b" * 40
LOCK = "c" * 64
VERSION = "9.9.9"

TEST_A = (
    'def test_traced():\n    """ABC:BEH-1"""\n\n\n'
    'def test_shared():\n    """ABC:BEH-3 ABC:BEH-4"""\n\n\n'
    "def test_other():\n    pass\n"
)
BLOBS = {
    "pkg/mod.py": b"def f():\n    return 1\n",
    "pkg/legacy.py": b"X = 1\n",
    "tests/test_a.py": TEST_A.encode(),
    "tests/conftest.py": b"",
    "pyproject.toml": b"[tool.pytest.ini_options]\n",
}
TRACKED = sorted([*BLOBS, "README.md", "legacy/skipped_mod.py"])
BLOBS["legacy/skipped_mod.py"] = b"Y = 2\n"
ITEMS = (
    TestItem("tests/test_a.py::test_traced", "tests/test_a.py", "test_traced", 1),
    TestItem("tests/test_a.py::test_shared", "tests/test_a.py", "test_shared", 5),
    TestItem("tests/test_a.py::test_other", "tests/test_a.py", "test_other", 9),
)
EXCLUDED = (
    Excluded("deselected", None, "tests/test_a.py::test_gone", None, None),
    Excluded("deselected", None, "tests/test_b.py::test_d", None, ("tests/test_b.py", "test_d", 3)),
    Excluded("ignored", "legacy", None, None, None),
    Excluded("skipped", "tests/test_skip.py", None, "no numpy", None),
)


def _request(*ids: str) -> dict[str, Any]:
    return {
        "protocol": 1,
        "owner_repo": "o/r",
        "workstream": "ws",
        "code": "ABC",
        "bundle_pin": "a" * 40,
        "product_sha": SHA,
        "test_criteria": [{"id": i, "verify_task": False} for i in ids],
    }


def _env() -> Environment:
    return Environment(Path("/env/bin/python"), "CPython", "3.12.13", LOCK, None, ())


def _inventory(
    xdist_active: bool = False, rerunfailures_active: bool = False, force_reruns: bool = False
) -> Inventory:
    return Inventory(
        items=ITEMS,
        test_files=("pyproject.toml", "tests/conftest.py", "tests/test_a.py"),
        inipath="pyproject.toml",
        plugins=("pytest-9.0.2",),
        xdist_active=xdist_active,
        rerunfailures_active=rerunfailures_active,
        rerunfailures_force_reruns=force_reruns,
        excluded=EXCLUDED,
        non_function=("tests/test_a.py::TestX",),
        rootpath="/collected/root",
    )


def _complete(lines: int = 1) -> dict[str, object]:
    return {
        "result": "complete",
        "collected": ["x"],
        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        "outcome": "passed",
        "product_lines": [{"file": "pkg/mod.py", "lines": [2]}] if lines else [],
        "product_line_count": lines,
        "process_operations": [],
    }


class Pipeline:
    """Fakes for every heavy step; `fail` raises a CriteriaError at a named step."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runs: list[dict[str, Any]] = []
        self.steps: list[str] = []
        self.inventory = _inventory()
        self.on_run: Callable[[dict[str, Any]], dict[str, object]] = lambda call: _complete()
        self.fail: dict[str, CriteriaError] = {}
        self.workspaces: list[Path] = []
        m = criteria_measure
        monkeypatch.setattr(m, "check_origin", self._step("check_origin", None))
        monkeypatch.setattr(m, "clone_at", self._clone_at)
        monkeypatch.setattr(
            m,
            "read_product_criteria",
            self._step("read_product_criteria", ProductCriteria(("pkg",), None, ())),
        )
        monkeypatch.setattr(m, "sync_environment", self._sync)
        monkeypatch.setattr(m, "collect", self._collect)
        monkeypatch.setattr(m, "resolve_roots", self._step("resolve_roots", ["pkg/mod.py"]))
        monkeypatch.setattr(m, "tracked_files", self._step("tracked_files", TRACKED))
        monkeypatch.setattr(m, "read_blobs", self._read_blobs)
        monkeypatch.setattr(m, "run_selector", self._run_selector)

    def _step(self, name: str, value: Any) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.steps.append(name)
            if name in self.fail:
                raise self.fail[name]
            return value

        return call

    def _clone_at(self, project_root: Path, sha: str, into: Path, deadline: Deadline) -> Path:
        self._step("clone_at", None)()
        self.workspaces.append(into)
        (into / "src").mkdir(parents=True)
        return into / "src"

    def _sync(self, checkout: Path, sha: str, env_dir: Path, criteria: Any, dl: Any) -> Any:
        assert not env_dir.resolve().is_relative_to(checkout.resolve())
        return self._step("sync_environment", _env())()

    def _collect(self, env: Environment, checkout: Path, sha: str, *rest: Any) -> Inventory:
        return self._step("collect", self.inventory)()  # type: ignore[no-any-return]

    def _read_blobs(self, checkout: Path, sha: str, paths: list[str], dl: Any) -> Any:
        self.steps.append("read_blobs")
        self.read_paths = list(paths)
        return {p: BLOBS[p] for p in paths}

    def _run_selector(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        names = (
            "env", "checkout", "sha", "probe_dir", "work", "node_id", "product_files",
            "measured", "body_lines", "deadline", "selector_timeout",
        )  # fmt: skip
        call = dict(zip(names, args, strict=True)) | kwargs
        self.runs.append(call)
        return self.on_run(call)

    def measure(self, request: object) -> tuple[int, dict[str, Any]]:
        code, doc = criteria_measure.measure(
            Path("/project"), request, selector_timeout=30.0, timeout=600.0, version=VERSION
        )
        errors = sorted(VALIDATOR.iter_errors(doc), key=str)
        assert not errors, [e.message for e in errors]
        if "error" in doc:
            kind = ErrorKind(doc["error"]["kind"])
            assert doc["error"]["retryable"] is kind.retryable
            assert code == kind.exit_code
            assert "beh" not in doc
        json.dumps(doc)  # one JSON document
        for workspace in self.workspaces:
            assert not workspace.exists()
        return code, doc


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    return Pipeline(monkeypatch)


IDS = ("ABC:BEH-1", "ABC:BEH-2", "ABC:BEH-3", "ABC:BEH-4")


class TestAnswer:
    def test_happy_path(self, pipeline: Pipeline) -> None:
        code, doc = pipeline.measure(_request(*IDS))
        assert code == 0
        assert doc["protocol"] == 1 and doc["spec_runner_version"] == VERSION
        assert doc["request"] == _request(*IDS)
        assert doc["product_roots"] == {"declared": ["pkg"], "files": ["pkg/mod.py"]}
        assert doc["test_files"] == ["pyproject.toml", "tests/conftest.py", "tests/test_a.py"]
        assert doc["test_items"][1] == {
            "node_id": "tests/test_a.py::test_shared",
            "definition": {"file": "tests/test_a.py", "qualname": "test_shared", "line": 5},
        }
        assert doc["environment"] == {
            "lock_sha256": LOCK,
            "python": "CPython 3.12.13",
            "pytest_plugins": ["pytest-9.0.2"],
            "groups": None,
            "extras": [],
        }
        beh = {b["id"]: b for b in doc["beh"]}
        assert [b["id"] for b in doc["beh"]] == list(IDS)
        assert beh["ABC:BEH-1"]["status"] == "traced" and "reason" not in beh["ABC:BEH-1"]
        assert [s["node_id"] for s in beh["ABC:BEH-1"]["selectors"]] == [
            "tests/test_a.py::test_traced"
        ]
        assert beh["ABC:BEH-2"] == {
            "id": "ABC:BEH-2",
            "status": "unconfirmed",
            "reason": "no-test",
            "selectors": [],
        }
        assert beh["ABC:BEH-3"]["selectors"] == beh["ABC:BEH-4"]["selectors"]
        assert beh["ABC:BEH-3"]["selectors"][0]["node_id"] == "tests/test_a.py::test_shared"
        assert len(beh["ABC:BEH-3"]["selectors"][0]["runs"]) == 2

    def test_a_shared_selector_runs_exactly_twice(self, pipeline: Pipeline) -> None:
        pipeline.measure(_request(*IDS))
        node_ids = [call["node_id"] for call in pipeline.runs]
        assert (
            node_ids == ["tests/test_a.py::test_traced"] * 2 + ["tests/test_a.py::test_shared"] * 2
        )  # inventory order; test_other is no selector

    def test_statuses_aggregate_from_the_runs(self, pipeline: Pipeline) -> None:
        pipeline.on_run = lambda call: _complete(
            0 if call["node_id"].endswith("test_shared") else 1
        )
        _, doc = pipeline.measure(_request(*IDS))
        beh = {b["id"]: b for b in doc["beh"]}
        assert (beh["ABC:BEH-3"]["status"], beh["ABC:BEH-3"]["reason"]) == (
            "unconfirmed",
            "no-product-execution",
        )
        selector = beh["ABC:BEH-3"]["selectors"][0]
        assert (selector["status"], selector["reason"]) == ("unconfirmed", "no-product-execution")
        assert beh["ABC:BEH-1"]["selectors"][0]["status"] == "traced"

    def test_an_error_run_is_an_error_selector_not_a_failed_step(self, pipeline: Pipeline) -> None:
        pipeline.on_run = lambda call: {"result": "error", "reason": "runner", "detail": "boom"}
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 0
        assert (doc["beh"][0]["status"], doc["beh"][0]["reason"]) == ("error", "runner")

    def test_collection_excluded_by_rule(self, pipeline: Pipeline) -> None:
        _, doc = pipeline.measure(_request(*IDS))
        assert doc["collection_excluded"] == [
            {"how": "deselected", "node_id": "tests/test_a.py::test_gone", "definition": None},
            {
                "how": "deselected",
                "node_id": "tests/test_b.py::test_d",
                "definition": {"file": "tests/test_b.py", "qualname": "test_d", "line": 3},
            },
            {"how": "ignored", "path": "legacy"},
            {"how": "skipped", "path": "tests/test_skip.py", "reason": "no numpy"},
        ]
        assert "non_function" not in json.dumps(doc)

    def test_content_sha256_is_over_the_digest_set(self, pipeline: Pipeline) -> None:
        _, doc = pipeline.measure(_request(*IDS))
        assert "legacy/skipped_mod.py" in pipeline.read_paths  # under an ignored root
        assert "pkg/legacy.py" not in pipeline.read_paths  # not declared product
        expected = content_sha256(
            ["pkg"], LOCK, None, [], {p: BLOBS[p] for p in pipeline.read_paths}
        )
        assert doc["content_sha256"] == expected

    def test_zero_tests_is_every_beh_no_test(self, pipeline: Pipeline) -> None:
        pipeline.inventory = Inventory(
            (), (), None, ("pytest-9.0.2",), False, False, False, (), (), "/r"
        )
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 0 and doc["beh"][0]["reason"] == "no-test" and not pipeline.runs


class TestRunArguments:
    @pytest.mark.parametrize(
        ("xdist", "rerun", "force", "flags"),
        [
            (True, False, False, ["-n", "0", "--dist", "no"]),
            (False, False, False, []),
            (False, True, False, ["--reruns", "0"]),
            (False, True, True, ["--reruns", "0", "--force-reruns", "0"]),
            (True, True, True, ["-n", "0", "--dist", "no", "--reruns", "0", "--force-reruns", "0"]),
        ],
    )
    def test_plugin_args_from_the_inventory(
        self, pipeline: Pipeline, xdist: bool, rerun: bool, force: bool, flags: list[str]
    ) -> None:
        pipeline.inventory = _inventory(
            xdist_active=xdist, rerunfailures_active=rerun, force_reruns=force
        )
        pipeline.measure(_request("ABC:BEH-1"))
        assert pipeline.runs and all(list(c["plugin_args"]) == flags for c in pipeline.runs)

    def test_runs_reuse_the_collections_config(self, pipeline: Pipeline) -> None:
        """R-B15: every run gets the collection's rootdir and config file."""
        pipeline.measure(_request("ABC:BEH-1"))
        checkout = pipeline.workspaces[0] / "src"
        assert pipeline.runs and all(c["rootpath"] == "/collected/root" for c in pipeline.runs)
        assert all(c["inipath"] == str(checkout / "pyproject.toml") for c in pipeline.runs)

    def test_no_collected_config_is_passed_as_none(self, pipeline: Pipeline) -> None:
        pipeline.inventory = dataclasses.replace(_inventory(), inipath=None)
        pipeline.measure(_request("ABC:BEH-1"))
        assert pipeline.runs and all(c["inipath"] is None for c in pipeline.runs)

    def test_measured_bytes_body_lines_and_timeout(self, pipeline: Pipeline) -> None:
        pipeline.measure(_request("ABC:BEH-1"))
        call = pipeline.runs[0]
        assert dict(call["measured"]) == {
            p: BLOBS[p]
            for p in ("pkg/mod.py", "pyproject.toml", "tests/conftest.py", "tests/test_a.py")
        }
        assert dict(call["body_lines"]) == {"pkg/mod.py": frozenset({2})}
        assert list(call["product_files"]) == ["pkg/mod.py"]
        assert call["selector_timeout"] == 30.0 and call["sha"] == SHA
        assert pipeline.runs[0]["deadline"] is pipeline.runs[1]["deadline"]


class TestErrors:
    @pytest.mark.parametrize("data", [None, [], "x", 3])
    def test_request_not_an_object(self, pipeline: Pipeline, data: object) -> None:
        code, doc = pipeline.measure(data)
        assert code == 2 and doc["error"]["kind"] == "request-invalid"
        assert "request" not in doc and pipeline.steps == []

    def test_request_schema_invalid_is_not_echoed(self, pipeline: Pipeline) -> None:
        request = _request("ABC:BEH-1") | {"extra": 1}
        code, doc = pipeline.measure(request)
        assert code == 2 and doc["error"]["kind"] == "request-invalid"
        assert "request" not in doc

    def test_owner_mismatch_echoes_the_request(self, pipeline: Pipeline) -> None:
        pipeline.fail["check_origin"] = CriteriaError(ErrorKind.OWNER_REPO_MISMATCH, "no")
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 2 and doc["request"] == _request("ABC:BEH-1")
        assert set(doc) == {"protocol", "request", "spec_runner_version", "error"}

    def test_product_roots_undeclared(self, pipeline: Pipeline) -> None:
        pipeline.fail["read_product_criteria"] = CriteriaError(
            ErrorKind.PRODUCT_ROOTS_UNDECLARED, "no criteria.product_roots"
        )
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 3 and doc["error"]["kind"] == "product-roots-undeclared"
        assert "environment" not in doc
        assert "sync_environment" not in pipeline.steps

    def test_environment_selection_invalid(self, pipeline: Pipeline) -> None:
        pipeline.fail["sync_environment"] = CriteriaError(
            ErrorKind.ENVIRONMENT_SELECTION_INVALID, "no group 'dev'"
        )
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 3 and doc["error"]["kind"] == "environment-selection-invalid"
        assert "environment" not in doc

    def test_collection_mutated_checkout_carries_environment(self, pipeline: Pipeline) -> None:
        pipeline.fail["collect"] = CriteriaError(
            ErrorKind.COLLECTION_MUTATED_CHECKOUT,
            "collection changed tracked files: pkg/mod.py",
            **{PLUGINS_ESTABLISHED: ["pytest-9.0.2", "pytest-xdist-3.8.0"]},
        )
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 3 and doc["error"]["kind"] == "collection-mutated-checkout"
        assert doc["environment"]["pytest_plugins"] == ["pytest-9.0.2", "pytest-xdist-3.8.0"]
        assert doc["environment"]["python"] == "CPython 3.12.13"
        assert "test_files" not in doc and PLUGINS_ESTABLISHED not in doc

    def test_collection_failed_without_a_manifest_has_no_environment(
        self, pipeline: Pipeline
    ) -> None:
        pipeline.fail["collect"] = CriteriaError(ErrorKind.COLLECTION_FAILED, "no manifest")
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 2 and "environment" not in doc

    def test_overlap_after_collection_carries_the_inventory(self, pipeline: Pipeline) -> None:
        pipeline.inventory = Inventory(
            ITEMS,
            ("pkg/mod.py", "tests/test_a.py"),
            None,
            ("pytest-9.0.2",),
            False,
            False,
            False,
            (),
            (),
            "/r",
        )
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 3 and doc["error"]["kind"] == "product-roots-overlap-tests"
        assert {"environment", "test_files", "test_items", "collection_excluded"} <= set(doc)
        # §4: product_roots after resolution — the overlap is judged on them
        assert doc["product_roots"] == {"declared": ["pkg"], "files": ["pkg/mod.py"]}
        assert "content_sha256" not in doc

    def test_deadline_exhausted_between_runs(self, pipeline: Pipeline) -> None:
        def expire(call: dict[str, Any]) -> dict[str, object]:
            call["deadline"].seconds = 0.0
            return _complete()

        pipeline.on_run = expire
        code, doc = pipeline.measure(_request(*IDS))
        assert code == 2 and doc["error"]["kind"] == "timeout"
        assert len(pipeline.runs) == 1
        for field in ("environment", "test_files", "test_items", "product_roots"):
            assert field in doc
        assert "content_sha256" in doc and "beh" not in doc

    def test_global_timeout_inside_a_run(self, pipeline: Pipeline) -> None:
        def global_expiry(call: dict[str, Any]) -> dict[str, object]:
            raise CriteriaError(ErrorKind.TIMEOUT, "the measurement deadline expired")

        pipeline.on_run = global_expiry
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 2 and "content_sha256" in doc and "beh" not in doc

    def test_unparseable_product_is_unsupported_runtime_before_any_run(
        self, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        versions: list[str] = []

        def refuse(files: Any, blobs: Any, *, product_version: str) -> Any:
            versions.append(product_version)
            raise CriteriaError(ErrorKind.UNSUPPORTED_RUNTIME, "pkg/mod.py: PEP 695")

        monkeypatch.setattr(criteria_measure, "product_body_lines", refuse)
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 2 and doc["error"]["kind"] == "unsupported-runtime"
        assert "content_sha256" in doc and "beh" not in doc and not pipeline.runs
        assert versions == ["3.12.13"]  # the product environment's Python (R-B16)

    @pytest.mark.parametrize(
        ("orchestrator", "code", "kind"),
        [
            ((3, 11, 9), 2, "unsupported-runtime"),
            ((3, 12, 0), 2, "unsupported-runtime"),  # micro counts: older than 3.12.13
            ((3, 12, 13), 3, "product-roots-invalid"),
        ],
    )
    def test_an_unparseable_product_file_by_version(
        self,
        pipeline: Pipeline,
        monkeypatch: pytest.MonkeyPatch,
        orchestrator: tuple[int, int, int],
        code: int,
        kind: str,
    ) -> None:
        """R-B16: older orchestrator → unsupported-runtime; otherwise the product's fault."""
        monkeypatch.setitem(BLOBS, "pkg/mod.py", b"def f(:\n    pass\n")
        monkeypatch.setattr(criteria_run, "_orchestrator_version", lambda: orchestrator)
        got, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert (got, doc["error"]["kind"]) == (code, kind)
        assert "pkg/mod.py" in doc["error"]["detail"] and not pipeline.runs

    def test_selector_absent_from_a_run(self, pipeline: Pipeline) -> None:
        def absent(call: dict[str, Any]) -> dict[str, object]:
            raise CriteriaError(ErrorKind.SELECTOR_ABSENT, "collects no test")

        pipeline.on_run = absent
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 3 and doc["error"]["kind"] == "selector-absent"

    def test_an_unexpected_exception_is_not_a_document(self, pipeline: Pipeline) -> None:
        def bug(call: dict[str, Any]) -> dict[str, object]:
            raise RuntimeError("a bug")

        pipeline.on_run = bug
        with pytest.raises(RuntimeError, match="a bug"):
            criteria_measure.measure(
                Path("/project"),
                _request("ABC:BEH-1"),
                selector_timeout=30.0,
                timeout=600.0,
                version=VERSION,
            )
        assert pipeline.workspaces and not pipeline.workspaces[0].exists()


class TestWorkspaceCleanup:
    def test_a_cleanup_failure_warns_on_stderr_only(
        self,
        pipeline: Pipeline,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def refuse(path: Path) -> None:
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(criteria_measure, "remove_tree", refuse)
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 0 and "beh" in doc
        out, err = capsys.readouterr()
        assert out == ""
        lines = err.splitlines()
        assert len(lines) == 1 and lines[0].startswith("spec-runner: warning:")
        assert "Permission denied" in lines[0]
        assert not pipeline.workspaces[0].exists()  # still removed as far as it goes

    def test_a_clean_removal_is_silent(
        self, pipeline: Pipeline, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pipeline.measure(_request("ABC:BEH-1"))
        assert capsys.readouterr() == ("", "")
        assert not pipeline.workspaces[0].exists()

    def test_a_read_only_directory_left_by_a_test_is_removed_silently(
        self, pipeline: Pipeline, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A product test's 0o500 directory (with a file) under the workspace: no residue."""

        def lock_a_directory(call: dict[str, Any]) -> dict[str, object]:
            locked = Path(call["work"]) / "tmp-left" / "locked"
            locked.mkdir(parents=True, exist_ok=True)
            (locked / "f").write_text("x")
            locked.chmod(0o500)
            return _complete()

        pipeline.on_run = lock_a_directory
        code, _ = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 0
        assert capsys.readouterr() == ("", "")
        assert not pipeline.workspaces[0].exists()


class TestDocumentHead:
    """`established` (from a step or an exception) never overrides the document's head."""

    FORGED = {
        "protocol": 99,
        "request": {"forged": True},
        "spec_runner_version": "0.0.0",
        "error": "forged",
        "test_files": ["tests/test_a.py"],
    }

    def test_error_document_head_wins(self) -> None:
        exc = CriteriaError(ErrorKind.COLLECTION_FAILED, "x", **self.FORGED)
        head: dict[str, object] = {"protocol": 1, "spec_runner_version": VERSION, "request": {}}
        code, doc = criteria_measure._error_document(exc, head, dict(self.FORGED))
        assert (doc["protocol"], doc["spec_runner_version"], doc["request"]) == (1, VERSION, {})
        retryable = ErrorKind.COLLECTION_FAILED.retryable
        assert doc["error"] == {"kind": "collection-failed", "retryable": retryable, "detail": "x"}
        assert code == ErrorKind.COLLECTION_FAILED.exit_code
        assert doc["test_files"] == ["tests/test_a.py"]
        assert list(doc)[:3] == ["protocol", "spec_runner_version", "request"]
        assert not list(VALIDATOR.iter_errors(doc))

    def test_answer_head_wins(self, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:
        original = criteria_measure._measure

        def forging(*args: Any) -> list[dict[str, object]]:
            beh = original(*args)
            args[-1].update({"protocol": 99, "spec_runner_version": "0.0.0", "beh": []})
            return beh

        monkeypatch.setattr(criteria_measure, "_measure", forging)
        code, doc = pipeline.measure(_request("ABC:BEH-1"))
        assert code == 0 and doc["protocol"] == 1 and doc["spec_runner_version"] == VERSION
        assert doc["beh"] and doc["request"] == _request("ABC:BEH-1")
