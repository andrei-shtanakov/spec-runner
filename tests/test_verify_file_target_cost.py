"""BEH-08 + BEH-32 (spec-runner#402/TASK-013, DT-13).

BEH-08's deterministic gate is the runner-invocation *count*, not seconds
(decomposition's own boundary): a declared file-target element costs exactly
one runner invocation, never one per collected member, while the same file
manually expanded into node ids costs one invocation per element. Measured
here on this project's own `tests/test_verify_first_declaration.py` — the
file the charter's 2026-09-08 measurement named (00-charter.md,
`10-requirements.md#NFR-01`) — copied byte-for-byte into an isolated fixture
repo so the run is a plain, fast `pytest` invocation rather than a worktree
checkout of this whole project. The charter's own numbers (2984 -> ~48
chars, 6.9-7.3s -> 0.36s) are a comparison baseline recorded alongside this
run's own artifact, never a literal this test is pinned to reproduce
(NFR-01: "числа приводятся замером, а не оценкой" — a different machine and
runner version measure different seconds).

BEH-32 asks two more things: the class this workstream adds never spends
money on its own (parsing, `validate`, composition resolution, the live
verify run itself) and the harness guard that already prevents that is not
weakened — plus that the one call this class *can* legitimately still
reach (RED-authoring, after a genuine `test_failure`) still goes through
`check_before_call` and lands in the agent-call ledger rather than being
silently skipped for `verify_first` tasks.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-08
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-32
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-13
"""

from __future__ import annotations

import ast
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import run_live_verify
from spec_runner.runner import CliInvocation
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task

REPO_ROOT = Path(__file__).resolve().parents[1]
MEASUREMENTS_DIR = (
    REPO_ROOT / "workstreams" / "verify-first-file-scope-group-targets-20260908" / "measurements"
)
ARTIFACT_PATH = MEASUREMENTS_DIR / "task-013-declaration-cost.json"
DECLARATION_TARGET = "tests/test_verify_first_declaration.py"

