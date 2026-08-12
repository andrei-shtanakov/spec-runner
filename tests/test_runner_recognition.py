"""#198: a confirmed red requires a runner whose exit codes we have measured.

`_TESTS_FAILED = 1` is pytest's convention, and the code called it "shared by
most runners". Measured on Elixir/OTP 28 it is **inverted**: `mix test` exits 2
when tests fail and 1 when the run never happened — a nonexistent file, or a
test file that would not compile.

So on an Elixir project the ordinary path produced a *false confirmed red*.
The agent is told to emit `TDD_SELECTOR: path::test`; it complies with the
shape; `verify_red` accepted it because the only check was `"::" in selector`;
`mix test 'test/x_test.exs::name'` matched no file and exited 1; and 1 was read
as "the selector failed on replay". A checkpoint, claims and a satisfied gate
all followed, for a test that never ran.

This is the fail-closed half of the fix (owner's step 1): `expected_fail` is
reachable only for a runner recognised as pytest, and everything else is
`unverifiable` with a message that names what is missing. The richer contract —
a runner adapter that also proves the selected test actually ran — is step 2,
and until it lands even pytest rests on its exit codes alone. That is sound for
pytest specifically, because a mis-selected node id there exits 4, never 1
(measured, `test_red_checkpoint.py`), which is exactly the property ExUnit
lacks.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.tdd import RedOutcome, detect_runner, verify_red


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, test_command: str) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command=test_command,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


class TestRecognisingTheRunner:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest",
            "pytest -x",
            "python -m pytest",
            "python3 -m pytest -q",
            "uv run pytest",
            "uv run pytest tests/ -v",
            "poetry run pytest",
            "./venv/bin/pytest",
        ],
    )
    def test_pytest_is_recognised(self, command):
        assert detect_runner(command) == "pytest"

    @pytest.mark.parametrize(
        "command",
        [
            "mix test",
            "mix test --trace",
            "go test ./...",
            "npm test",
            "cargo test",
            "bundle exec rspec",
            "",
            "   ",
            "make test",
            # The trap: a word containing the substring is not the runner.
            "./scripts/run-pytest-in-docker.sh",
            "echo pytest",
        ],
    )
    def test_everything_else_is_unrecognised(self, command):
        assert detect_runner(command) is None

    def test_recognition_is_token_based_not_substring(self):
        """`"pytest" in command` would call `mix test --cover pytest-style` a
        pytest run. The check looks at what is being executed."""
        assert detect_runner("mix test --formatter PytestFormatter") is None


class TestTheReplayRefusesAnUnmeasuredRunner:
    def test_mix_test_is_refused_before_anything_runs(self, tmp_path):
        """The #198 path, at its first step. Nothing is executed: no worktree,
        no test command, no chance for an exit code to be misread."""
        root = _repo(tmp_path)
        cfg = _cfg(root, "mix test")
        result = verify_red(
            cfg,
            sha=_head(root),
            selector="test/x_test.exs::calls missing module",
            baseline_sha=_head(root),
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE

    def test_the_message_names_the_runner_the_selector_and_what_is_missing(self, tmp_path):
        """A refusal that does not say why teaches people to route around it."""
        root = _repo(tmp_path)
        cfg = _cfg(root, "mix test")
        result = verify_red(
            cfg,
            sha=_head(root),
            selector="test/x_test.exs::name",
            baseline_sha=_head(root),
        )
        detail = result.detail.lower()
        assert "mix" in detail
        assert "test/x_test.exs::name" in result.detail
        assert "exit" in detail, result.detail

    def test_a_pytest_shaped_selector_does_not_rescue_an_unknown_runner(self, tmp_path):
        """`::` was the only gate, and shape-compliance is exactly what the
        agent gives you. It must not stand in for a measured runner."""
        root = _repo(tmp_path)
        cfg = _cfg(root, "mix test")
        result = verify_red(
            cfg,
            sha=_head(root),
            selector="test/probe_test.exs::TestThing::test_name",
            baseline_sha=_head(root),
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "node id" not in result.detail, (
            "the refusal must be about the runner, not blame the selector's shape"
        )

    def test_an_empty_test_command_is_refused(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, "")
        result = verify_red(
            cfg, sha=_head(root), selector="tests/t.py::test_x", baseline_sha=_head(root)
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE

    def test_the_runner_is_checked_before_the_selector(self, tmp_path):
        """Order is the whole point: an Elixir project must hear "unknown
        runner", not "your selector is not a pytest node id" — the latter
        invites someone to reshape the selector and try again."""
        root = _repo(tmp_path)
        cfg = _cfg(root, "mix test")
        result = verify_red(
            cfg, sha=_head(root), selector="test/x_test.exs:42", baseline_sha=_head(root)
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "mix" in result.detail

    def test_a_composite_command_is_still_refused(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, "pytest && ruff check .")
        result = verify_red(
            cfg, sha=_head(root), selector="tests/t.py::test_x", baseline_sha=_head(root)
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE

    def test_no_exit_code_is_ever_classified_for_an_unknown_runner(self, tmp_path, monkeypatch):
        """The defect in one assertion: whatever the process returns, it must
        not become a verdict. Nothing may even be run."""
        import spec_runner.tdd as tdd_mod

        root = _repo(tmp_path)
        cfg = _cfg(root, "mix test")

        def _explode(*a, **k):
            raise AssertionError("the runner was executed despite being unrecognised")

        monkeypatch.setattr(tdd_mod, "_run_selector", _explode)
        result = verify_red(
            cfg, sha=_head(root), selector="test/x_test.exs::name", baseline_sha=_head(root)
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE


class TestPytestIsUnchanged:
    """The fix must not cost the one runner that does work. `expected_fail` is
    still reachable, and by the same route."""

    def test_a_real_pytest_red_is_still_confirmed(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests").mkdir()
        (root / "tests" / "test_thing.py").write_text("def test_thing():\n    assert False\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "red")
        cfg = _cfg(root, "python -m pytest")
        result = verify_red(
            cfg,
            sha=_head(root),
            selector="tests/test_thing.py::test_thing",
            baseline_sha=_git(root, "rev-parse", "HEAD~1").stdout.strip(),
        )
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail

    def test_a_non_node_id_selector_is_still_refused_for_pytest(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, "pytest")
        result = verify_red(
            cfg, sha=_head(root), selector="tests/test_thing.py", baseline_sha=_head(root)
        )
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "node id" in result.detail
