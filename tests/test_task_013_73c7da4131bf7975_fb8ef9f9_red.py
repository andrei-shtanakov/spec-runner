"""RED checkpoint for TASK-013 (DT-13): BEH-08, BEH-32.

BEH-08's deterministic gate is the number of runner invocations, not seconds
(decomposition's own boundary on DT-13): one declared file-target element
must cost exactly one runner invocation, never one per collected test, while
the same file manually expanded into node ids costs one invocation per
element. BEH-32 asks that this reduction be measured live, under pytest, and
recorded into an artifact next to the WS-spec-runner-341 precedent
(`workstreams/WS-spec-runner-341/measurements/`) — this workstream has no
such artifact yet, which is what this checkpoint fails on.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-08
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-32
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-13
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task
from tests.test_verify_file_target_cost import ENVIRONMENT_KEYS, measurement_environment

REPO_ROOT = Path(__file__).resolve().parents[1]
MEASUREMENTS_DIR = (
    REPO_ROOT / "workstreams" / "verify-first-file-scope-group-targets-20260908" / "measurements"
)
ARTIFACT_PATH = MEASUREMENTS_DIR / "task-013-scenario-charter-cost.json"

# Small on purpose: BEH-08's own boundary names the run *count* as the
# gate, not elapsed seconds, so a handful of tests already proves the 1-vs-N
# multiplier without paying for a slow 20-test reproduction here.
_TEST_COUNT = 6


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    body = "\n\n\n".join(f"def test_{i:02d}():\n    assert True" for i in range(_TEST_COUNT))
    (tests_dir / "test_sample.py").write_text(body + "\n")
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


def _task(task_id: str, verifies: list[str]) -> Task:
    return Task(
        id=task_id,
        name=f"verify-first cost probe {task_id}",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=verifies,
    )


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden"))


class TestFileTargetRunCountIsMeasuredAndRecorded:
    """kind: e2e — BEH-08: a declared file target is one runner invocation,
    never one per collected test. BEH-32: the reduction is measured live,
    under pytest, and recorded next to the WS-spec-runner-341 precedent."""

    def test_one_element_is_one_run_and_the_measurement_is_recorded(
        self, tmp_path, monkeypatch, update_golden
    ):
        root = _repo(tmp_path)
        node_ids = [f"tests/test_sample.py::test_{i:02d}" for i in range(_TEST_COUNT)]

        invocations = {"count": 0}
        real_run = subprocess.run

        def _counting_run(argv, *args, **kwargs):
            # Git plumbing (HEAD resolution, worktree add/remove) goes
            # through this same patched name; only an actual runner
            # invocation (never "git" as argv[0]) counts toward BEH-08.
            if isinstance(argv, (list, tuple)) and argv and argv[0] != "git":
                invocations["count"] += 1
            return real_run(argv, *args, **kwargs)

        import spec_runner.live_verify as live_verify_module

        monkeypatch.setattr(live_verify_module.subprocess, "run", _counting_run)

        invocations["count"] = 0
        start = time.perf_counter()
        file_target_result = run_live_verify(
            _task("TASK-013-a", ["tests/test_sample.py"]), _cfg(root)
        )
        file_target_elapsed = time.perf_counter() - start
        file_target_runs = invocations["count"]

        invocations["count"] = 0
        start = time.perf_counter()
        expanded_result = run_live_verify(
            _task("TASK-013-b", node_ids),
            _cfg(root, state_file=root / ".expanded.db"),
        )
        expanded_elapsed = time.perf_counter() - start
        expanded_runs = invocations["count"]

        assert file_target_result.passed, file_target_result.detail
        assert expanded_result.passed, expanded_result.detail

        # BEH-08: the deterministic gate — exactly one runner invocation for
        # the declared file target, and exactly one per element for its
        # manual expansion, so the multiplier is observed, not assumed.
        assert file_target_runs == 1
        assert expanded_runs == _TEST_COUNT

        # BEH-32: the reduction, measured live under pytest, recorded next
        # to the WS-spec-runner-341 precedent
        # (workstreams/WS-spec-runner-341/measurements/). Writing the
        # tracked artifact is opt-in by repo convention (--update-golden);
        # a normal run reads the committed artifact back and checks it.
        if update_golden:
            MEASUREMENTS_DIR.mkdir(parents=True, exist_ok=True)
            ARTIFACT_PATH.write_text(
                json.dumps(
                    {
                        "task_id": "TASK-013",
                        "scenario": "verify-first-file-scope-group-targets-20260908#DT-13",
                        "file_target_runs": file_target_runs,
                        "expanded_runs": expanded_runs,
                        "file_target_elapsed_seconds": file_target_elapsed,
                        "expanded_elapsed_seconds": expanded_elapsed,
                        # DT-13: seconds without the machine and the runner
                        # versions invite an absolute threshold on someone
                        # else's CI. Same block, same definition, as the
                        # other DT-13 artifact.
                        "environment": measurement_environment(),
                        "baseline": {
                            "file_target_runs": 1,
                            "expanded_runs": _TEST_COUNT,
                        },
                    },
                    indent=2,
                )
                + "\n"
            )

        recorded = json.loads(ARTIFACT_PATH.read_text())
        assert recorded["task_id"] == "TASK-013"
        assert recorded["file_target_runs"] == 1
        assert recorded["expanded_runs"] == _TEST_COUNT
        assert recorded["file_target_elapsed_seconds"] > 0
        assert recorded["expanded_elapsed_seconds"] > 0
        # DT-13 again, and checked on every run for the same reason: an
        # artifact written before the block existed is what this catches.
        # (`expanded_runs` above may stay an equality here — unlike the
        # declaration-cost artifact, this fixture's sample file is generated
        # by this test and cannot grow behind its back.)
        assert set(recorded.get("environment", {})) >= ENVIRONMENT_KEYS, (
            f"the artifact must name the hardware and the runner versions "
            f"it was measured on; missing "
            f"{sorted(ENVIRONMENT_KEYS - set(recorded.get('environment', {})))}"
        )
