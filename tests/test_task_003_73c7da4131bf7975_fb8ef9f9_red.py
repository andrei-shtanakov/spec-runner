"""RED for TASK-003 (DT-03): the fold from a file target's per-member report
into the outcome triplet, and what BEH-16 requires of the evidence a green-
with-skips run leaves behind.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-03
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-16

kind: integration — real git/pytest subprocesses against a fixture repo,
mirroring tests/test_verify_file_composition.py's fixtures but for BEH-16's
own clause: a partially-skipped file target is green, and each skipped
member is named in the evidence BY NAME and WITH ITS REASON — the asymmetry
BEH-16 draws against the node-id rule (BEH-06), which is paid for with
visibility, not silence. Today `run_live_verify`'s green-path detail for a
file target is built from the raw declared selectors
(`", ".join(task.verifies)`), never from the resolved composition, so no
skipped member's name or reason reaches it at all.
"""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


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


class TestFileTargetGreenWithSkipsNamesTheSkippedMemberAndReason:
    """kind: integration — BEH-16: a declared file target where one member
    is skipped by the project's own condition (here: `pytest.mark.skip`
    with a reason) and the rest pass stays green, AND the skipped member is
    named in the evidence by its node id together with its skip reason —
    "this test does not apply here" is an answer, not silence, and it must
    not be paid for with the same silence BEH-06's node-id rule accepts."""

    def test_skipped_member_is_named_with_its_reason_in_the_green_evidence(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "def test_a():\n    assert True\n\n\n"
            "@pytest.mark.skip(reason='not applicable on this platform')\n"
            "def test_b():\n    assert True\n"
        )
        _commit(root, "test_a passes, test_b is skipped with a named reason")

        task = Task(
            id="TASK-301",
            name="verify-first file target",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
            verifies=["tests/test_group.py"],
        )
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.ran and result.passed, (
            "a partially-skipped file target with at least one executed, "
            f"passing member must be green: {result.detail}"
        )
        assert "test_b" in result.detail, (
            "BEH-16: the skipped member must be named by its node id in the "
            f"evidence, not just the declared file: {result.detail}"
        )
        assert "not applicable on this platform" in result.detail, (
            "BEH-16: the skipped member's own reason must be carried into "
            f"the evidence, not only that it was skipped: {result.detail}"
        )
