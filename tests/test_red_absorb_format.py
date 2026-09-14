"""Regression for spec-runner#507: the RED pass formats the file it freezes.

The live run behind the issue: a red file whose bytes `ruff check` accepted
but `ruff format --check .` (the post-done completion gate, #351) rejected.
The red was already claimed — byte-locked — so no GREEN attempt could ever
reformat it, and every attempt failed the same gate by construction: three
paid GREEN calls, $12.99, `on_task_failure: stop`.

`Given` a project that declared `commands.format_check` (the completion gate)
and `commands.format` (the write-mode formatter), under `execution_mode: tdd`.
`When` the RED-authoring pass produces a test that lints clean but carries
formatting drift.
`Then` the pre-freeze step repairs the drift with the declared formatter and
absorbs the result into the checkpoint commit, so the bytes that get frozen are
the bytes the completion gate accepts.
`And` with the gate declared but no formatter, the RED pass refuses BEFORE
freezing — one paid call and an actionable reason, instead of three paid GREEN
calls against a lock nobody can open.
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig, load_config_from_yaml
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

# "Formatting drift" is a `DRIFT` token; "lint finding" is a `BADWORD` token.
# Each check script exits 1 on drift/finding and 0 when clean; each fix script
# rewrites the token away. Every script records that it ran, so a test can
# assert a command was NOT invoked, not only that the outcome looks right.
_FORMAT_CHECK = """
import sys
from pathlib import Path

Path(sys.argv[1]).write_text("ran\\n")
# Explicit paths when narrowed; the whole tree (as post-done runs it) without.
paths = sys.argv[2:] or [str(p) for p in Path("tests").rglob("*.py")]
bad = any("DRIFT" in Path(p).read_text() for p in paths)
sys.exit(1 if bad else 0)
"""

# A formatter whose own configuration excludes `tests/`: tree-wide it never
# looks there (exit 0), yet an explicitly named file IS judged — ruff's
# behaviour for excluded paths passed on the command line.
_EXCLUDING_FORMAT_CHECK = """
import sys
from pathlib import Path

Path(sys.argv[1]).write_text("ran\\n")
bad = any("DRIFT" in Path(p).read_text() for p in sys.argv[2:])
sys.exit(1 if bad else 0)
"""

_FORMAT_FIX = """
import sys
from pathlib import Path

Path(sys.argv[1]).write_text("ran\\n")
for p in sys.argv[2:]:
    path = Path(p)
    path.write_text(path.read_text().replace("DRIFT", ""))
"""

_LINT_CHECK = """
import sys
from pathlib import Path

bad = any("BADWORD" in Path(p).read_text() for p in sys.argv[1:])
sys.exit(1 if bad else 0)
"""

_LINT_FIX = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", ""))
"""

_BROKEN_FORMAT_CHECK = """
import sys
sys.exit(2)
"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


class _Scripts:
    """The declared commands, each with its own "I ran" marker file."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.format_check_marker = directory / "format_check.ran"
        self.format_fix_marker = directory / "format_fix.ran"
        self.format_check = self._command(
            "format_check.py", _FORMAT_CHECK, self.format_check_marker
        )
        self.format_fix = self._command("format_fix.py", _FORMAT_FIX, self.format_fix_marker)
        self.lint_check = self._command("lint_check.py", _LINT_CHECK)
        self.lint_fix = self._command("lint_fix.py", _LINT_FIX)
        self.broken_format_check = self._command(
            "broken_format_check.py", _BROKEN_FORMAT_CHECK, self.format_check_marker
        )
        self.excluding_format_check = self._command(
            "excluding_format_check.py", _EXCLUDING_FORMAT_CHECK, self.format_check_marker
        )

    def _command(self, name: str, body: str, marker: Path | None = None) -> str:
        script = self.directory / name
        script.write_text(body)
        parts = [shlex.quote(sys.executable), shlex.quote(str(script))]
        if marker is not None:
            parts.append(shlex.quote(str(marker)))
        return " ".join(parts)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    # No linter declared unless a test says so: the format step must not
    # depend on the lint step having run.
    fields = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
        "lint_command": "",
        "lint_command_declared": False,
    }
    fields.update(overrides)
    cfg = ExecutorConfig(**fields)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


