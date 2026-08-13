"""#229 (F-25): a provider limit is retryable infrastructure, and a failure
says which one it was.

Battle-testing found a review agent whose entire output was

    You've hit your session limit · resets 5:30pm (Asia/Tbilisi)

and a run that reported `❌ Failed: tests/lint check`. Three separate defects
sat behind that one line, and this file pins the fix for each:

1. **The wording matched nothing.** `ERROR_PATTERNS` knew "you've hit your
   limit", "rate limit exceeded" and four others; none is a substring of what
   Claude actually prints for a session limit. On the implementation pass that
   made provider exhaustion a plain `TASK_FAILED`, retried on a **5-second
   linear backoff** against a cap that resets hours later — the precise shape
   of "retryable infrastructure treated as a verdict".
2. **The message dropped the one useful fact**: when the limit resets.
3. **`❌ Failed: tests/lint check` was printed for every post-done failure**,
   whatever refused — in the pilot, a byte-lock violation, while the suite was
   green. The operator read "tests/lint" and went looking at tests.

Plus the thing nobody was told: an agent that dies mid-way leaves its edits in
the working tree, and a blocked task said nothing about them.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.errors import classify
from spec_runner.execution import _headline, classify_retry_strategy, run_with_retries
from spec_runner.git_ops import uncommitted_work_paths
from spec_runner.runner import check_error_patterns
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task

#: Verbatim from the pilot log (kapelle TASK-101, review call 2026-08-12
#: 17:20:30) and the two other wordings the same CLI produces.
SESSION_LIMIT = "You've hit your session limit · resets 5:30pm (Asia/Tbilisi)"
USAGE_LIMIT = "Claude usage limit reached. Your limit will reset at 3pm (UTC)."
FIVE_HOUR = "5-hour limit reached ∙ resets 3pm"


class TestTheWordingIsRecognised:
    @pytest.mark.parametrize("message", [SESSION_LIMIT, USAGE_LIMIT, FIVE_HOUR])
    def test_provider_exhaustion_matches_a_pattern(self, message):
        assert check_error_patterns(message) is not None

    @pytest.mark.parametrize("message", [SESSION_LIMIT, USAGE_LIMIT, FIVE_HOUR])
    def test_it_classifies_as_a_rate_limit(self, message):
        kind, _human = classify(message, 1)
        assert kind == "rate_limit"

    @pytest.mark.parametrize(
        ("message", "reset"),
        [(SESSION_LIMIT, "5:30pm"), (USAGE_LIMIT, "3pm"), (FIVE_HOUR, "3pm")],
    )
    def test_the_message_carries_the_reset_time(self, message, reset):
        """The only actionable fact in a session-limit response."""
        _kind, human = classify(message, 1)
        assert reset in human

    def test_a_limit_without_a_reset_time_still_classifies(self):
        kind, human = classify("You've hit your session limit", 1)
        assert kind == "rate_limit"
        assert "resets" not in human, "no time is known — do not print a dangling 'resets'"

    def test_unrelated_prose_about_limits_is_not_a_provider_limit(self):
        """The patterns run against agent output too, so "limit" alone must
        not turn a real task failure into an infrastructure retry."""
        assert check_error_patterns("RecursionError: maximum recursion limit") is None
        assert classify("RecursionError: maximum recursion limit", 1)[0] == "unknown"


@pytest.mark.slow
class TestItIsTreatedAsInfrastructure:
    def _run(self, tmp_path: Path) -> list:
        root = tmp_path / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.c"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
        (root / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

        agent = root / "fake-agent"
        agent.write_text(f'#!/bin/bash\necho "{SESSION_LIMIT}"\nexit 1\n')
        agent.chmod(0o755)
        (root / "spec").mkdir()
        (root / "spec" / "tasks.md").write_text(
            "### TASK-001: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )
        cfg = ExecutorConfig(
            project_root=root,
            state_file=root / ".state.db",
            logs_dir=root / ".logs",
            claude_command=str(agent),
            max_retries=1,
            retry_delay_seconds=0,
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            run_with_retries(task, cfg, state)
            return state.get_task_state("TASK-001").attempts

    def test_a_session_limit_is_recorded_as_a_rate_limit_not_a_task_failure(self, tmp_path):
        attempts = self._run(tmp_path)
        assert attempts[-1].error_code is ErrorCode.RATE_LIMIT

    def test_and_therefore_backs_off_exponentially(self, tmp_path):
        """`TASK_FAILED` retries after 5 seconds. A cap that resets at 5:30pm
        does not care, and the attempt is spent for nothing."""
        attempts = self._run(tmp_path)
        assert classify_retry_strategy(attempts[-1].error_code) == "backoff_exponential"

    def test_the_recorded_error_names_the_reset_time(self, tmp_path):
        attempts = self._run(tmp_path)
        assert "5:30pm" in (attempts[-1].error or "")


class TestTheFailureLineNamesTheCause:
    @pytest.mark.slow
    def test_a_real_run_prints_the_reason_the_hook_gave(self, tmp_path, monkeypatch):
        """The call site, not just the helper.

        `_headline` can be perfect while `execute_task` still prints the old
        constant — which is exactly what a mutation of the call site showed,
        so this test exists because the unit test above did not catch it.
        """
        from spec_runner import execution

        root = tmp_path / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.c"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
        (root / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
        agent = root / "fake-agent"
        agent.write_text('#!/bin/bash\necho "TASK_COMPLETE: done"\n')
        agent.chmod(0o755)
        (root / "spec").mkdir()
        (root / "spec" / "tasks.md").write_text(
            "### TASK-001: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )
        cfg = ExecutorConfig(
            project_root=root,
            state_file=root / ".state.db",
            logs_dir=root / ".logs",
            claude_command=str(agent),
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)

        refusal = "Pre-terminal gate unsatisfied: tdd.claims: claim violated — modified test/x.exs"
        monkeypatch.setattr(
            execution, "post_done_hook", lambda *a, **k: (False, refusal, "skipped", "", False)
        )
        lines: list[str] = []
        monkeypatch.setattr(execution, "log_progress", lambda msg, tid=None: lines.append(msg))

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            execution.execute_task(task, cfg, state)

        failed = [line for line in lines if line.startswith("❌ Failed:")]
        assert failed, f"no failure line was printed; got {lines}"
        assert "claim violated" in failed[-1]
        assert "tests/lint" not in failed[-1]

    def test_it_uses_the_actual_reason(self):
        reason = (
            "Pre-terminal gate unsatisfied: tdd.claims: claim violated — modified "
            "test/kapelle/providers/catalog_test.exs"
        )
        assert "claim violated" in _headline(reason)
        assert "tests/lint" not in _headline(reason)

    def test_only_the_first_line_reaches_the_log(self):
        assert _headline("Tests failed:\n\n(500 lines of pytest output)") == "Tests failed:"

    def test_a_long_reason_is_truncated_not_wrapped(self):
        line = _headline("x" * 500)
        assert len(line) <= 120
        assert line.endswith("…")

    def test_an_empty_reason_still_says_something(self):
        assert _headline("") == "post-done checks"
        assert _headline("   \n  ") == "post-done checks"


class TestStrandedWorkIsReported:
    """An agent that dies mid-way leaves its edits in the tree. The tool used
    to record `blocked` and say nothing about them."""

    def _repo(self, tmp_path: Path) -> ExecutorConfig:
        root = tmp_path / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.c"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
        (root / "src.py").write_text("x = 1\n")
        (root / "spec").mkdir()
        (root / "spec" / "tasks.md").write_text("### TASK-001: t\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
        cfg = ExecutorConfig(
            project_root=root,
            state_file=root / "spec" / ".executor-state.db",
            logs_dir=root / "spec" / ".executor-logs",
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    def test_modified_and_untracked_files_are_both_reported(self, tmp_path):
        cfg = self._repo(tmp_path)
        (cfg.project_root / "src.py").write_text("x = 2\n")
        (cfg.project_root / "new_fixture.toml").write_text("a = 1\n")

        assert set(uncommitted_work_paths(cfg)) == {"src.py", "new_fixture.toml"}

    def test_runtime_state_is_not_stranded_work(self, tmp_path):
        """The state DB and its sidecars are written by the run itself;
        reporting them would make every blocked task look like it left work."""
        cfg = self._repo(tmp_path)
        cfg.state_file.write_text("db")
        cfg.state_file.with_name(cfg.state_file.name + "-wal").write_text("wal")
        (cfg.logs_dir / "TASK-001.log").write_text("log")

        assert uncommitted_work_paths(cfg) == []

    def test_the_caller_can_exclude_what_it_is_about_to_commit(self, tmp_path):
        cfg = self._repo(tmp_path)
        cfg.tasks_file.write_text("### TASK-001: t (blocked)\n")
        (cfg.project_root / "src.py").write_text("x = 2\n")

        assert uncommitted_work_paths(cfg, exclude=[cfg.tasks_file]) == ["src.py"]

    def test_a_clean_tree_reports_nothing(self, tmp_path):
        assert uncommitted_work_paths(self._repo(tmp_path)) == []

    def test_no_repo_is_not_an_error(self, tmp_path):
        """A report that cannot be produced must not become a failure — this
        runs on the path of a task that is already blocked."""
        root = tmp_path / "bare"
        root.mkdir()
        cfg = ExecutorConfig(project_root=root, state_file=root / ".s.db", logs_dir=root / ".logs")
        assert uncommitted_work_paths(cfg) == []

    def test_the_block_reason_names_the_stranded_paths(self, tmp_path):
        from spec_runner.hooks import _note_stranded_work

        cfg = self._repo(tmp_path)
        (cfg.project_root / "src.py").write_text("x = 2\n")
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        reason = _note_stranded_work(task, cfg, "Pre-terminal gate unsatisfied: tdd.claims")

        assert "tdd.claims" in reason  # the original reason survives
        assert "src.py" in reason
        assert "uncommitted" in reason

    def test_the_blocked_path_actually_asks(self, tmp_path):
        """The call site. `_note_stranded_work` can be correct while
        `_commit_blocked_status` never calls it — a mutation proved it could.

        `auto_commit` is off on purpose: the report must not depend on whether
        the harness commits the status flip, since a tree left dirty by an
        agent is exactly what an operator with auto-commit off will meet.
        """
        from spec_runner.hooks import _commit_blocked_status

        cfg = self._repo(tmp_path)
        cfg.auto_commit = False
        (cfg.project_root / "src.py").write_text("x = 2\n")
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        reason = _commit_blocked_status(task, cfg, "gate said no", candidate_sha="deadbeef")

        assert "src.py" in reason
        assert reason.startswith("gate said no")

    def test_a_clean_tree_leaves_the_reason_alone(self, tmp_path):
        from spec_runner.hooks import _note_stranded_work

        cfg = self._repo(tmp_path)
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        assert _note_stranded_work(task, cfg, "gate said no") == "gate said no"

    def test_many_paths_are_summarised(self, tmp_path):
        from spec_runner.hooks import STRANDED_PATHS_SHOWN, _note_stranded_work

        cfg = self._repo(tmp_path)
        for i in range(STRANDED_PATHS_SHOWN + 3):
            (cfg.project_root / f"f{i}.txt").write_text("x")
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        reason = _note_stranded_work(task, cfg, "gate said no")

        assert "and 3 more" in reason
        assert f"{STRANDED_PATHS_SHOWN + 3} path(s)" in reason
