"""RED for TASK-005 (BEH-25, DT-05): `record_verify_group_claims` must accept
a file-target element of a declared verify-first group — parsed with the
declared-group-element vocabulary (`tdd_runners.parse_group_element`), not
`adapter.parse_selector`, which only ever recognises a node id and refuses
anything without `::` as `not_a_node_id`. Today the function still calls
`adapter.parse_selector` directly, so a legal file-target declaration is
refused with `ClaimRefused` instead of being byte-locked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.claims import ClaimRefused, record_verify_group_claims
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _base_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    (root / "tests" / "test_group.py").write_text("def test_it():\n    assert 2 + 2 == 4\n")
    (root / "spec").mkdir()
    (root / "spec" / "tasks.md").write_text(
        "# Tasks\n\n### TASK-101: verify-first task\nP1 | TODO Est: 1h\n"
    )
    (root / "spec" / ".gitignore").write_text(".executor-*\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
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
        auto_commit=True,
        run_review=False,
        callback_url="",
        lint_command="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_file_target_element_is_byte_locked_not_refused(tmp_path):
    """BEH-25: a declared file target ("tests/test_group.py", no `::`) must
    freeze that file exactly like a node-id selector for it already does —
    not raise `ClaimRefused` for failing `parse_selector`'s node-id-only
    vocabulary."""
    root = _base_repo(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    config = _cfg(root)
    task = Task(
        id="TASK-101",
        name="verify-first task",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=["tests/test_group.py"],
    )
    namespace = resolve_namespace(config)

    with ExecutorState(config) as state:
        try:
            claims = record_verify_group_claims(config, state, task, head, ["tests/test_group.py"])
        except ClaimRefused as exc:
            raise AssertionError(
                "BEH-25: a legal file-target element of the declared group "
                f"must be claimable, not refused as an unparseable selector: {exc}"
            ) from exc

        assert [c.path for c in claims] == ["tests/test_group.py"]
        assert [c.path for c in state.active_claims(namespace)] == ["tests/test_group.py"]
