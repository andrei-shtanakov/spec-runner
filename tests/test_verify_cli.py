"""BEH-30/BEH-31 (#367 TASK-011): the live verify-first run gets its own
stage, and its durable evidence — plus which of the three outcome paths a
task took — is presented through the CLI (`tdd status`, `status`).

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-30 (-BEH-31)
Traces: FR-22, FR-23, FR-12

kind: integration — real `git`/`pytest` subprocesses against a fixture
repository; only the paid agent call is stood in for.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner import tdd_status
from spec_runner.claims import record_claims
from spec_runner.cli import build_task_json_result
from spec_runner.cli_info import print_status
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import VerifyRunResult, run_live_verify
from spec_runner.runner import CliInvocation
from spec_runner.stages import STAGES
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, RedCheckpoint, RedOutcome, _config_hash, resolve_namespace
from spec_runner.validate import validate_all


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _base_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    (root / "tests" / "test_green.py").write_text("def test_it():\n    assert 2 + 2 == 4\n")
    (root / "tests" / "test_redgroup.py").write_text(
        "def test_it():\n    assert False, 'deliberate failure'\n"
    )
    _commit(root, "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": True,
        "run_review": False,
        "callback_url": "",
        "lint_command": "",
        "tdd_namespace": "ws-beh30-31",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str, **overrides) -> Task:
    defaults: dict = {
        "id": task_id,
        "name": "verify-first task",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_green.py::test_it"],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _completes(config, invocation):
    return MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)


def _run(task: Task, config: ExecutorConfig, state: ExecutorState, *, agent_side_effect=_completes):
    """`execute_task` with the paid CLI call replaced, no red authoring
    expected (same seam `test_verify_claims.py` uses)."""
    no_red = MagicMock(side_effect=AssertionError("no red authoring expected here"))
    with (
        patch("spec_runner.execution.update_task_status"),
        patch("spec_runner.execution.log_progress"),
        patch(
            "spec_runner.execution.build_cli_invocation",
            return_value=CliInvocation(["echo", "hi"], "text"),
        ),
        patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
        patch("spec_runner.execution.pre_start_hook", return_value=True),
        patch("spec_runner.execution._run_agent_process", side_effect=agent_side_effect),
        patch("spec_runner.tdd._run_agent", no_red),
    ):
        return execute_task(task, config, state)


class TestBEH30LiveVerifyStageIsFirstClass:
    """kind: integration — BEH-30: the live verify-first run has its own
    named stage, mirrored to progress, and used to record `error_stage`."""

    def test_verify_is_a_named_stage(self):
        assert "verify" in STAGES

    def test_progress_mirrors_the_verify_stage_not_tests(self, tmp_path):
        root = _base_repo(tmp_path)
        config = _cfg(root, tdd_namespace="ws-beh30-mirror")
        task = _task("TASK-MIRROR")
        mirrored: list[str] = []
        no_red = MagicMock(side_effect=AssertionError("no red authoring expected here"))

        with (
            ExecutorState(config) as state,
            patch("spec_runner.execution.update_task_status"),
            patch(
                "spec_runner.execution.log_progress",
                side_effect=lambda line, *_a, **_k: mirrored.append(line),
            ),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["echo", "hi"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
            patch("spec_runner.execution.pre_start_hook", return_value=True),
            patch("spec_runner.execution._run_agent_process", side_effect=_completes),
            patch("spec_runner.tdd._run_agent", no_red),
        ):
            result = execute_task(task, config, state)

        assert result is True
        assert any("stage: verify" in line for line in mirrored), (
            "BEH-30: the live verify-first run must mirror its own 'verify' stage, "
            f"not 'tests' — saw: {mirrored}"
        )


class TestBEH31EvidenceAndPathThroughTheCli:
    """kind: integration — BEH-31: `tdd status` (human + `--json`, one
    `collect()`) shows each task's verify-evidence — outcome, SHA, declared
    group, config hash — so an operator can tell which of the three paths a
    verify-first task took, and a green-only DONE from a confirmed-red DONE.
    Plain `status` reflects the fact and outcome too."""

    def _populate(self, tmp_path):
        root = _base_repo(tmp_path)

        # Green: the declared group is already green on entry.
        green_task = _task("TASK-GREEN")
        green_config = _cfg(root)
        with ExecutorState(green_config) as state:
            assert _run(green_task, green_config, state) is True

        # Test-failure: the declared group is red; the entry run observes it
        # and hands off to ordinary red authoring (BEH-21) rather than
        # refusing outright.
        redpath_task = _task("TASK-REDPATH", verifies=["tests/test_redgroup.py::test_it"])
        redpath_config = _cfg(root)

        def _fake_red_agent(config, prompt, **kwargs):
            red_test = Path(config.project_root) / "tests" / "test_red_task_redpath.py"
            red_test.write_text("def test_red():\n    assert False, 'red'\n")
            return AgentCall(
                text="TDD_SELECTOR: tests/test_red_task_redpath.py::test_red\nTASK_COMPLETE"
            )

        with (
            ExecutorState(redpath_config) as state,
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution.log_progress"),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["echo", "hi"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
            patch("spec_runner.execution.pre_start_hook", return_value=True),
            patch("spec_runner.execution._run_agent_process", side_effect=_completes),
            patch("spec_runner.tdd._run_agent", side_effect=_fake_red_agent),
        ):
            execute_task(redpath_task, redpath_config, state)

        # Instrument-error: a composite `test_command` refuses before any
        # selector runs — same shape as the frozen BEH-30 red test.
        instrument_task = _task("TASK-INSTRUMENT")
        instrument_config = _cfg(root, test_command="pytest tests/ && echo done")
        with ExecutorState(instrument_config) as state:
            result = _run(instrument_task, instrument_config, state)
        assert result is False

        # An ordinary `tdd` task with a confirmed red — no verify-evidence at
        # all, the baseline this CLI must stay distinguishable from.
        tdd_config = _cfg(root)
        namespace = resolve_namespace(tdd_config)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        checkpoint = RedCheckpoint(
            task_id="TASK-TDD",
            namespace=namespace,
            commit_sha=head,
            baseline_sha=head,
            selector="tests/test_green.py::test_it",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(tdd_config),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-09-06T00:00:00",
        )
        with ExecutorState(tdd_config) as state:
            state.record_red_checkpoint(checkpoint)
            record_claims(tdd_config, state, checkpoint)

        return root, tdd_config

    def test_tdd_status_shows_outcome_sha_group_and_config_hash_per_evidence(self, tmp_path):
        root, config = self._populate(tmp_path)
        data = tdd_status.collect(config)
        by_task = {v["task_id"]: v for v in data["verify_evidence"]}

        assert set(by_task) == {"TASK-GREEN", "TASK-REDPATH", "TASK-INSTRUMENT"}
        assert by_task["TASK-GREEN"]["outcome"] == "green"
        assert by_task["TASK-REDPATH"]["outcome"] == "test_failure"
        assert by_task["TASK-INSTRUMENT"]["outcome"] == "instrument_error"
        for tid, evidence in by_task.items():
            assert evidence["commit_sha"], f"{tid}: no judged commit recorded"
            assert evidence["config_hash"], f"{tid}: no config hash recorded"
            assert evidence["group_declared"], f"{tid}: no declared group recorded"

    def test_green_only_reads_differently_from_a_confirmed_red(self, tmp_path):
        """BEH-31 Then clause: the green-only checkpoint-equivalent must be
        distinguishable from a genuinely confirmed red — both reach a
        terminal `done`-shaped state, but for different, evidenced reasons."""
        root, config = self._populate(tmp_path)
        data = tdd_status.collect(config)

        green = tdd_status.lifecycle_of(data, "TASK-GREEN")
        instrument = tdd_status.lifecycle_of(data, "TASK-INSTRUMENT")
        confirmed_red = tdd_status.lifecycle_of(data, "TASK-TDD")

        assert "green" in green
        assert "done" in green
        assert "red confirmed" in confirmed_red
        assert green != confirmed_red, (
            "a green-only checkpoint-equivalent must not read the same as a genuinely confirmed red"
        )
        assert "instrument" in instrument

    def test_human_and_json_views_are_fed_by_the_same_collect(self, tmp_path):
        root, config = self._populate(tmp_path)
        data = tdd_status.collect(config)
        text = tdd_status.render(data, None)

        for tid in ("TASK-GREEN", "TASK-REDPATH", "TASK-INSTRUMENT", "TASK-TDD"):
            assert tid in text
        assert "verify-evidence" in text
        assert "config_hash" in text
        # No second, independent read: `render` only ever consumes the dict
        # `collect()` already produced (and `--json` would print verbatim).
        assert tdd_status.render(data, None) == text

    def test_plain_status_reports_the_verify_run_fact_and_outcome(self, tmp_path, capsys):
        root, config = self._populate(tmp_path)

        print_status(config)
        out = capsys.readouterr().out

        assert "TASK-GREEN" in out
        assert "Verify: green" in out
        assert "TASK-REDPATH" in out
        assert "Verify: test_failure" in out
        assert "TASK-INSTRUMENT" in out
        assert "Verify: instrument_error" in out


class TestBEH31GreenOnlyExcludesAConfirmedRed:
    """kind: unit/integration — review finding on TASK-011 (WS-367): a
    verify_first task only ever reaches `done` off a *green* re-verify row,
    even when it walked the red-authoring cycle and confirmed a red on
    entry (BEH-21). `lifecycle_of` must not call that task "green-only" —
    the confirmed checkpoint is the very account of the red it authored —
    and plain `status` must not hide that fact either."""

    def test_done_with_latest_green_evidence_and_a_confirmed_red_is_not_green_only(self):
        data = {
            "active_checkpoints": [
                {
                    "checkpoint_id": "cp-redpath-1",
                    "task_id": "TASK-REDPATH",
                    "commit_sha": "a" * 40,
                    "baseline_sha": "b" * 40,
                    "selector": "tests/test_redgroup.py::test_it",
                    "outcome": RedOutcome.EXPECTED_FAIL.value,
                    "environment_id": "unpinned",
                    "timestamp": "2026-09-06T00:00:00",
                }
            ],
            "retired_checkpoints": [],
            "verify_evidence": [
                {
                    "task_id": "TASK-REDPATH",
                    "commit_sha": "c" * 40,
                    "outcome": "green",
                }
            ],
            "phases": {"TASK-REDPATH": [{"phase": "done", "detail": ""}]},
        }

        result = tdd_status.lifecycle_of(data, "TASK-REDPATH")

        assert "green-only" not in result, (
            "a task that reached done off a confirmed red must not read as one "
            f"that never authored a red — got: {result!r}"
        )
        assert "done" in result

    def test_done_with_latest_green_evidence_and_a_retired_confirmed_red_is_not_green_only(self):
        # The checkpoint that authored the red may since have been retired
        # (a remedy, or a later run) — the account of the red still stands.
        data = {
            "active_checkpoints": [],
            "retired_checkpoints": [
                {
                    "task_id": "TASK-REDPATH",
                    "status": "repaired",
                    "outcome": RedOutcome.EXPECTED_FAIL.value,
                    "selector": "tests/test_redgroup.py::test_it",
                    "timestamp": "2026-09-06T00:00:00",
                }
            ],
            "verify_evidence": [
                {
                    "task_id": "TASK-REDPATH",
                    "commit_sha": "c" * 40,
                    "outcome": "green",
                }
            ],
            "phases": {"TASK-REDPATH": [{"phase": "done", "detail": ""}]},
        }

        result = tdd_status.lifecycle_of(data, "TASK-REDPATH")

        assert "green-only" not in result
        assert "done" in result

    def test_done_with_latest_green_evidence_and_no_red_checkpoint_stays_green_only(self):
        # Control: a genuinely green-only task (BEH-20) must keep the label.
        data = {
            "active_checkpoints": [],
            "retired_checkpoints": [],
            "verify_evidence": [
                {
                    "task_id": "TASK-GREEN",
                    "commit_sha": "c" * 40,
                    "outcome": "green",
                }
            ],
            "phases": {"TASK-GREEN": [{"phase": "done", "detail": ""}]},
        }

        result = tdd_status.lifecycle_of(data, "TASK-GREEN")

        assert "green-only" in result

    def test_plain_status_marks_a_confirmed_red_behind_a_green_reverify(self, tmp_path, capsys):
        root = _base_repo(tmp_path)
        config = _cfg(root, tdd_namespace="ws-redconfirmed")
        namespace = resolve_namespace(config)
        task = _task("TASK-REDCONFIRMED", verifies=["tests/test_redgroup.py::test_it"])
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        with ExecutorState(config) as state:
            state.record_attempt(task.id, True, 0.0)
            state.record_verify_evidence(
                task=task,
                config=config,
                result=VerifyRunResult(
                    sha=head, ran=True, passed=True, detail="", adapter="pytest"
                ),
            )
            state.record_red_checkpoint(
                RedCheckpoint(
                    task_id=task.id,
                    namespace=namespace,
                    commit_sha=head,
                    baseline_sha=head,
                    selector="tests/test_redgroup.py::test_it",
                    environment_id="unpinned",
                    execution_mode="verify_first",
                    config_hash=_config_hash(config),
                    outcome=RedOutcome.EXPECTED_FAIL,
                    timestamp="2026-09-06T00:00:00",
                )
            )

        print_status(config)
        out = capsys.readouterr().out

        assert "Verify: green" in out
        assert "red confirmed" in out, (
            f"a green re-verify behind a confirmed red must say so in plain status — got: {out!r}"
        )


class TestVerifyEvidenceForNamespaceQuery:
    """kind: unit — `verify_evidence_for_namespace` (state.py) is a new
    query, not exercised by the integration fixtures above beyond one row
    per task: it must pick the *latest* row per task, stay scoped to its own
    namespace, and honour an explicit `task_id` filter."""

    def _result(self, sha: str, passed: bool) -> VerifyRunResult:
        return VerifyRunResult(sha=sha, ran=True, passed=passed, detail="", adapter="pytest")

    def test_latest_row_wins_when_a_task_has_several(self, tmp_path):
        config = _cfg(tmp_path, tdd_namespace="ws-latest")
        task = _task("TASK-A")
        with ExecutorState(config) as state:
            assert state.record_verify_evidence(
                task=task, config=config, result=self._result("a" * 40, passed=False)
            )
            assert state.record_verify_evidence(
                task=task, config=config, result=self._result("b" * 40, passed=True)
            )
            rows = state.verify_evidence_for_namespace("ws-latest")

        assert len(rows) == 1, "only the latest row per task_id must be returned"
        assert rows[0].commit_sha == "b" * 40
        assert rows[0].outcome == "green"

    def test_namespace_is_isolated(self, tmp_path):
        config = _cfg(tmp_path, tdd_namespace="ws-a")
        other_config = _cfg(tmp_path, tdd_namespace="ws-b")
        with ExecutorState(config) as state:
            assert state.record_verify_evidence(
                task=_task("TASK-A"), config=config, result=self._result("a" * 40, passed=True)
            )
            assert state.record_verify_evidence(
                task=_task("TASK-B"),
                config=other_config,
                result=self._result("b" * 40, passed=True),
            )
            ws_a_rows = state.verify_evidence_for_namespace("ws-a")
            ws_b_rows = state.verify_evidence_for_namespace("ws-b")

        assert {r.task_id for r in ws_a_rows} == {"TASK-A"}
        assert {r.task_id for r in ws_b_rows} == {"TASK-B"}

    def test_task_id_filter_narrows_to_one_task(self, tmp_path):
        config = _cfg(tmp_path, tdd_namespace="ws-filter")
        with ExecutorState(config) as state:
            assert state.record_verify_evidence(
                task=_task("TASK-A"), config=config, result=self._result("a" * 40, passed=True)
            )
            assert state.record_verify_evidence(
                task=_task("TASK-B"), config=config, result=self._result("b" * 40, passed=False)
            )
            all_rows = state.verify_evidence_for_namespace("ws-filter")
            filtered = state.verify_evidence_for_namespace("ws-filter", "TASK-B")

        assert {r.task_id for r in all_rows} == {"TASK-A", "TASK-B"}
        assert [r.task_id for r in filtered] == ["TASK-B"]


class TestBEH27EveryRefusalNamesFileAndReason:
    """kind: integration — BEH-27 (FR-20, group state-and-surfaces, TASK-007):
    each of the five refusal classes a file target introduces names a
    concrete file and a concrete reason — never only a return code — and is
    readable at the surface a verify-first refusal is read at today
    (`status`'s `Last error` line, `error_stage`), without reading the run's
    own logs. The rejected value is quoted verbatim (design, врезка 1)."""

    def test_no_test_collected_names_the_file(self, tmp_path, capsys):
        root = _base_repo(tmp_path)
        (root / "tests" / "test_empty.py").write_text("# no tests in this file\n")
        _commit(root, "add a test file collecting nothing")
        config = _cfg(root, tdd_namespace="ws-beh27-empty")
        task = _task("TASK-BEH27-EMPTY", verifies=["tests/test_empty.py"])

        with ExecutorState(config) as state:
            result = _run(task, config, state)
            assert result is False
            ts = state.get_task_state(task.id)
            assert ts is not None and ts.status == "failed"
            last_error = ts.last_error or ""
            assert "tests/test_empty.py" in last_error, (
                f"BEH-27: no tests collected must name the file — got: {last_error!r}"
            )
            assert ts.attempts[-1].error_stage == "verify"

        print_status(config)
        out = capsys.readouterr().out
        assert "tests/test_empty.py" in out, (
            "BEH-27: the reason must be readable at the same surface (plain "
            f"`status`), without reading the run's logs — got: {out!r}"
        )

    def test_zero_executed_member_names_the_file(self, tmp_path, capsys):
        root = _base_repo(tmp_path)
        (root / "tests" / "test_all_skipped.py").write_text(
            "import pytest\n\n"
            "@pytest.mark.skip(reason='not ready')\n"
            "def test_a():\n    assert True\n"
        )
        _commit(root, "add a file whose only member is skipped")
        config = _cfg(root, tdd_namespace="ws-beh27-skip")
        task = _task("TASK-BEH27-SKIP", verifies=["tests/test_all_skipped.py"])

        with ExecutorState(config) as state:
            result = _run(task, config, state)
            assert result is False
            ts = state.get_task_state(task.id)
            last_error = ts.last_error or ""
            assert "tests/test_all_skipped.py" in last_error, (
                f"BEH-27: zero members executed must name the file — got: {last_error!r}"
            )

        print_status(config)
        out = capsys.readouterr().out
        assert "tests/test_all_skipped.py" in out

    def test_unaccounted_member_names_the_member(self, tmp_path, capsys):
        root = _base_repo(tmp_path)
        (root / "tests" / "test_early_stop.py").write_text(
            "def test_a():\n    assert False, 'genuine failure'\n\n\n"
            "def test_b():\n    assert True\n"
        )
        _commit(root, "test_a fails, -x stops before test_b ever runs")
        config = _cfg(
            root, tdd_namespace="ws-beh27-unaccounted", test_command="python -m pytest -x"
        )
        task = _task("TASK-BEH27-UNACCOUNTED", verifies=["tests/test_early_stop.py"])

        with ExecutorState(config) as state:
            result = _run(task, config, state)
            assert result is False
            ts = state.get_task_state(task.id)
            last_error = ts.last_error or ""
            assert "test_b" in last_error, (
                f"BEH-27/BEH-15: the unaccounted member must be named by name — got: {last_error!r}"
            )

        print_status(config)
        out = capsys.readouterr().out
        assert "test_b" in out

    def test_form_that_is_not_a_file_target_quotes_the_rejected_value(self, tmp_path, capsys):
        root = _base_repo(tmp_path)
        config = _cfg(root, tdd_namespace="ws-beh27-form")
        task = _task("TASK-BEH27-FORM", verifies=["-k something"])

        with ExecutorState(config) as state:
            result = _run(task, config, state)
            assert result is False
            ts = state.get_task_state(task.id)
            last_error = ts.last_error or ""
            assert "-k something" in last_error, (
                f"BEH-27: the rejected value must be quoted verbatim — got: {last_error!r}"
            )

        print_status(config)
        out = capsys.readouterr().out
        assert "-k something" in out

    def test_adapter_without_file_target_support_refuses_by_name(self, tmp_path):
        """The fifth class (`адаптер не поддерживает файловые цели`) refuses
        on `validate`, before any execution (design Q-05): an adapter that
        never declared `supports_file_targets` has nothing to add over its
        existing node-id-only vocabulary, and refuses a bare file path under
        its own stable code, naming the file — never a synthetic mock."""
        tasks_path = tmp_path / "tasks.md"
        tasks_path.write_text(
            "### TASK-927: t\n"
            "\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/some_test.exs\n"
            "Est: 1d\n"
        )
        config_path = tmp_path / "spec-runner.config.yaml"
        config_path.write_text("tdd_runner: exunit\ncommands:\n  test: mix test\n")

        result = validate_all(tasks_file=tasks_path, config_file=config_path, project_root=tmp_path)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "tests/some_test.exs" in joined, (
            f"BEH-27: the adapter's own refusal must name the file — got: {joined!r}"
        )
        assert "exunit" in joined


class TestBEH28ComposedSizeMatchesEvidenceAndAddsNoRunnerInvocation:
    """kind: integration — BEH-28 (FR-21, TASK-007) in this test file (the
    frozen red for this scenario lives in
    tests/test_task_007_73c7da4131bf7975_fb8ef9f9_red.py): the announced
    count and the count recorded into evidence are the SAME number, the
    pre-run budget line names the DECLARED group's length (distinct from
    the resolved composition), and announcing the size adds no runner
    invocation."""

    def test_announced_composition_size_matches_the_recorded_evidence(self, tmp_path):
        root = _base_repo(tmp_path)
        (root / "tests" / "test_multi.py").write_text(
            "import pytest\n\n"
            "def test_a():\n    assert True\n\n"
            "def test_b():\n    assert True\n\n"
            "@pytest.mark.skip(reason='wip')\n"
            "def test_c():\n    assert True\n"
        )
        _commit(root, "a three-member file target")
        config = _cfg(root, tdd_namespace="ws-beh28-match")
        task = _task("TASK-BEH28-MATCH", verifies=["tests/test_multi.py"])

        lines: list[str] = []
        result = run_live_verify(task, config, log_progress=lines.append)

        assert result.passed
        assert len(result.composition) == 3

        announced = [
            line
            for line in lines
            if "3" in line
            and any(kw in line.lower() for kw in ("composition", "collected", "member"))
        ]
        assert announced, f"composition size must be announced: {lines!r}"

        # The pre-run budget line names the DECLARED group's length (one
        # `**Verifies:**` element) — distinct from the resolved composition
        # size, which the same run's collection phase alone can produce.
        budget_lines = [line for line in lines if "budget" in line.lower()]
        assert budget_lines and "1 selector" in budget_lines[0], (
            f"the pre-run budget line must still name the declared length, "
            f"not the resolved composition: {budget_lines!r}"
        )

    def test_no_extra_runner_invocation_is_added(self, tmp_path):
        root = _base_repo(tmp_path)
        (root / "tests" / "test_multi2.py").write_text(
            "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n"
        )
        _commit(root, "a two-member file target")
        config = _cfg(root, tdd_namespace="ws-beh28-count")
        task = _task("TASK-BEH28-COUNT", verifies=["tests/test_multi2.py"])

        calls: list[list[str]] = []
        real_run = subprocess.run

        def _counting_run(argv, *args, **kwargs):
            calls.append(list(argv))
            return real_run(argv, *args, **kwargs)

        with patch("spec_runner.live_verify.subprocess.run", side_effect=_counting_run):
            result = run_live_verify(task, config, log_progress=lambda _line: None)

        assert result.passed
        pytest_invocations = [c for c in calls if any("pytest" in part for part in c)]
        assert len(pytest_invocations) == 1, (
            f"announcing the composition size must add no runner invocation of its own — {calls!r}"
        )


class TestBEH29GreenWithSkipsIsDistinguishableFromFullyExecuted:
    """kind: integration — BEH-29 (FR-22, TASK-007): two green runs of a
    file target — one where every member executed, one where part was
    skipped — are distinguishable in `status`, in `tdd status` (human +
    `--json`), and in `build_task_json_result` (`--json-result`), without
    comparing compositions by hand (the Q-A asymmetry stays visible)."""

    def _run_file_target(self, root, task_id, filename, body, *, namespace):
        (root / "tests" / filename).write_text(body)
        _commit(root, f"add {filename}")
        config = _cfg(root, tdd_namespace=namespace)
        task = _task(task_id, verifies=[f"tests/{filename}"])
        with ExecutorState(config) as state:
            assert _run(task, config, state) is True
        return config

    def test_json_result_distinguishes_the_two_greens(self, tmp_path):
        root = _base_repo(tmp_path)
        full_config = self._run_file_target(
            root,
            "TASK-BEH29-FULL",
            "test_full.py",
            "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
            namespace="ws-beh29-full",
        )
        partial_config = self._run_file_target(
            root,
            "TASK-BEH29-PARTIAL",
            "test_partial.py",
            "import pytest\n\ndef test_a():\n    assert True\n\n"
            "@pytest.mark.skip(reason='wip')\ndef test_b():\n    assert True\n",
            namespace="ws-beh29-partial",
        )

        with ExecutorState(full_config) as state:
            full_entry = build_task_json_result("TASK-BEH29-FULL", state, full_config)
        with ExecutorState(partial_config) as state:
            partial_entry = build_task_json_result("TASK-BEH29-PARTIAL", state, partial_config)

        assert full_entry["verify_outcome"] == "green"
        assert partial_entry["verify_outcome"] == "green"
        assert full_entry["verify_composition"] == {"size": 2, "executed": 2, "skipped": 0}
        assert partial_entry["verify_composition"] == {"size": 2, "executed": 1, "skipped": 1}
        assert full_entry["verify_composition"] != partial_entry["verify_composition"], (
            "BEH-29: a fully-executed green must not read the same as a "
            "green with skips in --json-result"
        )

    def test_plain_status_names_the_asymmetry(self, tmp_path, capsys):
        root = _base_repo(tmp_path)
        self._run_file_target(
            root,
            "TASK-BEH29-STATUS-FULL",
            "test_status_full.py",
            "def test_a():\n    assert True\n",
            namespace="ws-beh29-status",
        )
        config = self._run_file_target(
            root,
            "TASK-BEH29-STATUS-PARTIAL",
            "test_status_partial.py",
            "import pytest\n\ndef test_a():\n    assert True\n\n"
            "@pytest.mark.skip(reason='wip')\ndef test_b():\n    assert True\n",
            namespace="ws-beh29-status",
        )

        print_status(config)
        out = capsys.readouterr().out

        assert "1/1 member(s) executed" in out, (
            f"BEH-29: a fully-executed green must be named as such — got: {out!r}"
        )
        assert "member(s) executed, 1 skipped" in out, (
            f"BEH-29: a green with skips must name the skip count — got: {out!r}"
        )

    def test_tdd_status_json_carries_the_named_composition(self, tmp_path):
        root = _base_repo(tmp_path)
        config = self._run_file_target(
            root,
            "TASK-BEH29-TDD",
            "test_tdd_partial.py",
            "import pytest\n\ndef test_a():\n    assert True\n\n"
            "@pytest.mark.skip(reason='wip')\ndef test_b():\n    assert True\n",
            namespace="ws-beh29-tdd",
        )

        data = tdd_status.collect(config)
        row = next(v for v in data["verify_evidence"] if v["task_id"] == "TASK-BEH29-TDD")

        assert len(row["composition"]) == 2
        skipped = [
            m for m in row["composition"] if m["outcome"] not in ("passed", "failed", "error")
        ]
        assert skipped, f"the skipped member must be named: {row['composition']!r}"

        text = tdd_status.render(data, None)
        assert "skipped" in text
