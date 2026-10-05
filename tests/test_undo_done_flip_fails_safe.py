"""`_undo_done_flip` never raises over the refusal it accompanies (final pre-acceptance).

`current_branch` ran outside the helper's `try`: with git missing, a
`FileNotFoundError` escaped a helper documented as best effort.
"""

from spec_runner.config import ExecutorConfig
from spec_runner.task import Task


def test_git_missing_does_not_escape(tmp_path, monkeypatch):
    from spec_runner import git_ops, hooks

    (tmp_path / "spec").mkdir()
    tasks = tmp_path / "spec" / "tasks.md"
    tasks.write_text("# Tasks\n\n### TASK-001: t\n🟠 P1 | ✅ DONE\nEst: 1d\n")
    before = "# Tasks\n\n### TASK-001: t\n🟠 P1 | 🔄 IN_PROGRESS\nEst: 1d\n"

    def no_git(config):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_ops, "current_branch", no_git)
    cfg = ExecutorConfig(project_root=tmp_path, create_git_branch=True)
    task = Task(id="TASK-001", name="t", priority="p1", status="done", estimate="1d")

    note = hooks._undo_done_flip(task, cfg, before)

    assert tasks.read_text() == before, "the flip was not put back"
    assert "working tree only" in note
