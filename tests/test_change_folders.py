"""M2: change-as-folder lifecycle.

A change is a self-rooted spec dir at ``spec/changes/<id>/`` selected via
``config.change_id`` (CLI ``--change``). Every path flows through the
``ExecutorConfig`` seam, so the whole toolchain (run, gated pipeline,
governance, verify) scopes to the change automatically; the per-change
state-db also yields a per-change executor lock (it derives from the state
path). The flat ``spec/`` layout and the Maestro contract are untouched —
design: docs/plans/2026-07-13-m2-change-folder-design.md.
"""

from argparse import Namespace
from pathlib import Path

import pytest

from spec_runner.change_commands import (
    cmd_change_archive,
    cmd_change_list,
    cmd_change_new,
    list_changes,
)
from spec_runner.config import ConfigError, ExecutorConfig, build_config


def _args(**overrides) -> Namespace:
    defaults = {
        "max_retries": None,
        "timeout": None,
        "no_tests": False,
        "no_branch": False,
        "no_commit": False,
        "no_review": False,
        "callback_url": "",
        "spec_prefix": "",
        "project_root": None,
        "max_concurrent": 0,
        "budget": None,
        "task_budget": None,
        "hitl_review": False,
        "log_level": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


DONE_TASKS = """# Tasks

### TASK-001: Done thing
P0 | DONE

### TASK-002: Also done
P1 | DONE
"""

MIXED_TASKS = """# Tasks

### TASK-001: Done thing
P0 | DONE

### TASK-002: Not done
P1 | TODO
"""


class TestConfigSeam:
    def test_default_spec_dir_and_paths_unchanged(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path)
        assert cfg.spec_dir == tmp_path / "spec"
        assert cfg.tasks_file == tmp_path / "spec" / "tasks.md"
        assert cfg.state_file == tmp_path / "spec" / ".executor-state.db"

    def test_change_redirects_every_spec_path(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path, change_id="add-dark-mode")
        root = tmp_path / "spec" / "changes" / "add-dark-mode"
        assert cfg.spec_dir == root
        assert cfg.tasks_file == root / "tasks.md"
        assert cfg.requirements_file == root / "requirements.md"
        assert cfg.design_file == root / "design.md"
        assert cfg.constitution_file == root / "constitution.md"
        assert cfg.spec_lock_file == root / ".spec.lock"
        assert cfg.stop_file == root / ".executor-stop"

    def test_change_relocates_default_state_and_logs(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path, change_id="add-x")
        root = tmp_path / "spec" / "changes" / "add-x"
        assert cfg.state_file == root / ".executor-state.db"
        assert cfg.logs_dir == root / ".executor-logs"

    def test_explicit_state_path_wins_over_change(self, tmp_path: Path):
        cfg = ExecutorConfig(
            project_root=tmp_path, change_id="add-x", state_file=Path("custom/state.db")
        )
        assert cfg.state_file == tmp_path / "custom" / "state.db"

    def test_change_plus_spec_prefix_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="spec_prefix"):
            ExecutorConfig(project_root=tmp_path, change_id="add-x", spec_prefix="phase2-")

    @pytest.mark.parametrize("bad", ["Bad_ID", "-lead", "has space", "a/b", "archive", ""])
    def test_invalid_change_id_rejected(self, tmp_path: Path, bad: str):
        if bad == "":
            # empty is the default (flat layout) — must NOT raise
            ExecutorConfig(project_root=tmp_path, change_id=bad)
            return
        with pytest.raises(ConfigError, match="change"):
            ExecutorConfig(project_root=tmp_path, change_id=bad)

    def test_build_config_wires_cli_flag(self, tmp_path: Path):
        cfg = build_config({}, _args(project_root=str(tmp_path), change="add-x"))
        assert cfg.change_id == "add-x"


class TestChangeNew:
    def test_creates_folder_and_tasks_stub(self, tmp_path: Path):
        rc = cmd_change_new(Namespace(change_id="add-x"), ExecutorConfig(project_root=tmp_path))
        assert rc == 0
        tasks = tmp_path / "spec" / "changes" / "add-x" / "tasks.md"
        assert tasks.exists()
        assert "TASK-001" in tasks.read_text()

    def test_refuses_existing(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path)
        assert cmd_change_new(Namespace(change_id="add-x"), cfg) == 0
        assert cmd_change_new(Namespace(change_id="add-x"), cfg) == 2

    def test_rejects_invalid_id(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path)
        assert cmd_change_new(Namespace(change_id="Bad_ID"), cfg) == 2