#: The charter's own 2026-09-08 measurement on this same file (00-charter.md,
#: NFR-01) — a comparison baseline the live artifact is recorded next to,
#: never a constant this test asserts equality against.
CHARTER_BASELINE = {
    "declaration_chars_file_target": 48,
    "declaration_chars_expanded": 2984,
    "file_target_elapsed_seconds": 0.36,
    "expanded_elapsed_seconds": 7.3,
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _declaration_node_ids(path: Path, *, declared_as: str) -> list[str]:
    """Every `test_*` in `path`, as real pytest node ids rooted at
    `declared_as` — derived from the file's own AST rather than a hardcoded
    count, so a rename or an added test does not silently stale BEH-08's
    manual-expansion side."""
    tree = ast.parse(path.read_text())
    ids: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test_"):
                    ids.append(f"{declared_as}::{node.name}::{sub.name}")
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            ids.append(f"{declared_as}::{node.name}")
    return ids


def _repo_with_real_declaration_file(tmp_path: Path) -> Path:
    """A fixture repo carrying a byte-for-byte copy of this project's own
    `tests/test_verify_first_declaration.py` (BEH-08's `Given`) — isolated
    from the live project tree so the measurement is a plain, fast pytest
    run rather than a worktree checkout of everything else in this repo."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    real_file = REPO_ROOT / DECLARATION_TARGET
    (tests_dir / real_file.name).write_text(real_file.read_text())
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
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
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _verify_task(task_id: str, verifies: list[str]) -> Task:
    return Task(
        id=task_id,
        name=f"verify-first cost probe {task_id}",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=verifies,
    )


def _count_runner_invocations(monkeypatch, module) -> dict:
    """One shared counting seam (frozen checkpoint's own technique, #13
    red): git plumbing (HEAD resolution, worktree add/remove) goes through
    this same patched name; only an actual runner invocation — never `git`
    as argv[0] — counts toward BEH-08."""
    invocations = {"count": 0}
    real_run = subprocess.run

    def _counting_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] != "git":
            invocations["count"] += 1
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", _counting_run)
    return invocations


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden"))


class TestBEH08FileTargetCostIsMeasuredOnTheRealDeclarationFile:
    """kind: e2e — BEH-08: `tests/test_verify_first_declaration.py`,
    declared as a file target, costs exactly one runner invocation and a
    ~48-char declaration line; the same file manually expanded into node ids
    costs one invocation per member and a line long enough to list every one
    of them — measured live, under pytest, never assumed from the charter's
    own numbers (AC-26)."""

    def test_declaration_is_one_run_and_the_reduction_is_measured(
        self, tmp_path, monkeypatch, update_golden
    ):
        import spec_runner.live_verify as live_verify_module

        root = _repo_with_real_declaration_file(tmp_path)
        node_ids = _declaration_node_ids(
            root / "tests" / "test_verify_first_declaration.py", declared_as=DECLARATION_TARGET
        )
        assert node_ids, "the real declaration file grew zero tests to expand"

        file_target_line = f"**Verifies:** {DECLARATION_TARGET}"
        expanded_line = f"**Verifies:** {', '.join(node_ids)}"

        invocations = _count_runner_invocations(monkeypatch, live_verify_module)
        start = time.perf_counter()
        file_target_result = run_live_verify(
            _verify_task("TASK-013-charter-a", [DECLARATION_TARGET]), _cfg(root)
        )
        file_target_elapsed = time.perf_counter() - start
        file_target_runs = invocations["count"]

        invocations["count"] = 0
        start = time.perf_counter()
        expanded_result = run_live_verify(
            _verify_task("TASK-013-charter-b", node_ids),
            _cfg(root, state_file=root / ".expanded.db"),
        )
        expanded_elapsed = time.perf_counter() - start
        expanded_runs = invocations["count"]

        assert file_target_result.passed, file_target_result.detail
        assert expanded_result.passed, expanded_result.detail

        # BEH-08's deterministic gate: run count, not seconds. The file
        # target never unrolls into one invocation per collected member.
        assert file_target_runs == 1
        assert expanded_runs == len(node_ids)

        # The declaration itself shrinks (charter: 2984 -> ~48 chars).
        assert len(file_target_line) < len(expanded_line)

        # A real, live-measured speedup — not pinned to the charter's own
        # seconds, only to the same direction of reduction.
        assert file_target_elapsed < expanded_elapsed

        if update_golden:
            MEASUREMENTS_DIR.mkdir(parents=True, exist_ok=True)
            ARTIFACT_PATH.write_text(
                json.dumps(
                    {
                        "task_id": "TASK-013",
                        "scenario": (
                            "verify-first-file-scope-group-targets-20260908#DT-13-declaration-cost"
                        ),
                        "declaration_chars_file_target": len(file_target_line),
                        "declaration_chars_expanded": len(expanded_line),
                        "file_target_runs": file_target_runs,
                        "expanded_runs": expanded_runs,
                        "file_target_elapsed_seconds": file_target_elapsed,
                        "expanded_elapsed_seconds": expanded_elapsed,
                        "baseline": CHARTER_BASELINE,
                    },
                    indent=2,
                )
                + "\n"
            )

        recorded = json.loads(ARTIFACT_PATH.read_text())
        assert recorded["task_id"] == "TASK-013"
        assert recorded["file_target_runs"] == 1
        assert recorded["expanded_runs"] == len(node_ids)
        assert recorded["declaration_chars_file_target"] < recorded["declaration_chars_expanded"]
        assert recorded["file_target_elapsed_seconds"] > 0
        assert recorded["expanded_elapsed_seconds"] > 0
        # These two fields are deterministic (derived from this file's own
        # AST, not from wall-clock timing), so a stale or hand-edited
        # artifact is caught here even outside --update-golden runs.
        assert recorded["declaration_chars_file_target"] == len(file_target_line)
        assert recorded["declaration_chars_expanded"] == len(expanded_line)
        assert (
            recorded["baseline"]["expanded_elapsed_seconds"]
            > recorded["baseline"]["file_target_elapsed_seconds"]
        )
        assert (
            recorded["baseline"]["declaration_chars_expanded"]
            > recorded["baseline"]["declaration_chars_file_target"]
        )


class TestBEH32ClassCostAndHarnessGuard:
    """kind: e2e — BEH-32: the class of verify-first file-target tasks never
    spends money on its own (parsing/`validate`/resolution/the live run
    itself), the harness guard is not weakened, and the one legitimate paid
    call this class can still reach — RED-authoring after a genuine
    `test_failure` — passes through `check_before_call` and lands in the
    agent-call ledger rather than being silently skipped for `verify_first`.
    """

    def _repo(self, tmp_path: Path, *, failing: bool) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        tests_dir = root / "tests"
        tests_dir.mkdir()
        assertion = "False" if failing else "True"
        (tests_dir / "test_probe.py").write_text(f"def test_it():\n    assert {assertion}\n")
        spec = root / "spec"
        spec.mkdir()
        (spec / "tasks.md").write_text("# Tasks\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        return root

    def _verify_first_cfg(self, root: Path, **overrides) -> ExecutorConfig:
        defaults: dict = {
            "project_root": root,
            "state_file": root / ".state.db",
            "logs_dir": root / ".logs",
            "test_command": "pytest",
            "tdd_runner": "pytest",
            "run_tests_on_done": False,
            "create_git_branch": False,
            "auto_commit": True,
            "run_review": False,
        }
        defaults.update(overrides)
        cfg = ExecutorConfig(**defaults)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_a_green_class_member_never_calls_the_red_authoring_seam(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
        monkeypatch,
    ):
        # The green implementation call is the one paid seam this class
        # legitimately still uses on a fully green group (same as every
        # other execution mode); it is stubbed here, never invoked for
        # real, exactly like `PAID_AGENT_COMMANDS` demands.
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE", stderr="cost: $0.01", returncode=0
        )

        red_authoring_calls: list[str] = []

        def _spy_run_agent(config, prompt, **kwargs):
            red_authoring_calls.append("called")
            return tdd.AgentCall(text="TDD_SELECTOR: tests/should_not_exist.py::never")

        monkeypatch.setattr(tdd, "_run_agent", _spy_run_agent)

        root = self._repo(tmp_path, failing=False)
        cfg = self._verify_first_cfg(root)
        task = _verify_task("TASK-100", ["tests/test_probe.py"])

        with ExecutorState(cfg) as state:
            outcome = execute_task(task, cfg, state)
            red_calls = [
                call for call in state.agent_calls(task.id) if call["provenance"] == "red_authoring"
            ]

        assert outcome is not False, "a fully green file target must not fail the task"
        assert red_authoring_calls == []
        assert red_calls == []

    def test_the_red_call_on_a_genuine_failure_is_refused_before_it_is_authored(self, tmp_path):
        root = self._repo(tmp_path, failing=True)
        cfg = self._verify_first_cfg(root, task_budget_usd=1.0)
        task = _verify_task("TASK-101", ["tests/test_probe.py"])

        with ExecutorState(cfg) as state:
            state.record_agent_call(task.id, "review", cost_usd=1.0)
            with patch("spec_runner.tdd._run_agent") as agent:
                outcome = execute_task(task, cfg, state)
            attempts = state.tasks[task.id].attempts

        assert outcome is False
        agent.assert_not_called()
        assert attempts[-1].error_code is ErrorCode.BUDGET_EXCEEDED
        assert "red_authoring" in (attempts[-1].error or "")

    def test_the_red_call_on_a_genuine_failure_is_priced_and_ledgered(self, tmp_path):
        root = self._repo(tmp_path, failing=True)
        cfg = self._verify_first_cfg(root)
        task = _verify_task("TASK-102", ["tests/test_probe.py"])

        fake_call = tdd.AgentCall(
            text="TDD_SELECTOR: tests/test_probe.py::test_it",
            cost_usd=1.23,
        )

        with ExecutorState(cfg) as state:
            with patch("spec_runner.tdd._run_agent", return_value=fake_call):
                execute_task(task, cfg, state)
            red_calls = [
                call for call in state.agent_calls(task.id) if call["provenance"] == "red_authoring"
            ]

        assert len(red_calls) == 1
        assert red_calls[0]["cost_usd"] == pytest.approx(1.23)
