"""#282: the RED authoring prompt was written nowhere.

Found while rehearsing the published v2.33.0: I wanted to confirm that the RED
prompt names the adapter's file (#252 D) and discovered there is nothing to
read. The implementation pass has logged its prompt for a long time; the RED
pass — a **paid** call whose output becomes the checkpoint's selector, and so
decides which file is frozen for the rest of the task — logged nothing.

Three ways that bites, all of them already visible in this repo's history:

- a published artifact cannot be checked against its own prompt, which is
  exactly what the rehearsal was for;
- a wrong prompt is invisible afterwards. #198 (a pytest-shaped selector asked
  of an ExUnit project) and #220 (a Python linter applied to an Elixir file)
  were both caught by *reading the code*; from a run's logs they would not have
  been recoverable;
- the ledger records `red_authoring` with its cost, and what the money bought
  was gone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import run_red_phase

FAILING = "def test_new_behaviour():\n    assert False\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> ExecutorConfig:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _agent(monkeypatch):
    from spec_runner import tdd

    def _red(config, prompt, **kwargs):
        target = Path(config.project_root) / "tests" / "test_task_104_red.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAILING)
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_task_104_red.py::test_new_behaviour")

    monkeypatch.setattr(tdd, "_run_agent", _red)


def _red_logs(cfg: ExecutorConfig) -> list[Path]:
    return sorted(cfg.logs_dir.glob("*-red-*.log"))


@pytest.mark.slow
class TestThePromptIsOnDisk:
    def test_a_red_pass_leaves_its_prompt(self, tmp_path, monkeypatch):
        cfg = _repo(tmp_path)
        _agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        logs = _red_logs(cfg)
        assert len(logs) == 1
        assert "=== RED PROMPT ===" in logs[0].read_text()

    def test_it_holds_what_the_agent_was_actually_asked(self, tmp_path, monkeypatch):
        """Not a paraphrase: the point is to be able to answer "why did it
        write that" afterwards, and #198/#220 were both wrong *instructions*."""
        cfg = _repo(tmp_path)
        _agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        body = _red_logs(cfg)[0].read_text()
        assert "new file that does not exist yet" in body, "#252's rule"
        assert "tests/test_task_104_red.py" in body, "the adapter's own path"
        assert "TDD_SELECTOR" in body, "the marker contract the selector depends on"

    def test_the_file_names_the_task(self, tmp_path, monkeypatch):
        """A workstream runs many tasks; a log nobody can attribute is close to
        no log at all."""
        cfg = _repo(tmp_path)
        _agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        assert _red_logs(cfg)[0].name.startswith("TASK-104-red-")

    def test_a_second_authoring_pass_does_not_overwrite_the_first(self, tmp_path, monkeypatch):
        """Two authoring passes asked two different things, and keeping only
        the last would lose the one that explains how the task got here.

        The first red is abandoned between them: without that the phase reuses
        the confirmed checkpoint and makes no call at all (F-4), which is the
        next test.
        """
        import time

        from spec_runner.remedy import abandon

        cfg = _repo(tmp_path)
        _agent(monkeypatch)

        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)
            assert first.checkpoint is not None
            abandon(cfg, state, "TASK-104", first.checkpoint.checkpoint_id, reason="wrong test")
            time.sleep(1.05)  # the name is stamped to the second
            run_red_phase(_task(), cfg, state)

        assert len(_red_logs(cfg)) == 2

    def test_a_reused_red_writes_no_prompt(self, tmp_path, monkeypatch):
        """No call, no prompt. The log is a record of what was *asked*, and a
        reused checkpoint asks nothing — writing one would suggest money was
        spent when the whole point of the reuse is that none was (F-4)."""
        cfg = _repo(tmp_path)
        _agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            run_red_phase(_task(), cfg, state)

        assert len(_red_logs(cfg)) == 1


@pytest.mark.slow
class TestItNeverCostsTheTask:
    """Bookkeeping that can fail work is a second, weaker gate — and this one
    would fail it *before* the call it describes."""

    def _read_only_logs(self, cfg: ExecutorConfig) -> None:
        """A real unwritable directory rather than a patched `write_text`: what
        is under test is the handling of a filesystem that says no.

        Skips when the mode is not enforced, asked as a **probe** rather than
        as `os.geteuid() == 0` (Copilot, PR #284). Root is one reason a
        chmod-ed directory stays writable; a container, a mounted filesystem
        that ignores modes, or an ACL are others, and each would make these
        tests pass while proving nothing. Asking the condition the test
        actually depends on covers all of them — and does not need to know
        which platform it is on.
        """
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.chmod(0o500)
        probe = cfg.logs_dir / ".probe"
        try:
            probe.write_text("x")
        except OSError:
            return  # the mode holds; the test can run
        probe.unlink()
        cfg.logs_dir.chmod(0o700)
        pytest.skip("this filesystem does not enforce a read-only directory")

    def test_an_unwritable_log_directory_does_not_fail_the_red(self, tmp_path, monkeypatch):
        from spec_runner.tdd import RedOutcome

        cfg = _repo(tmp_path)
        _agent(monkeypatch)
        self._read_only_logs(cfg)
        try:
            with ExecutorState(cfg) as state:
                result = run_red_phase(_task(), cfg, state)
        finally:
            cfg.logs_dir.chmod(0o700)

        assert result.outcome is RedOutcome.EXPECTED_FAIL

    def test_the_failure_is_logged_rather_than_swallowed(self, tmp_path, monkeypatch):
        said: list[tuple[str, str]] = []
        fields: list[tuple[str, str, dict]] = []

        class _Recorder:
            def __getattr__(self, level):
                def log(event, **kw):
                    said.append((level, event))
                    fields.append((level, event, kw))

                return log

        from spec_runner import prompts_log

        cfg = _repo(tmp_path)
        _agent(monkeypatch)
        # The writer is shared with the review stages now, so the warning comes
        # from there and names *which* prompt could not be written.
        monkeypatch.setattr(prompts_log, "logger", _Recorder())
        self._read_only_logs(cfg)
        try:
            with ExecutorState(cfg) as state:
                run_red_phase(_task(), cfg, state)
        finally:
            cfg.logs_dir.chmod(0o700)

        assert ("warning", "Could not log the prompt") in said
        assert any(kw.get("provenance") == "red" for _lvl, _ev, kw in fields), fields