class TestChangeList:
    def test_lists_in_flight_changes_only(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path)
        cmd_change_new(Namespace(change_id="add-a"), cfg)
        cmd_change_new(Namespace(change_id="add-b"), cfg)
        # archive dir must be excluded
        (tmp_path / "spec" / "changes" / "archive" / "2026-01-01-old").mkdir(parents=True)
        infos = list_changes(cfg)
        assert [i.change_id for i in infos] == ["add-a", "add-b"]

    def test_counts_tasks(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path)
        cmd_change_new(Namespace(change_id="add-a"), cfg)
        (tmp_path / "spec" / "changes" / "add-a" / "tasks.md").write_text(MIXED_TASKS)
        (info,) = list_changes(cfg)
        assert info.total == 2
        assert info.done == 1

    def test_cmd_list_empty_ok(self, tmp_path: Path, capsys):
        assert cmd_change_list(Namespace(json=False), ExecutorConfig(project_root=tmp_path)) == 0
        assert "no changes" in capsys.readouterr().out


class TestChangeArchive:
    def _new_change(self, tmp_path: Path, tasks: str) -> ExecutorConfig:
        cfg = ExecutorConfig(project_root=tmp_path)
        cmd_change_new(Namespace(change_id="add-x"), cfg)
        (tmp_path / "spec" / "changes" / "add-x" / "tasks.md").write_text(tasks)
        return cfg

    def test_archives_when_all_done(self, tmp_path: Path):
        cfg = self._new_change(tmp_path, DONE_TASKS)
        rc = cmd_change_archive(Namespace(change_id="add-x", force=False), cfg)
        assert rc == 0
        assert not (tmp_path / "spec" / "changes" / "add-x").exists()
        archived = list((tmp_path / "spec" / "changes" / "archive").iterdir())
        assert len(archived) == 1
        assert archived[0].name.endswith("-add-x")
        assert (archived[0] / "tasks.md").read_text() == DONE_TASKS

    def test_refuses_undone_tasks(self, tmp_path: Path):
        cfg = self._new_change(tmp_path, MIXED_TASKS)
        rc = cmd_change_archive(Namespace(change_id="add-x", force=False), cfg)
        assert rc == 1
        assert (tmp_path / "spec" / "changes" / "add-x").exists()

    def test_force_overrides_undone(self, tmp_path: Path):
        cfg = self._new_change(tmp_path, MIXED_TASKS)
        assert cmd_change_archive(Namespace(change_id="add-x", force=True), cfg) == 0

    def test_refuses_missing_change(self, tmp_path: Path):
        cfg = ExecutorConfig(project_root=tmp_path)
        assert cmd_change_archive(Namespace(change_id="ghost", force=False), cfg) == 2

    def test_archive_name_collision_gets_suffix(self, tmp_path: Path):
        cfg = self._new_change(tmp_path, DONE_TASKS)
        from spec_runner.change_commands import _archive_dest

        first = _archive_dest(cfg, "add-x")
        first.mkdir(parents=True)
        assert cmd_change_archive(Namespace(change_id="add-x", force=False), cfg) == 0
        assert (first.parent / f"{first.name}-2").exists()

    def test_refuses_while_run_lock_held(self, tmp_path: Path):
        from spec_runner.config import ExecutorLock

        cfg = self._new_change(tmp_path, DONE_TASKS)
        run_cfg = ExecutorConfig(project_root=tmp_path, change_id="add-x")
        lock = ExecutorLock(run_cfg.state_file.with_suffix(".lock"))
        assert lock.acquire()
        try:
            assert cmd_change_archive(Namespace(change_id="add-x", force=False), cfg) == 1
        finally:
            lock.release()


class TestParallelIsolation:
    def test_two_changes_have_independent_state_and_locks(self, tmp_path: Path):
        from spec_runner.config import ExecutorLock
        from spec_runner.state import ExecutorState

        a = ExecutorConfig(project_root=tmp_path, change_id="add-a")
        b = ExecutorConfig(project_root=tmp_path, change_id="add-b")
        assert a.state_file != b.state_file

        lock_a = ExecutorLock(a.state_file.with_suffix(".lock"))
        lock_b = ExecutorLock(b.state_file.with_suffix(".lock"))
        assert lock_a.acquire()
        try:
            assert lock_b.acquire()  # no contention across changes
            lock_b.release()
        finally:
            lock_a.release()

        a.state_file.parent.mkdir(parents=True, exist_ok=True)
        b.state_file.parent.mkdir(parents=True, exist_ok=True)
        with ExecutorState(a) as sa:
            sa.mark_running("TASK-001")
        with ExecutorState(b) as sb:
            assert "TASK-001" not in sb.tasks  # isolated


class TestCliParser:
    def test_run_accepts_change_flag(self):
        from spec_runner.cli import _build_parser

        args = _build_parser().parse_args(["run", "--change", "add-x", "--dry-run"])
        assert args.change == "add-x"

    def test_change_family_parses(self):
        from spec_runner.cli import _build_parser

        p = _build_parser()
        assert p.parse_args(["change", "new", "add-x"]).change_id == "add-x"
        assert p.parse_args(["change", "list", "--json"]).json is True
        a = p.parse_args(["change", "archive", "add-x", "--force"])
        assert a.change_id == "add-x" and a.force is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
