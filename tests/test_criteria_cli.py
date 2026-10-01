"""#603 B2b: `spec-runner verify --criteria`, the command (design §3.1-3.2, §4, §6.1).

stdout carries exactly one JSON document on every `--criteria` path; the command reads the
measured repo's config only through `criteria_config` at product_sha, never through main().
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from spec_runner import __version__
from spec_runner.cli import main

ROOT = Path(__file__).parent.parent
SCHEMA = json.loads((ROOT / "schemas/criteria-closure/v1/response.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    code = raised.value.code
    assert isinstance(code, int)
    return code, capsys.readouterr().out


class TestUsage:
    def test_criteria_without_request_is_a_usage_error(self, capsys) -> None:
        code, out = _run(["verify", "--criteria", "--json"], capsys)
        assert code == 2
        assert out == ""

    def test_criteria_without_json_is_a_usage_error(self, capsys, tmp_path: Path) -> None:
        code, out = _run(["verify", "--criteria", "--request", str(tmp_path / "r.json")], capsys)
        assert code == 2
        assert out == ""

    def test_help_documents_the_flags(self, capsys) -> None:
        with pytest.raises(SystemExit):
            main(["verify", "--help"])
        text = capsys.readouterr().out
        for flag in ("--criteria", "--request", "--selector-timeout", "--timeout"):
            assert flag in text
        assert "--json" in text

    def test_plain_verify_is_unchanged(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "spec").mkdir()
        try:
            main(["verify", "--project-root", str(tmp_path), "--json"])
        except SystemExit as exc:
            assert exc.code in (0, 1)
        assert "protocol" not in json.loads(capsys.readouterr().out)


class TestRequestInvalid:
    def _document(self, argv: list[str], capsys) -> dict:
        code, out = _run(argv, capsys)
        assert code == 2
        document = json.loads(out)  # the whole of stdout is one document
        assert document["error"]["kind"] == "request-invalid"
        assert document["spec_runner_version"] == __version__
        assert "beh" not in document
        assert not list(VALIDATOR.iter_errors(document))
        return document

    def test_missing_file(self, tmp_path: Path, capsys) -> None:
        argv = ["verify", "--criteria", "--request", str(tmp_path / "nope.json"), "--json"]
        self._document(argv, capsys)

    def test_invalid_json(self, tmp_path: Path, capsys) -> None:
        request = tmp_path / "r.json"
        request.write_text("{not json")
        self._document(["verify", "--criteria", "--request", str(request), "--json"], capsys)

    def test_non_utf8_file(self, tmp_path: Path, capsys) -> None:
        request = tmp_path / "r.json"
        request.write_bytes(b"\xff\xfe\x00")
        self._document(["verify", "--criteria", "--request", str(request), "--json"], capsys)

    def test_a_valid_json_that_is_no_request(self, tmp_path: Path, capsys) -> None:
        request = tmp_path / "r.json"
        request.write_text("[]")
        self._document(["verify", "--criteria", "--request", str(request), "--json"], capsys)

    def test_a_broken_config_in_the_measured_repo_does_not_prevent_it(
        self, tmp_path: Path, capsys
    ) -> None:
        (tmp_path / "spec-runner.config.yaml").write_text("executor: {}\nmax_retries: 3\n")
        request = tmp_path / "r.json"
        request.write_text("{}")
        argv = ["verify", "--criteria", "--project-root", str(tmp_path)]
        self._document([*argv, "--request", str(request), "--json"], capsys)

    def test_a_malformed_config_yaml_does_not_prevent_it(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "spec-runner.config.yaml").write_text("{: [unclosed")
        argv = ["verify", "--criteria", "--project-root", str(tmp_path), "--json"]
        self._document([*argv, "--request", str(tmp_path / "missing.json")], capsys)


class TestRobustness:
    def test_deeply_nested_json_is_request_invalid(self, tmp_path: Path, capsys) -> None:
        request = tmp_path / "r.json"
        request.write_text("[" * 100000)
        code, out = _run(["verify", "--criteria", "--request", str(request), "--json"], capsys)
        assert code == 2
        assert json.loads(out)["error"]["kind"] == "request-invalid"

    @pytest.mark.parametrize("flag", ["--selector-timeout", "--timeout"])
    @pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "abc"])
    def test_timeouts_must_be_positive_and_finite(self, flag: str, value: str, capsys) -> None:
        argv = ["verify", "--criteria", "--request", "r.json", "--json", flag, value]
        code, out = _run(argv, capsys)
        assert code == 2
        assert out == ""


class TestRoot:
    def _captured(self, monkeypatch, argv: list[str], capsys) -> Path:
        seen: list[Path] = []

        def fake(project_root, data, **kwargs):
            seen.append(project_root)
            return 0, {"protocol": 1}

        monkeypatch.setattr("spec_runner.criteria_measure.measure", fake)
        request = Path(argv[argv.index("--request") + 1])
        request.write_text("{}")
        _run(argv, capsys)
        return seen[0]

    def test_a_subdirectory_measures_the_enclosing_repo(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        repo = (tmp_path / "repo").resolve()
        (repo / "sub").mkdir(parents=True)
        _git(repo, "init", "-q")
        monkeypatch.chdir(repo / "sub")
        argv = ["verify", "--criteria", "--request", str(tmp_path / "r.json"), "--json"]
        assert self._captured(monkeypatch, argv, capsys) == repo

    def test_outside_a_repo_falls_back_to_the_cwd(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        plain = (tmp_path / "plain").resolve()
        plain.mkdir()
        monkeypatch.chdir(plain)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        argv = ["verify", "--criteria", "--request", str(tmp_path / "r.json"), "--json"]
        assert self._captured(monkeypatch, argv, capsys) == plain

    def test_an_explicit_project_root_wins(self, tmp_path: Path, monkeypatch, capsys) -> None:
        elsewhere = (tmp_path / "e").resolve()
        elsewhere.mkdir()
        argv = ["verify", "--criteria", "--project-root", str(elsewhere)]
        argv += ["--request", str(tmp_path / "r.json"), "--json"]
        assert self._captured(monkeypatch, argv, capsys) == elsewhere


def test_the_command_path_needs_no_pytest(tmp_path: Path) -> None:
    code = (
        "import sys; sys.modules['pytest'] = None; sys.modules['_pytest'] = None\n"
        "import spec_runner.cli\n"
        f"spec_runner.cli.main(['verify', '--criteria', '--request', {str(tmp_path / 'x')!r},"
        " '--json'])\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 2, done.stderr
    assert json.loads(done.stdout)["error"]["kind"] == "request-invalid"


# --- the slow end to end -------------------------------------------------------------

PYPROJECT = (
    '[project]\nname = "p"\nversion = "0"\nrequires-python = ">=3.12"\ndependencies = []\n'
    '[dependency-groups]\ndev = ["pytest"]\n[tool.uv]\npackage = false\n'
)
CONFIG = "criteria:\n  product_roots:\n    - pkg\n  environment:\n    groups: [dev]\n"
FILES = {
    "pyproject.toml": PYPROJECT,
    "spec-runner.config.yaml": CONFIG,
    "conftest.py": "import os\nimport sys\n\nsys.path.insert(0, os.path.dirname(__file__))\n",
    "pkg/__init__.py": "",
    "pkg/mod.py": '"""DOC."""\n\n\ndef work(x):\n    y = x + 1\n    return y\n',
    "tests/test_a.py": (
        "import pkg.mod\n\n\n"
        "def test_traced():\n    '''ENC:BEH-01'''\n    assert pkg.mod.work(1) == 2\n\n\n"
        "def test_reader():\n    '''ENC:BEH-02'''\n    assert pkg.mod.__doc__ == 'DOC.'\n"
    ),
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(root: Path, files: dict[str, str] = FILES) -> str:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "remote", "add", "origin", "git@github.com:o/r.git")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["uv", "lock", "-q", "--offline"], cwd=root, check=True)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    return _git(root, "rev-parse", "HEAD")


def _show(root: Path, sha: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=root, capture_output=True, check=True
    ).stdout


def _definition_at(root: Path, sha: str, file: str, qualname: str) -> int:
    tree = ast.parse(_show(root, sha, file))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == qualname.split(".")[-1]:
            return node.lineno
    raise AssertionError(qualname)


@pytest.mark.slow
@pytest.mark.skipif(sys.version_info < (3, 12), reason="the product env needs CPython >= 3.12")
def test_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("UV_OFFLINE", "1")
    root = tmp_path / "repo"
    try:
        sha = _repo(root)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"uv cannot lock offline here: {exc}")
    request = {
        "protocol": 1,
        "owner_repo": "o/r",
        "workstream": "ws",
        "code": "ENC",
        "bundle_pin": "a" * 40,
        "product_sha": sha,
        "test_criteria": [
            {"id": "ENC:BEH-01", "verify_task": False},
            {"id": "ENC:BEH-02", "verify_task": False},
            {"id": "ENC:BEH-03", "verify_task": False},
        ],
    }
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request))
    code, out = _run(
        ["verify", "--criteria", "--project-root", str(root), "--request", str(request_file),
         "--json"],
        capsys,
    )  # fmt: skip
    document = json.loads(out)
    if document.get("error", {}).get("kind") == "environment-sync-failed":
        pytest.skip(f"uv cannot provide the environment offline: {document['error']['detail']}")
    assert code == 0, document
    assert not list(VALIDATOR.iter_errors(document))
    assert document["request"] == request
    statuses = {b["id"]: (b["status"], b.get("reason")) for b in document["beh"]}
    assert statuses == {
        "ENC:BEH-01": ("traced", None),
        "ENC:BEH-02": ("unconfirmed", "no-product-execution"),
        "ENC:BEH-03": ("unconfirmed", "no-test"),
    }
    assert document["environment"]["groups"] == ["dev"]

    assert document["product_roots"]["files"] == ["pkg/__init__.py", "pkg/mod.py"]
    assert document["test_files"] == ["conftest.py", "pyproject.toml", "tests/test_a.py"]
    assert document["collection_excluded"] == []
    digest_files = [
        "conftest.py",
        "pkg/__init__.py",
        "pkg/mod.py",
        "pyproject.toml",
        "tests/test_a.py",
    ]
    lock = hashlib.sha256(_show(root, sha, "uv.lock")).hexdigest()
    rev4 = {
        "v": 1,
        "product_roots": ["pkg"],
        "lock": lock,
        "environment": {"groups": ["dev"], "extras": []},
        "files": [[p, hashlib.sha256(_show(root, sha, p)).hexdigest()] for p in digest_files],
    }
    encoded = json.dumps(rev4, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert document["content_sha256"] == hashlib.sha256(encoded.encode("ascii")).hexdigest()

    items = {i["node_id"]: i for i in document["test_items"]}
    assert set(items) == {"tests/test_a.py::test_traced", "tests/test_a.py::test_reader"}
    for beh in document["beh"]:
        for selector in beh["selectors"]:
            assert selector["definition"] == items[selector["node_id"]]["definition"]
    for item in document["test_items"]:
        qualname = item["definition"]["qualname"]
        assert item["definition"]["line"] == _definition_at(
            root, sha, item["definition"]["file"], qualname
        )


def _request_for(sha: str, *ids: str) -> dict[str, object]:
    return {
        "protocol": 1,
        "owner_repo": "o/r",
        "workstream": "ws",
        "code": "ENC",
        "bundle_pin": "a" * 40,
        "product_sha": sha,
        "test_criteria": [{"id": i, "verify_task": False} for i in ids],
    }


def _locked_repo(root: Path, files: dict[str, str]) -> str:
    try:
        return _repo(root, files)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"uv cannot lock offline here: {exc}")


@pytest.mark.slow
@pytest.mark.skipif(sys.version_info < (3, 12), reason="the product env needs CPython >= 3.12")
def test_a_nested_pytest_ini_is_measured_with_the_collections_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """R-B15: collection reads no config; a run given `<node_id>` alone would find
    tests/pytest.ini, root pytest at tests/ and collect nothing under the node id."""
    monkeypatch.setenv("UV_OFFLINE", "1")
    root = tmp_path / "repo"
    sha = _locked_repo(root, {**FILES, "tests/pytest.ini": "[pytest]\n"})
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(_request_for(sha, "ENC:BEH-01")))
    code, out = _run(
        ["verify", "--criteria", "--project-root", str(root), "--request", str(request_file),
         "--json"],
        capsys,
    )  # fmt: skip
    document = json.loads(out)
    if document.get("error", {}).get("kind") == "environment-sync-failed":
        pytest.skip(f"uv cannot provide the environment offline: {document['error']['detail']}")
    assert code == 0, document
    (beh,) = document["beh"]
    assert (beh["status"], beh.get("reason")) == ("traced", None), beh
    (selector,) = beh["selectors"]
    assert [run["collected"] for run in selector["runs"]] == [["tests/test_a.py::test_traced"]] * 2


def _gone(pid: int) -> bool:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


SLEEPER = '''import os
import time

import pkg.mod


def test_traced():
    """ENC:BEH-01"""
    with open({pid_file!r}, "w") as handle:
        handle.write(str(os.getpid()))
    time.sleep(300)
    assert pkg.mod.work(1) == 2
'''


@pytest.mark.slow
@pytest.mark.skipif(sys.version_info < (3, 12), reason="the product env needs CPython >= 3.12")
def test_sigterm_mid_run_kills_the_child_and_removes_the_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """R-B17: SIGTERM unwinds through run_bounded's kill and the TemporaryDirectory."""
    monkeypatch.setenv("UV_OFFLINE", "1")
    pid_file = tmp_path / "child.pid"  # outside the checkout: the test writes it
    root = tmp_path / "repo"
    sha = _locked_repo(root, {**FILES, "tests/test_a.py": SLEEPER.format(pid_file=str(pid_file))})
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(_request_for(sha, "ENC:BEH-01")))
    tmpdir = tmp_path / "tmp"  # the workspace's parent: TemporaryDirectory honours TMPDIR
    tmpdir.mkdir()
    argv = [sys.executable, "-m", "spec_runner", "verify", "--criteria"]
    argv += ["--project-root", str(root), "--request", str(request_file), "--json"]
    proc = subprocess.Popen(
        argv, env={**os.environ, "TMPDIR": str(tmpdir)}, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 240
        while not pid_file.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if not pid_file.exists():
            if proc.poll() is None:
                raise AssertionError("the selector run never started")
            out, _ = proc.communicate(timeout=30)
            if b"environment-sync-failed" in out:
                pytest.skip("uv cannot provide the environment offline")
            raise AssertionError(f"the selector run never started: {out[-2000:]!r}")
        time.sleep(0.5)  # the pid is written; the test now sleeps inside pytest
        child = int(pid_file.read_text())
        assert any(tmpdir.glob("spec-runner-criteria-*"))
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert proc.returncode == 143, err[-2000:]
    assert out == b""  # no document: the measurement was interrupted
    assert _gone(child)
    assert list(tmpdir.glob("spec-runner-criteria-*")) == []


class TestTerminationSignals:
    @pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
    def test_a_signal_unwinds_as_system_exit_and_handlers_are_restored(
        self, sig: signal.Signals, code: int
    ) -> None:
        from spec_runner import cli

        before = signal.getsignal(sig)
        unwound: list[bool] = []
        with pytest.raises(SystemExit) as raised, cli._exit_on_termination():
            try:
                os.kill(os.getpid(), sig)
                time.sleep(5)  # the handler interrupts this
            finally:
                unwound.append(True)
        assert raised.value.code == code and unwound == [True]
        assert signal.getsignal(sig) is before

    def test_handlers_are_restored_after_a_normal_exit(self) -> None:
        from spec_runner import cli

        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        with cli._exit_on_termination():
            assert signal.getsignal(signal.SIGTERM) is not before[signal.SIGTERM]
        assert {s: signal.getsignal(s) for s in before} == before
