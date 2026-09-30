"""#603 B2a: origin, clone at product_sha, blobs, the declared env, the child env (§3.3, §3.6)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spec_runner import criteria_config, criteria_process
from spec_runner.criteria_config import ProductCriteria, read_product_criteria, resolve_roots
from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_process import Deadline, Finished, c_locale_env
from spec_runner.criteria_workspace import (
    Environment,
    check_origin,
    child_env,
    clone_at,
    distribution_args,
    read_blobs,
    reset_checkout,
    sync_environment,
    tracked_changes,
)

REAL_RUN = criteria_process.run_bounded


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


def _commit(root: Path, message: str = "c") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _dl() -> Deadline:
    return Deadline(120)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    _init(root)
    (root / "a.py").write_text("x = 1\n")
    _commit(root, "one")
    return root


def _finished(**fields: Any) -> Finished:
    base: dict[str, Any] = {
        "returncode": 0,
        "stdout": b"",
        "stderr": b"",
        "pid": 1,
        "timed_out": None,
    }
    base.update(fields)
    return Finished(**base)


class TestOrigin:
    def test_matching_origin_passes(self, source: Path) -> None:
        _git(source, "remote", "add", "origin", "git@github.com:andrei-shtanakov/devtools.git")
        check_origin(source, "devtools", _dl())
        check_origin(source, "andrei-shtanakov/devtools", _dl())

    @pytest.mark.parametrize("requested", ["spec-runner", "other/devtools"])
    def test_mismatch_is_retryable(self, source: Path, requested: str) -> None:
        _git(source, "remote", "add", "origin", "https://github.com/andrei-shtanakov/devtools")
        with pytest.raises(CriteriaError) as raised:
            check_origin(source, requested, _dl())
        assert raised.value.kind is ErrorKind.OWNER_REPO_MISMATCH

    def test_no_origin_is_a_mismatch(self, source: Path) -> None:
        with pytest.raises(CriteriaError) as raised:
            check_origin(source, "devtools", _dl())
        assert raised.value.kind is ErrorKind.OWNER_REPO_MISMATCH

    def test_global_expiry_is_timeout(self, source: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            criteria_process,
            "run_bounded",
            lambda *a, **k: _finished(returncode=None, timed_out="global"),
        )
        with pytest.raises(CriteriaError) as raised:
            check_origin(source, "devtools", _dl())
        assert raised.value.kind is ErrorKind.TIMEOUT


class TestClone:
    def test_clone_resolves_to_exactly_the_sha(self, source: Path, tmp_path: Path) -> None:
        first = _git(source, "rev-parse", "HEAD")
        (source / "a.py").write_text("x = 2\n")
        _git(source, "commit", "-qam", "two")
        checkout = clone_at(source, first, tmp_path / "ws", _dl())
        assert _git(checkout, "rev-parse", "HEAD") == first
        assert (checkout / "a.py").read_text() == "x = 1\n"
        assert _git(checkout, "status", "--porcelain") == ""

    def test_unknown_sha_is_product_sha_absent(self, source: Path, tmp_path: Path) -> None:
        with pytest.raises(CriteriaError) as raised:
            clone_at(source, "f" * 40, tmp_path / "ws", _dl())
        assert raised.value.kind is ErrorKind.PRODUCT_SHA_ABSENT

    def test_global_expiry_is_timeout(self, source: Path, tmp_path: Path, monkeypatch) -> None:
        sha = _git(source, "rev-parse", "HEAD")
        monkeypatch.setattr(
            criteria_process,
            "run_bounded",
            lambda *a, **k: _finished(returncode=None, timed_out="global"),
        )
        with pytest.raises(CriteriaError) as raised:
            clone_at(source, sha, tmp_path / "ws", _dl())
        assert raised.value.kind is ErrorKind.TIMEOUT


# Every byte shape a line-oriented or text-mode reader would damage.
SPECIAL_BLOBS: dict[str, bytes] = {
    "crlf.py": b"a = 1\r\nb = 2\r\n",
    "lone_cr.py": b"a = 1\rb = 2\r",
    "utf8.py": "s = 'ж€𝄞'\n".encode(),
    "invalid.py": b"s = b'\xff\xfe\x80'\n",
    "nul.bin": b"a\x00b\x00\n",
    "no_newline.py": b"x = 1",
    "empty.py": b"",
    "dir with space/f.py": b"\n\n",
}


@pytest.fixture
def blobs_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "blobs"
    _init(root)
    (root / ".gitattributes").write_bytes(b"* -text\n")
    for rel, data in SPECIAL_BLOBS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root, _commit(root)


class TestReadBlobs:
    def test_committed_bytes_not_the_working_tree(self, source: Path) -> None:
        sha = _git(source, "rev-parse", "HEAD")
        (source / "a.py").write_text("x = 999\n")
        assert read_blobs(source, sha, ["a.py"], _dl()) == {"a.py": b"x = 1\n"}

    def test_absent_path_is_clone_failed(self, source: Path) -> None:
        sha = _git(source, "rev-parse", "HEAD")
        with pytest.raises(CriteriaError) as raised:
            read_blobs(source, sha, ["a.py", "nope.py"], _dl())
        assert raised.value.kind is ErrorKind.CLONE_FAILED
        assert "nope.py" in raised.value.detail

    def test_one_batch_keeps_every_byte(self, blobs_repo, monkeypatch) -> None:
        root, sha = blobs_repo
        calls: list[list[str]] = []

        def counting(argv, **kwargs):
            calls.append(list(argv))
            return REAL_RUN(argv, **kwargs)

        monkeypatch.setattr(criteria_process, "run_bounded", counting)
        got = read_blobs(root, sha, list(SPECIAL_BLOBS), _dl())
        assert len(calls) == 1 and calls[0][:3] == ["git", "cat-file", "--batch"]
        assert got == SPECIAL_BLOBS
        for rel, data in got.items():
            shown = _git_bytes(root, "show", f"{sha}:{rel}")
            assert hashlib.sha256(data).hexdigest() == hashlib.sha256(shown).hexdigest()

    def test_empty_request_runs_nothing(self, source: Path, monkeypatch) -> None:
        monkeypatch.setattr(criteria_process, "run_bounded", lambda *a, **k: pytest.fail("ran"))
        assert read_blobs(source, "0" * 40, [], _dl()) == {}

    @pytest.mark.parametrize(
        "damage",
        [
            lambda out: out[:-1],  # the final framing newline lost
            lambda out: out[:-5],  # the payload cut short
            lambda out: out.split(b"\n", 1)[0],  # header only
            lambda out: b"",  # nothing at all
            lambda out: out.replace(b" blob ", b" blob x", 1),  # malformed size
            lambda out: out + b"extra",  # trailing garbage
        ],
    )
    def test_truncated_or_malformed_output_is_clone_failed(
        self, blobs_repo, monkeypatch, damage: Callable[[bytes], bytes]
    ) -> None:
        root, sha = blobs_repo

        def damaged(argv, **kwargs):
            done = REAL_RUN(argv, **kwargs)
            return _finished(stdout=damage(done.stdout))

        monkeypatch.setattr(criteria_process, "run_bounded", damaged)
        with pytest.raises(CriteriaError) as raised:
            read_blobs(root, sha, ["crlf.py", "no_newline.py"], _dl())
        assert raised.value.kind is ErrorKind.CLONE_FAILED

    def test_a_path_with_a_newline_is_refused(self, source: Path) -> None:
        sha = _git(source, "rev-parse", "HEAD")
        with pytest.raises(CriteriaError) as raised:
            read_blobs(source, sha, ["a\n.py"], _dl())
        assert raised.value.kind is ErrorKind.CLONE_FAILED

    def test_a_directory_is_not_a_blob(self, blobs_repo) -> None:
        root, sha = blobs_repo
        with pytest.raises(CriteriaError) as raised:
            read_blobs(root, sha, ["dir with space"], _dl())
        assert raised.value.kind is ErrorKind.CLONE_FAILED


class TestResetAndChanges:
    def test_reset_restores_tracked_and_removes_everything_else(
        self, source: Path, tmp_path: Path
    ) -> None:
        (source / ".gitignore").write_text("ignored/\n")
        sha = _commit(source)
        checkout = clone_at(source, sha, tmp_path / "ws", _dl())
        (checkout / "a.py").write_text("mutated\n")
        (checkout / "new.py").write_text("")
        (checkout / "ignored").mkdir()
        (checkout / "ignored" / "cache.pyc").write_bytes(b"x")
        (checkout / "nested").mkdir()
        _init(checkout / "nested")  # clean -ffdx removes nested repositories too
        assert tracked_changes(checkout, _dl()) == ["a.py"]
        reset_checkout(checkout, sha, _dl())
        assert (checkout / "a.py").read_text() == "x = 1\n"
        assert not (checkout / "new.py").exists()
        assert not (checkout / "ignored").exists()
        assert not (checkout / "nested").exists()
        assert tracked_changes(checkout, _dl()) == []

    def test_tracked_changes_ignores_untracked_and_reports_deletions(self, source: Path) -> None:
        (source / "b.py").write_text("")
        _commit(source)
        (source / "untracked.py").write_text("")
        (source / "b.py").unlink()
        (source / "a.py").write_text("changed\n")
        assert tracked_changes(source, _dl()) == ["a.py", "b.py"]


class TestChildEnv:
    def test_python_pytest_and_virtualenv_vars_removed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PYTHONPATH", "/elsewhere")
        monkeypatch.setenv("PYTHONHOME", "/home")
        monkeypatch.setenv("PYTHONSTARTUP", "/startup.py")
        monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
        monkeypatch.setenv("PYTEST_PLUGINS", "evil")
        monkeypatch.setenv("VIRTUAL_ENV", "/somewhere")
        env = child_env(tmp_path, {"SPEC_RUNNER_PROBE_MODE": "collect"})
        assert "PYTHONHOME" not in env and "PYTHONSTARTUP" not in env
        assert not [k for k in env if k.startswith("PYTEST_")]
        assert "VIRTUAL_ENV" not in env
        assert env["PYTHONPATH"] == str(tmp_path)
        assert env["PYTHONNOUSERSITE"] == "1"
        assert env["SPEC_RUNNER_PROBE_MODE"] == "collect"
        assert {k for k in env if k.startswith("PYTHON")} == {"PYTHONPATH", "PYTHONNOUSERSITE"}

    def test_the_rest_of_the_environment_survives(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HOME_LIKE_VAR", "kept")
        assert child_env(tmp_path, {})["HOME_LIKE_VAR"] == "kept"


def _environment(**overrides: Any) -> Environment:
    base: dict[str, Any] = {
        "python": Path("/p"),
        "implementation": "CPython",
        "version": "3.12.13",
        "lock_sha256": "0" * 64,
        "has_xdist": False,
        "groups": None,
        "extras": (),
    }
    base.update(overrides)
    return Environment(**base)


class TestEnvironment:
    def test_distribution_args(self) -> None:
        assert distribution_args(_environment(has_xdist=True)) == ["-n", "0", "--dist", "no"]
        assert distribution_args(_environment(has_xdist=False)) == []

    def test_label(self) -> None:
        assert _environment().label == "CPython 3.12.13"


class TestCLocale:
    """R10: git and uv stderr is matched by wording, so every call runs untranslated."""

    def test_c_locale_env(self, monkeypatch) -> None:
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        monkeypatch.setenv("SOMETHING_ELSE", "kept")
        env = c_locale_env()
        assert env["LC_ALL"] == "C" and env["LANG"] == "C"
        assert env["SOMETHING_ELSE"] == "kept"
        assert os.environ["LC_ALL"] == "de_DE.UTF-8"  # a copy, not the process env

    def test_every_git_call_runs_in_the_c_locale(self, source: Path, tmp_path, monkeypatch):
        seen: list[tuple[list[str], Any]] = []

        def recording(argv, **kwargs):
            seen.append((list(argv), kwargs.get("env")))
            return REAL_RUN(argv, **kwargs)

        monkeypatch.setattr(criteria_process, "run_bounded", recording)
        monkeypatch.setattr(criteria_config, "run_bounded", recording)
        (source / "pkg").mkdir()
        (source / "pkg" / "m.py").write_text("")
        (source / "spec-runner.config.yaml").write_text("criteria:\n  product_roots: [pkg]\n")
        (source / "pyproject.toml").write_text(
            '[project]\nname = "p"\n[dependency-groups]\nt = []\n'
        )
        sha = _commit(source)
        _git(source, "remote", "add", "origin", "git@github.com:o/devtools.git")
        check_origin(source, "devtools", _dl())
        checkout = clone_at(source, sha, tmp_path / "ws", _dl())
        read_blobs(checkout, sha, ["a.py"], _dl())
        reset_checkout(checkout, sha, _dl())
        tracked_changes(checkout, _dl())
        read_product_criteria(checkout, sha, _dl())
        resolve_roots(checkout, sha, ["pkg"], _dl())
        criteria_config.check_selection(checkout, sha, ProductCriteria(("pkg",), ("t",), ()), _dl())
        git_calls = [(argv, env) for argv, env in seen if argv[0] == "git"]
        assert len(git_calls) >= 9
        for argv, env in git_calls:
            assert env is not None, argv
            assert env["LC_ALL"] == "C" and env["LANG"] == "C", argv


def _locked_checkout(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "uv.lock").write_text("version = 1\n")
    return root


class _FakeRun:
    """Answers uv and the interpreter checks; everything else runs for real."""

    def __init__(
        self,
        uv: Finished | None = None,
        interpreter: bytes = b'["CPython", "3.12.13", false]',
        pytest_rc: int = 0,
    ) -> None:
        self.uv = uv or _finished()
        self.interpreter = interpreter
        self.pytest_rc = pytest_rc
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[0] == "uv":
            return self.uv
        if argv[-1] == "import pytest":
            return _finished(returncode=self.pytest_rc, stderr=b"No module named 'pytest'")
        if argv[0].endswith("python"):
            return _finished(stdout=self.interpreter)
        return REAL_RUN(argv, **kwargs)

    def argv_of(self, first: str) -> list[str]:
        return next(argv for argv, _ in self.calls if argv[0].endswith(first))


UNDECLARED = ProductCriteria(roots=("pkg",), groups=None, extras=())


class TestSyncMapping:
    """The mapping of uv's and the interpreter's answers, without running uv."""

    def test_global_deadline_during_sync_is_timeout(self, tmp_path: Path, monkeypatch) -> None:
        checkout = _locked_checkout(tmp_path / "co")
        deadline = _dl()
        received: list[Deadline] = []

        def expired(argv, **kwargs):
            received.append(kwargs["deadline"])
            return _finished(returncode=None, timed_out="global")

        monkeypatch.setattr(criteria_process, "run_bounded", expired)
        with pytest.raises(CriteriaError) as raised:
            sync_environment(checkout, "0" * 40, tmp_path / "env", UNDECLARED, deadline)
        assert raised.value.kind is ErrorKind.TIMEOUT
        assert received and all(d is deadline for d in received)

    @pytest.mark.parametrize(
        ("stderr", "kind"),
        [
            (
                b"The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.",
                ErrorKind.LOCK_NOT_CURRENT,
            ),
            (
                b"error: Groups `a` and `b` are incompatible with the conflicts: {`p:a`, `p:b`}",
                ErrorKind.ENVIRONMENT_SELECTION_INVALID,
            ),
            # the pre-check owns undefined names; uv's own wording is no longer read
            (
                b"error: Group `x` is not defined in the project's `dependency-groups` table",
                ErrorKind.ENVIRONMENT_SYNC_FAILED,
            ),
            (b"error: Failed to fetch: network unreachable", ErrorKind.ENVIRONMENT_SYNC_FAILED),
        ],
    )
    def test_uv_refusals(self, tmp_path: Path, monkeypatch, stderr: bytes, kind) -> None:
        fake = _FakeRun(uv=_finished(returncode=1, stderr=stderr))
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        with pytest.raises(CriteriaError) as raised:
            sync_environment(
                _locked_checkout(tmp_path / "co"), "0" * 40, tmp_path / "e", UNDECLARED, _dl()
            )
        assert raised.value.kind is kind

    def test_local_uv_timeout_is_sync_failed(self, tmp_path: Path, monkeypatch) -> None:
        fake = _FakeRun(uv=_finished(returncode=None, timed_out="local"))
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        with pytest.raises(CriteriaError) as raised:
            sync_environment(
                _locked_checkout(tmp_path / "co"), "0" * 40, tmp_path / "e", UNDECLARED, _dl()
            )
        assert raised.value.kind is ErrorKind.ENVIRONMENT_SYNC_FAILED

    def test_missing_lock_before_uv_runs(self, tmp_path: Path, monkeypatch) -> None:
        fake = _FakeRun()
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        (tmp_path / "co").mkdir()
        with pytest.raises(CriteriaError) as raised:
            sync_environment(tmp_path / "co", "0" * 40, tmp_path / "e", UNDECLARED, _dl())
        assert raised.value.kind is ErrorKind.LOCK_NOT_CURRENT
        assert not fake.calls

    def test_undeclared_selection_argv_and_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        fake = _FakeRun()
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        env_dir = tmp_path / "e"
        checkout = _locked_checkout(tmp_path / "co")
        result = sync_environment(checkout, "0" * 40, env_dir, UNDECLARED, _dl())
        uv_argv, uv_kwargs = next((a, k) for a, k in fake.calls if a[0] == "uv")
        assert uv_argv == ["uv", "sync", "--locked"]
        assert uv_kwargs["cwd"] == checkout
        uv_env = uv_kwargs["env"]
        assert uv_env["UV_PROJECT_ENVIRONMENT"] == str(env_dir)
        assert uv_env["LC_ALL"] == "C" and uv_env["LANG"] == "C"
        assert not [k for k in uv_env if k.startswith("PYTEST_")]
        assert result.groups is None and result.extras == ()
        assert result.python == env_dir / "bin" / "python"
        assert result.lock_sha256 == hashlib.sha256(b"version = 1\n").hexdigest()
        assert result.label == "CPython 3.12.13" and not result.has_xdist

    def test_declared_selection_argv(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "co"
        _init(root)
        (root / "uv.lock").write_text("version = 1\n")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "p"\n[project.optional-dependencies]\ncli = []\n'
            "[dependency-groups]\ntest = []\ngov-x = []\n"
        )
        sha = _commit(root)
        fake = _FakeRun()
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        criteria = ProductCriteria(("pkg",), ("gov-x", "test"), ("cli",))
        result = sync_environment(root, sha, tmp_path / "e", criteria, _dl())
        assert fake.argv_of("uv") == [
            "uv", "sync", "--locked", "--no-default-groups",
            "--group", "gov-x", "--group", "test", "--extra", "cli",
        ]  # fmt: skip
        assert result.groups == ("gov-x", "test") and result.extras == ("cli",)

    def test_empty_groups_means_no_group(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "co"
        _init(root)
        (root / "uv.lock").write_text("version = 1\n")
        (root / "pyproject.toml").write_text('[project]\nname = "p"\n')
        sha = _commit(root)
        fake = _FakeRun()
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        result = sync_environment(root, sha, tmp_path / "e", ProductCriteria(("p",), (), ()), _dl())
        assert fake.argv_of("uv") == ["uv", "sync", "--locked", "--no-default-groups"]
        assert result.groups == ()

    def test_interpreter_checks_run_with_the_child_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PYTHONPATH", "/elsewhere")
        monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
        fake = _FakeRun()
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        env_dir = tmp_path / "e"
        sync_environment(_locked_checkout(tmp_path / "co"), "0" * 40, env_dir, UNDECLARED, _dl())
        python_calls = [(a, k) for a, k in fake.calls if a[0] != "uv"]
        assert len(python_calls) == 2
        assert python_calls[1][0][-3:] == ["-P", "-c", "import pytest"]
        for argv, kwargs in python_calls:
            assert argv[0] == str(env_dir / "bin" / "python") and argv[1] == "-P"
            env = kwargs["env"]
            assert env["PYTHONNOUSERSITE"] == "1" and env["PYTHONPATH"] != "/elsewhere"
            assert not [k for k in env if k.startswith("PYTEST_")]

    @pytest.mark.parametrize(
        "answer",
        [b'["PyPy", "3.12.1", false]', b'["CPython", "3.11.9", false]'],
    )
    def test_unsupported_runtime(self, tmp_path: Path, monkeypatch, answer: bytes) -> None:
        monkeypatch.setattr(criteria_process, "run_bounded", _FakeRun(interpreter=answer))
        with pytest.raises(CriteriaError) as raised:
            sync_environment(
                _locked_checkout(tmp_path / "co"), "0" * 40, tmp_path / "e", UNDECLARED, _dl()
            )
        assert raised.value.kind is ErrorKind.UNSUPPORTED_RUNTIME

    def test_unreadable_interpreter_answer_is_sync_failed(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(criteria_process, "run_bounded", _FakeRun(interpreter=b"garbage"))
        with pytest.raises(CriteriaError) as raised:
            sync_environment(
                _locked_checkout(tmp_path / "co"), "0" * 40, tmp_path / "e", UNDECLARED, _dl()
            )
        assert raised.value.kind is ErrorKind.ENVIRONMENT_SYNC_FAILED

    def test_pytest_not_importable_is_selection_invalid(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(criteria_process, "run_bounded", _FakeRun(pytest_rc=1))
        with pytest.raises(CriteriaError) as raised:
            sync_environment(
                _locked_checkout(tmp_path / "co"), "0" * 40, tmp_path / "e", UNDECLARED, _dl()
            )
        assert raised.value.kind is ErrorKind.ENVIRONMENT_SELECTION_INVALID

    def test_undefined_group_refused_before_uv(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "co"
        _init(root)
        (root / "uv.lock").write_text("version = 1\n")
        (root / "pyproject.toml").write_text('[project]\nname = "p"\n')
        sha = _commit(root)
        fake = _FakeRun()
        monkeypatch.setattr(criteria_process, "run_bounded", fake)
        with pytest.raises(CriteriaError) as raised:
            sync_environment(root, sha, tmp_path / "e", ProductCriteria(("p",), ("x",), ()), _dl())
        assert raised.value.kind is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        assert not [a for a, _ in fake.calls if a[0] == "uv"]


PYPROJECT = (
    '[project]\nname = "p"\nversion = "0"\nrequires-python = ">=3.12"\ndependencies = []\n'
    "{extra}[tool.uv]\npackage = false\n{uv_extra}"
)


@pytest.mark.slow
class TestSync:
    """Real `uv sync --locked` on dependency-free projects, offline from the uv cache."""

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch) -> None:
        monkeypatch.setenv("UV_OFFLINE", "1")

    def _project(
        self, root: Path, extra: str = "", uv_extra: str = "", lock: bool = True
    ) -> tuple[Path, str]:
        _init(root)
        (root / "pyproject.toml").write_text(PYPROJECT.format(extra=extra, uv_extra=uv_extra))
        if lock:
            subprocess.run(["uv", "lock", "-q", "--offline"], cwd=root, check=True)
        return root, _commit(root)

    def _sync(self, checkout: Path, sha: str, env_dir: Path, groups=None, extras=()):
        criteria = ProductCriteria(("p",), groups, extras)
        return sync_environment(checkout, sha, env_dir, criteria, Deadline(300))

    def _kind(self, call: Callable[[], object], says: str = "") -> ErrorKind:
        with pytest.raises(CriteriaError) as raised:
            call()
        assert says in raised.value.detail
        return raised.value.kind

    def test_missing_lock_is_lock_not_current(self, tmp_path: Path) -> None:
        root, sha = self._project(tmp_path / "co", lock=False)
        kind = self._kind(lambda: self._sync(root, sha, tmp_path / "env"))
        assert kind is ErrorKind.LOCK_NOT_CURRENT

    def test_stale_lock_is_lock_not_current(self, tmp_path: Path) -> None:
        # a version bump makes a virtual project's lock stale without any network
        root, _ = self._project(tmp_path / "co")
        text = (root / "pyproject.toml").read_text()
        (root / "pyproject.toml").write_text(text.replace('version = "0"', 'version = "1"'))
        sha = _commit(root)
        kind = self._kind(
            lambda: self._sync(root, sha, tmp_path / "env"), "`--locked` was provided"
        )
        assert kind is ErrorKind.LOCK_NOT_CURRENT

    def test_undefined_group_is_selection_invalid(self, tmp_path: Path) -> None:
        root, sha = self._project(tmp_path / "co")
        kind = self._kind(
            lambda: self._sync(root, sha, tmp_path / "env", groups=("nope",)), "group nope"
        )
        assert kind is ErrorKind.ENVIRONMENT_SELECTION_INVALID

    def test_conflicting_pair_is_selection_invalid(self, tmp_path: Path) -> None:
        root, sha = self._project(
            tmp_path / "co",
            extra="[dependency-groups]\na = []\nb = []\n",
            uv_extra='conflicts = [[{group = "a"}, {group = "b"}]]\n',
        )
        kind = self._kind(
            lambda: self._sync(root, sha, tmp_path / "env", groups=("a", "b")), "incompatible"
        )
        assert kind is ErrorKind.ENVIRONMENT_SELECTION_INVALID

    def test_declared_group_installs_and_is_reported(self, tmp_path: Path) -> None:
        root, sha = self._project(
            tmp_path / "co", extra='[dependency-groups]\ntest = ["pytest"]\nother = []\n'
        )
        env_dir = tmp_path / "env"
        env = self._sync(root, sha, env_dir, groups=("test",))
        assert env.groups == ("test",) and env.extras == ()
        assert env.python == env_dir / "bin" / "python" and env.implementation == "CPython"
        assert tuple(int(p) for p in env.version.split(".")[:2]) >= (3, 12)
        assert env.lock_sha256 == hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
        assert not env.has_xdist
        assert not (root / ".venv").exists()  # the environment lives outside the checkout
        assert tracked_changes(root, _dl()) == []

    def test_pytest_only_in_an_unselected_group(self, tmp_path: Path) -> None:
        # R4: `groups: []` selects no group, so pytest (only in `test`) is not importable
        root, sha = self._project(tmp_path / "co", extra='[dependency-groups]\ntest = ["pytest"]\n')
        kind = self._kind(
            lambda: self._sync(root, sha, tmp_path / "env-none", groups=()),
            "pytest is not importable",
        )
        assert kind is ErrorKind.ENVIRONMENT_SELECTION_INVALID
        env = self._sync(root, sha, tmp_path / "env-test", groups=("test",))
        assert env.groups == ("test",)
