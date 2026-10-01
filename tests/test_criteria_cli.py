"""#603 B2b: `spec-runner verify --criteria`, the command (design §3.1-3.2, §4, §6.1).

stdout carries exactly one JSON document on every `--criteria` path; the command reads the
measured repo's config only through `criteria_config` at product_sha, never through main().
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from spec_runner import __version__
from spec_runner.cli import main
from spec_runner.criteria_select import content_sha256, digest_paths

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


def _repo(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "remote", "add", "origin", "git@github.com:o/r.git")
    for rel, text in FILES.items():
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

    excluded = [
        type("E", (), {"how": e["how"], "path": e.get("path")})()
        for e in document["collection_excluded"]
    ]
    paths = digest_paths(
        document["product_roots"]["files"],
        document["test_files"],
        excluded,  # type: ignore[arg-type]
        [
            p
            for p in _git(root, "ls-tree", "-r", "--name-only", sha).splitlines()
            if p.endswith(".py")
        ],
    )
    blobs = {p: _show(root, sha, p) for p in paths}
    lock = hashlib.sha256(_show(root, sha, "uv.lock")).hexdigest()
    assert document["content_sha256"] == content_sha256(
        document["product_roots"]["declared"], lock, ["dev"], [], blobs
    )

    for item in document["test_items"]:
        qualname = item["definition"]["qualname"]
        assert item["definition"]["line"] == _definition_at(
            root, sha, item["definition"]["file"], qualname
        )