_RED_WITH_DRIFT = "def test_y():  # DRIFT\n    assert False\n"
_RED_CLEAN = "def test_y():\n    assert False\n"
_RED_WITH_BOTH = "def test_y():  # DRIFT BADWORD\n    assert False\n"


def _agent_writing(monkeypatch, calls: list, body: str) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        calls.append("red")
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


def _committed(root: Path, sha: str, path: str = "tests/test_x.py") -> str:
    return subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=root, capture_output=True, text=True, check=True
    ).stdout


class TestDriftIsRepairedBeforeTheFreeze:
    def test_the_checkpoint_holds_bytes_the_completion_gate_accepts(
        self, tmp_path_factory, monkeypatch
    ):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(
            root,
            format_check_command=scripts.format_check,
            format_command=scripts.format_fix,
            format_command_declared=True,
        )
        calls: list = []
        _agent_writing(monkeypatch, calls, _RED_WITH_DRIFT)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        assert [c.path for c in claims] == ["tests/test_x.py"]
        assert calls == ["red"]

        # The frozen bytes are the formatted bytes — in the commit, not only
        # in the working tree (the claim byte-locks the commit's bytes).
        committed = _committed(root, result.checkpoint.commit_sha)
        assert "DRIFT" not in committed
        assert committed == (root / "tests/test_x.py").read_text()
        assert result.checkpoint.commit_sha == _git(root, "rev-parse", "HEAD").stdout.strip()

        # And the completion gate, run the way post-done runs it — the whole
        # tree — is clean on that tree. This is the property the live run
        # lacked: every GREEN attempt failed here, against a locked file.
        gate = subprocess.run(
            f"{scripts.format_check} tests/test_x.py",
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert gate.returncode == 0, gate.stdout + gate.stderr

        # The amend kept the authored subject — the remainder stays adoptable
        # (#261), exactly as the lint fix's absorb promises.
        subject = _git(root, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "TASK-001: red for tests/test_x.py::test_y"

    def test_a_lint_fix_and_a_format_fix_ride_the_same_commit(self, tmp_path_factory, monkeypatch):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(
            root,
            lint_command=scripts.lint_check,
            lint_command_declared=True,
            lint_fix_command=scripts.lint_fix,
            lint_fix_command_declared=True,
            format_check_command=scripts.format_check,
            format_command=scripts.format_fix,
            format_command_declared=True,
        )
        _agent_writing(monkeypatch, [], _RED_WITH_BOTH)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        committed = _committed(root, result.checkpoint.commit_sha)
        assert "BADWORD" not in committed
        assert "DRIFT" not in committed
        # One candidate commit on top of the baseline, both repairs inside it.
        count = _git(root, "rev-list", "--count", "main").stdout.strip()
        assert count == "2"


class TestNothingRunsWhenNothingIsNeeded:
    def test_a_clean_red_never_invokes_the_formatter(self, tmp_path_factory, monkeypatch):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(
            root,
            format_check_command=scripts.format_check,
            format_command=scripts.format_fix,
            format_command_declared=True,
        )
        _agent_writing(monkeypatch, [], _RED_CLEAN)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert scripts.format_check_marker.exists()
        assert not scripts.format_fix_marker.exists()

    def test_the_step_is_dormant_when_the_completion_gate_is_off(
        self, tmp_path_factory, monkeypatch
    ):
        """`format_check` follows `hooks.post_done.run_lint` (#351). With the
        gate off, no GREEN would ever fail on formatting, so pre-freeze has
        nothing to protect — and must not refuse a red over it."""
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(
            root,
            run_lint_on_done=False,
            format_check_command=scripts.format_check,
            format_command=scripts.format_fix,
            format_command_declared=True,
        )
        _agent_writing(monkeypatch, [], _RED_WITH_DRIFT)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert not scripts.format_check_marker.exists()
        assert "DRIFT" in _committed(root, result.checkpoint.commit_sha)


class TestWithoutADeclaredFormatterTheRedIsRefusedBeforeItFreezes:
    def test_the_refusal_names_the_missing_declaration_and_locks_nothing(
        self, tmp_path_factory, monkeypatch
    ):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(root, format_check_command=scripts.format_check)
        _agent_writing(monkeypatch, [], _RED_WITH_DRIFT)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        # A verdict about the work, not an instrument failure: the gate would
        # block this file and nothing declared can repair it.
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.instrument_error is False
        assert result.checkpoint is None
        assert claims == []
        assert "commands.format" in result.detail
        assert "format" in result.detail.lower()
        # The formatter was never guessed at — there is none to run.
        assert not scripts.format_fix_marker.exists()

    def test_a_declared_but_unnarrowable_formatter_gets_the_right_advice(
        self, tmp_path_factory, monkeypatch
    ):
        """A formatter that names its own existing path (`ruff format src tests`)
        cannot be narrowed to the claim and is not run; the refusal must not
        tell the operator to declare a key they already declared."""
        root = _repo(tmp_path_factory.mktemp("proj"))
        (root / "tests").mkdir()
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(
            root,
            format_check_command=scripts.format_check,
            format_command=f"{scripts.format_fix} tests",
            format_command_declared=True,
        )
        _agent_writing(monkeypatch, [], _RED_WITH_DRIFT)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.checkpoint is None
        assert not scripts.format_fix_marker.exists()
        assert "could not be narrowed" in result.detail
        assert "narrowable" in result.detail
        assert "declare commands.format" not in result.detail

    def test_a_formatter_that_excludes_the_file_does_not_refuse(
        self, tmp_path_factory, monkeypatch
    ):
        """The narrowed check judges the explicit file; the tree-wide gate
        post-done runs would skip it. The gate is what matters."""
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(root, format_check_command=scripts.excluding_format_check)
        _agent_writing(monkeypatch, [], _RED_WITH_DRIFT)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert "DRIFT" in _committed(root, result.checkpoint.commit_sha)

    def test_a_non_blocking_gate_only_warns(self, tmp_path_factory, monkeypatch):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(root, lint_blocking=False, format_check_command=scripts.format_check)
        _agent_writing(monkeypatch, [], _RED_WITH_DRIFT)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        # Post-done would only warn, so pre-freeze does the same.
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail


class TestTheCheckerItselfBreaking:
    def test_an_exit_outside_the_contract_is_an_instrument_error(
        self, tmp_path_factory, monkeypatch
    ):
        """Exit 0 is clean, 1 is measured drift; anything else is the tool
        failing (#351's contract, `format_check_instrument_error`)."""
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = _Scripts(tmp_path_factory.mktemp("scripts"))
        cfg = _cfg(
            root,
            format_check_command=scripts.broken_format_check,
            format_command=scripts.format_fix,
            format_command_declared=True,
        )
        _agent_writing(monkeypatch, [], _RED_CLEAN)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.instrument_error is True
        assert result.checkpoint is None
        assert not scripts.format_fix_marker.exists()


class TestTheLoaderReadsTheDeclaration:
    def test_commands_format_is_declared_only_when_present(self, tmp_path):
        (tmp_path / "spec-runner.config.yaml").write_text(
            "commands:\n  format_check: ruff format --check .\n  format: ruff format .\n"
        )
        loaded = load_config_from_yaml(tmp_path / "spec-runner.config.yaml")
        assert loaded["format_command"] == "ruff format ."
        assert loaded["format_command_declared"] is True

        (tmp_path / "bare.yaml").write_text("commands:\n  format_check: ruff format --check .\n")
        bare = load_config_from_yaml(tmp_path / "bare.yaml")
        assert bare["format_command"] is None
        assert bare["format_command_declared"] is False

    def test_the_dataclass_default_is_no_formatter(self):
        cfg = ExecutorConfig()
        assert cfg.format_command == ""
        assert cfg.format_command_declared is False
