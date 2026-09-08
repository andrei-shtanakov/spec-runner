"""RED for TASK-006 (BEH-20, DT-06): verify-evidence must name a file
target's composition by member and per-member outcome, with its size — not
only fold a skipped member into `detail`'s free-text summary, which a
machine reader cannot parse back into "which members ran and which were
skipped" without the original log.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-20
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-06

Today `VerifyEvidence` (`live_verify.py`) has no `composition` field at all:
a file target's per-member manifest (BEH-16, already resolved inside
`run_live_verify`) is folded into `detail`'s prose
("...; skipped: test_skip_a (reason)") and nothing else survives the write
to `verify_evidence` — so a reader of the persisted evidence, without the
run's own log, cannot reproduce which named members executed-and-passed
versus which were accounted-but-skipped, nor their count.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace


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
    (tests_dir / "test_group.py").write_text(
        "import pytest\n\n\n"
        "def test_pass():\n    assert True\n\n\n"
        "@pytest.mark.skip(reason='no network access in CI')\n"
        "def test_skip_a():\n    assert True\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one pass, one named skip")
    return root


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        max_retries=1,
        retry_delay_seconds=0,
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=False,
        run_review=False,
        callback_url="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-601",
        "name": "verify-first file target composition",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py"],
    }
    defaults.update(overrides)
    return Task(**defaults)


def test_evidence_names_composition_with_per_member_outcome_and_size(tmp_path):
    """BEH-20: a green file-target run's evidence, read back from state,
    must name its composition by member — the executed-and-passed member and
    the accounted-but-skipped member each recorded on their own, with the
    skipped member's own reason — and its size, so a reader of the evidence
    alone (no log) can answer which tests ran and which were skipped."""
    root = _repo(tmp_path)
    task = _task()
    config = _cfg(root)

    result = run_live_verify(task, config)
    assert result.passed, result.detail

    state = ExecutorState(config)
    state.record_verify_evidence(task=task, config=config, result=result)
    namespace = resolve_namespace(config)
    evidence = state.verify_evidence(namespace, task.id)
    state.close()
    assert evidence is not None

    composition = getattr(evidence, "composition", ())
    assert composition, (
        "BEH-20: verify-evidence carries no per-member composition at all — "
        "a reader cannot tell which of the file target's tests ran and "
        "which were skipped without the original run log"
    )
    assert len(composition) == 2, (
        "BEH-20: composition must name both accounted members with their own "
        "outcome (executed-and-passed 'test_pass', skipped 'test_skip_a'), "
        f"got {composition!r}"
    )
    composition_text = repr(composition)
    assert "test_pass" in composition_text, (
        f"BEH-20: executed-and-passed member 'test_pass' not named in {composition!r}"
    )
    assert "test_skip_a" in composition_text and "no network access in CI" in composition_text, (
        f"BEH-20: skipped member 'test_skip_a' must be named with its own reason, "
        f"got {composition!r}"
    )
