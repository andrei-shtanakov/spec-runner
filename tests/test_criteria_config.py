"""#603 B2a: the product's criteria config is read at product_sha, never the tree (§3.3, §3.4)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spec_runner.criteria_config import (
    ProductCriteria,
    check_overlap,
    check_selection,
    normalise_name,
    read_product_criteria,
    resolve_roots,
)
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import Deadline


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str], config: str | None) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if config is not None:
        (root / "spec-runner.config.yaml").write_text(config)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    return root, _git(root, "rev-parse", "HEAD")


FILES = {
    "pkg/__init__.py": "",
    "pkg/mod.py": "x = 1\n",
    "pkg/data.txt": "d",
    "tool.py": "",
    "tests/test_a.py": "",
}
ROOTS = "criteria:\n  product_roots: [pkg]\n"


def _dl() -> Deadline:
    return Deadline(60)


def _kind(fn: Callable[..., Any], *args: Any) -> ErrorKind:
    with pytest.raises(CriteriaError) as raised:
        fn(*args)
    return raised.value.kind


def _read(root: Path, sha: str) -> ProductCriteria:
    return read_product_criteria(root, sha, _dl())


class TestRoots:
    def test_flat_and_executor_shapes(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, "criteria:\n  product_roots: [pkg/, ./tool.py]\n")
        assert _read(root, sha).roots == ("pkg", "tool.py")
        (root / "spec-runner.config.yaml").write_text(
            "executor:\n  criteria:\n    product_roots: [pkg]\n"
        )
        _git(root, "commit", "-qam", "exec")
        assert _read(root, _git(root, "rev-parse", "HEAD")).roots == ("pkg",)

    def test_legacy_location(self, tmp_path):
        cfg = "executor:\n  criteria:\n    product_roots: [pkg]\n"
        root, sha = _repo(tmp_path, {**FILES, "spec/executor.config.yaml": cfg}, None)
        assert _read(root, sha).roots == ("pkg",)

    def test_working_tree_edit_is_not_seen(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, ROOTS)
        (root / "spec-runner.config.yaml").write_text("criteria:\n  product_roots: [tool.py]\n")
        assert _read(root, sha).roots == ("pkg",)
        (root / "spec-runner.config.yaml").unlink()
        assert _read(root, sha).roots == ("pkg",)

    def test_config_added_only_in_tree_is_undeclared(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, None)
        (root / "spec-runner.config.yaml").write_text(ROOTS)
        assert _kind(_read, root, sha) is ErrorKind.PRODUCT_ROOTS_UNDECLARED

    @pytest.mark.parametrize(
        ("config", "kind"),
        [
            (None, ErrorKind.PRODUCT_ROOTS_UNDECLARED),
            ("claude_model: sonnet\n", ErrorKind.PRODUCT_ROOTS_UNDECLARED),
            ("criteria:\n  product_roots: []\n", ErrorKind.PRODUCT_ROOTS_EMPTY),
            ("criteria:\n  product_roots: pkg\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: [/abs]\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: [../up]\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: [pkg, ./pkg/]\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: ['.']\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria: [\n", ErrorKind.PRODUCT_ROOTS_INVALID),
        ],
    )
    def test_refusals(self, tmp_path, config, kind):
        root, sha = _repo(tmp_path, FILES, config)
        assert _kind(_read, root, sha) is kind

    @pytest.mark.parametrize("root_spec", [":", ":(top)", ":(glob)**/*.py", ":!x", ":(icase)PKG"])
    def test_pathspec_magic_root_is_invalid(self, tmp_path, root_spec):
        """A root is a path, never a git pathspec: `:` / `:(top)` would list the repo."""
        config = f"criteria:\n  product_roots: [{root_spec!r}]\n"
        root, sha = _repo(tmp_path, FILES, config)
        assert _kind(_read, root, sha) is ErrorKind.PRODUCT_ROOTS_INVALID

    def test_ordinary_roots_unchanged(self, tmp_path):
        config = "criteria:\n  product_roots: [pkg, 'tool.py', 'a:b']\n"
        root, sha = _repo(tmp_path, FILES, config)
        assert _read(root, sha).roots == ("a:b", "pkg", "tool.py")


class TestEnvironment:
    def _env(self, tmp_path, env: str) -> ProductCriteria:
        body = ROOTS + "  environment:\n" + "".join(f"    {ln}\n" for ln in env.splitlines())
        root, sha = _repo(tmp_path, FILES, body)
        return _read(root, sha)

    def test_absent(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, ROOTS)
        crit = _read(root, sha)
        assert (crit.groups, crit.extras) == (None, ())

    def test_groups_only(self, tmp_path):
        crit = self._env(tmp_path, "groups: [governance]")
        assert (crit.groups, crit.extras) == (("governance",), ())

    def test_groups_and_extras_sorted(self, tmp_path):
        crit = self._env(tmp_path, "groups: [governance, dev]\nextras: [cli]")
        assert (crit.groups, crit.extras) == (("dev", "governance"), ("cli",))

    def test_empty_groups_differs_from_absent(self, tmp_path):
        assert self._env(tmp_path, "groups: []").groups == ()

    def test_spelling_is_normalised(self, tmp_path):
        assert self._env(tmp_path, "groups: [Gov_X]").groups == ("gov-x",)

    @pytest.mark.parametrize(
        "env",
        [
            "groups: governance",
            'groups: ["bad name"]',
            "groups: [a, a]",
            "groups: [gov-x, GOV.X]",
            'groups: ["x-"]',
            'groups: ["-x"]',
            "unknown: []",
            "groups: [1]",
            "extras: {a: b}",
        ],
    )
    def test_invalid(self, tmp_path, env):
        assert _kind(self._env, tmp_path, env) is ErrorKind.ENVIRONMENT_SELECTION_INVALID

    def test_environment_not_a_mapping(self, tmp_path):
        assert _kind(self._env, tmp_path, "- a") is ErrorKind.ENVIRONMENT_SELECTION_INVALID


class TestNormalise:
    @pytest.mark.parametrize(
        ("raw", "want"), [("Gov_X", "gov-x"), ("a.-_b", "a-b"), ("dev", "dev"), ("A", "a")]
    )
    def test_table(self, raw, want):
        assert normalise_name(raw) == want


class TestResolve:
    def test_directory_expands_to_tracked_python_files(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, "")
        (root / "pkg" / "untracked.py").write_text("")
        got = resolve_roots(root, sha, ["pkg", "tool.py"], _dl())
        assert got == ["pkg/__init__.py", "pkg/mod.py", "tool.py"]

    @pytest.mark.parametrize("magic", [":", ":(top)", ":(glob)**/*.py", ":!x", "pk*"])
    def test_roots_are_literal_paths_at_ls_tree(self, tmp_path, magic):
        """`--literal-pathspecs`: a magic spelling names nothing tracked, never the repo."""
        root, sha = _repo(tmp_path, FILES, "")
        assert _kind(resolve_roots, root, sha, [magic], _dl()) is ErrorKind.PRODUCT_ROOTS_INVALID

    def test_missing_root_is_invalid(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, "")
        assert _kind(resolve_roots, root, sha, ["nope"], _dl()) is ErrorKind.PRODUCT_ROOTS_INVALID

    def test_no_python_under_roots(self, tmp_path):
        root, sha = _repo(tmp_path, {**FILES, "docs/readme.md": "x"}, "")
        assert _kind(resolve_roots, root, sha, ["docs"], _dl()) is ErrorKind.PRODUCT_ROOTS_NO_PYTHON

    @pytest.mark.parametrize("declared", ["link", "pkg"])
    def test_tracked_symlink_is_invalid(self, tmp_path, declared):
        root, _ = _repo(tmp_path, FILES, "")
        (root / "link").symlink_to("/etc")
        (root / "pkg" / "escape.py").symlink_to("/etc/hosts")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "links")
        sha = _git(root, "rev-parse", "HEAD")
        assert _kind(resolve_roots, root, sha, [declared], _dl()) is ErrorKind.PRODUCT_ROOTS_INVALID


class TestOverlap:
    def test_a_test_file_under_a_root_is_refused(self):
        with pytest.raises(CriteriaError) as raised:
            check_overlap(["pkg/mod.py", "pkg/conftest.py"], ["pkg/conftest.py", "tests/test_a.py"])
        assert raised.value.kind is ErrorKind.PRODUCT_ROOTS_OVERLAP_TESTS
        assert "pkg/conftest.py" in raised.value.detail

    def test_disjoint_passes(self):
        check_overlap(["pkg/mod.py"], ["tests/test_a.py"])


PYPROJECT = """\
[project]
name = "p"
version = "0"
[project.optional-dependencies]
cli = ["click"]
[dependency-groups]
gov-x = ["a"]
"""


def _sel(groups: tuple[str, ...] | None = None, extras: tuple[str, ...] = ()) -> ProductCriteria:
    return ProductCriteria(roots=("pkg",), groups=groups, extras=extras)


class TestCheckSelection:
    def _root(self, tmp_path, pyproject: str | None = PYPROJECT) -> tuple[Path, str]:
        files = dict(FILES)
        if pyproject is not None:
            files["pyproject.toml"] = pyproject
        return _repo(tmp_path, files, ROOTS)

    def test_nothing_declared_reads_nothing(self, tmp_path):
        root, sha = self._root(tmp_path, None)
        check_selection(root, sha, _sel(), _dl())

    def test_defined_group_and_extra(self, tmp_path):
        root, sha = self._root(tmp_path)
        check_selection(root, sha, _sel(("gov-x",), ("cli",)), _dl())

    def test_undefined_group_named(self, tmp_path):
        root, sha = self._root(tmp_path)
        with pytest.raises(CriteriaError) as raised:
            check_selection(root, sha, _sel(("nope",)), _dl())
        assert raised.value.kind is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        assert "nope" in raised.value.detail

    def test_undefined_extra(self, tmp_path):
        root, sha = self._root(tmp_path)
        assert (
            _kind(check_selection, root, sha, _sel(None, ("nope",)), _dl())
            is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        )

    def test_legacy_dev_dependencies_make_dev_available(self, tmp_path):
        legacy = '[project]\nname="p"\nversion="0"\n[tool.uv]\ndev-dependencies = ["x"]\n'
        root, sha = self._root(tmp_path, legacy)
        check_selection(root, sha, _sel(("dev",)), _dl())

    def test_dev_unavailable_without_either(self, tmp_path):
        root, sha = self._root(tmp_path)
        assert (
            _kind(check_selection, root, sha, _sel(("dev",)), _dl())
            is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        )

    def test_pyproject_spelling_normalised(self, tmp_path):
        py = PYPROJECT.replace("gov-x = ", "Gov_X = ")
        root, sha = self._root(tmp_path, py)
        check_selection(root, sha, _sel(("gov-x",)), _dl())

    def test_missing_pyproject(self, tmp_path):
        root, sha = self._root(tmp_path, None)
        assert (
            _kind(check_selection, root, sha, _sel(("gov-x",)), _dl())
            is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        )

    def test_unparseable_pyproject(self, tmp_path):
        root, sha = self._root(tmp_path, "[[[ nope")
        assert (
            _kind(check_selection, root, sha, _sel(("gov-x",)), _dl())
            is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        )

    def test_working_tree_pyproject_is_not_read(self, tmp_path):
        root, sha = self._root(tmp_path)
        (root / "pyproject.toml").write_text(
            PYPROJECT.replace("gov-x", "other").replace("cli =", "other =")
        )
        check_selection(root, sha, _sel(("gov-x",), ("cli",)), _dl())
        (root / "pyproject.toml").unlink()
        check_selection(root, sha, _sel(("gov-x",), ("cli",)), _dl())


def _fake_run(monkeypatch, **fields):
    from spec_runner import criteria_config
    from spec_runner.criteria_process import Finished

    defaults = {"returncode": 0, "stdout": b"", "stderr": b"", "pid": 1, "timed_out": None}
    done = Finished(**{**defaults, **fields})
    monkeypatch.setattr(criteria_config, "run_bounded", lambda *a, **k: done)


class TestTimeouts:
    """R8: an exhausted or expired deadline is TIMEOUT, never a product property."""

    def _calls(self, tmp_path):
        root, sha = _repo(tmp_path, {**FILES, "pyproject.toml": PYPROJECT}, ROOTS)
        return {
            "read": lambda dl: read_product_criteria(root, sha, dl),
            "resolve": lambda dl: resolve_roots(root, sha, ["pkg"], dl),
            "select": lambda dl: check_selection(root, sha, _sel(("gov-x",)), dl),
        }

    @pytest.mark.parametrize("name", ["read", "resolve", "select"])
    def test_exhausted_deadline(self, tmp_path, name):
        assert _kind(self._calls(tmp_path)[name], Deadline(0)) is ErrorKind.TIMEOUT

    @pytest.mark.parametrize("name", ["read", "resolve", "select"])
    def test_global_expiry_mid_step(self, tmp_path, monkeypatch, name):
        calls = self._calls(tmp_path)
        _fake_run(monkeypatch, returncode=None, timed_out="global")
        assert _kind(calls[name], _dl()) is ErrorKind.TIMEOUT


class TestGitFailuresAreNotDeclarations:
    """R9: only 'path absent at sha' is a property of the product."""

    def _calls(self, tmp_path):
        root, sha = _repo(tmp_path, {**FILES, "pyproject.toml": PYPROJECT}, ROOTS)
        return root, sha

    @pytest.mark.parametrize("which", ["read", "resolve", "select"])
    def test_local_timeout_is_clone_failed(self, tmp_path, monkeypatch, which):
        root, sha = self._calls(tmp_path)
        _fake_run(monkeypatch, returncode=None, timed_out="local")
        call = {
            "read": lambda: read_product_criteria(root, sha, _dl()),
            "resolve": lambda: resolve_roots(root, sha, ["pkg"], _dl()),
            "select": lambda: check_selection(root, sha, _sel(("gov-x",)), _dl()),
        }[which]
        assert _kind(call) is ErrorKind.CLONE_FAILED

    def test_bad_sha_read(self, tmp_path):
        root, _ = self._calls(tmp_path)
        with pytest.raises(CriteriaError) as raised:
            read_product_criteria(root, "deadbeef", _dl())
        assert raised.value.kind is ErrorKind.CLONE_FAILED
        assert "deadbeef" in raised.value.detail

    def test_bad_sha_resolve(self, tmp_path):
        root, _ = self._calls(tmp_path)
        assert _kind(resolve_roots, root, "deadbeef", ["pkg"], _dl()) is ErrorKind.CLONE_FAILED

    def test_bad_sha_select(self, tmp_path):
        root, _ = self._calls(tmp_path)
        assert (
            _kind(check_selection, root, "deadbeef", _sel(("gov-x",)), _dl())
            is ErrorKind.CLONE_FAILED
        )

    def test_not_a_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _kind(_read, plain, "HEAD") is ErrorKind.CLONE_FAILED

    def test_absent_path_unchanged(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, None)
        assert _kind(_read, root, sha) is ErrorKind.PRODUCT_ROOTS_UNDECLARED
        assert _kind(resolve_roots, root, sha, ["nope"], _dl()) is ErrorKind.PRODUCT_ROOTS_INVALID


class TestNamesAndTables:
    def test_trailing_newline_name_refused(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, ROOTS + '  environment:\n    groups: ["x\\n"]\n')
        assert _kind(_read, root, sha) is ErrorKind.ENVIRONMENT_SELECTION_INVALID

    @pytest.mark.parametrize(
        "pyproject",
        [
            'tool = "x"\n',
            '[tool]\nuv = "x"\n',
            'project = "x"\n',
            '[project]\noptional-dependencies = "x"\n',
            'dependency-groups = "x"\n',
        ],
    )
    def test_non_table_is_invalid_selection(self, tmp_path, pyproject):
        root, sha = _repo(tmp_path, {**FILES, "pyproject.toml": pyproject}, ROOTS)
        assert (
            _kind(check_selection, root, sha, _sel(("gov-x",)), _dl())
            is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        )
