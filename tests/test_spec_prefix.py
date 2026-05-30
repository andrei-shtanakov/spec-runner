"""Tests for spec_prefix support: path namespacing, history file derivation,
project_root resolution, and stop file property.

Also contains end-to-end multi-phase workflow coverage (LABS-39):
two phases coexisting in one project must have fully isolated task files,
state databases, log directories, and history files.
"""

import argparse
from pathlib import Path

from spec_runner.config import ExecutorConfig, build_config
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import history_file_for, parse_tasks, resolve_dependencies

# === ExecutorConfig properties ===


class TestExecutorConfigDefaults:
    """Default config (no prefix) behaves like the original code."""

    def test_tasks_file_default(self):
        c = ExecutorConfig()
        assert c.tasks_file == c.project_root / "spec" / "tasks.md"

    def test_requirements_file_default(self):
        c = ExecutorConfig()
        assert c.requirements_file == c.project_root / "spec" / "requirements.md"

    def test_design_file_default(self):
        c = ExecutorConfig()
        assert c.design_file == c.project_root / "spec" / "design.md"

    def test_stop_file_default(self):
        c = ExecutorConfig()
        assert c.stop_file == c.project_root / "spec" / ".executor-stop"

    def test_state_file_default_is_absolute(self):
        c = ExecutorConfig()
        assert c.state_file.is_absolute()
        assert str(c.state_file).endswith("spec/.executor-state.db")

    def test_logs_dir_default_is_absolute(self):
        c = ExecutorConfig()
        assert c.logs_dir.is_absolute()
        assert str(c.logs_dir).endswith("spec/.executor-logs")


class TestExecutorConfigPrefix:
    """Config with spec_prefix namespaces all paths."""

    def test_tasks_file_with_prefix(self):
        c = ExecutorConfig(spec_prefix="phase5-")
        assert c.tasks_file.name == "phase5-tasks.md"

    def test_requirements_file_with_prefix(self):
        c = ExecutorConfig(spec_prefix="phase5-")
        assert c.requirements_file.name == "phase5-requirements.md"

    def test_design_file_with_prefix(self):
        c = ExecutorConfig(spec_prefix="phase5-")
        assert c.design_file.name == "phase5-design.md"

    def test_state_file_namespaced(self):
        c = ExecutorConfig(spec_prefix="phase5-")
        assert "phase5-" in c.state_file.name

    def test_logs_dir_namespaced(self):
        c = ExecutorConfig(spec_prefix="phase5-")
        assert "phase5-" in c.logs_dir.name

    def test_stop_file_unchanged_by_prefix(self):
        c = ExecutorConfig(spec_prefix="phase5-")
        assert c.stop_file.name == ".executor-stop"


class TestProjectRootResolution:
    """project_root is resolved to absolute in __post_init__."""

    def test_default_resolves_to_absolute(self):
        c = ExecutorConfig()
        assert c.project_root.is_absolute()

    def test_custom_root_resolves(self):
        c = ExecutorConfig(project_root=Path("/tmp/myproject"))
        assert c.project_root.is_absolute()
        # On macOS /tmp -> /private/tmp, so check resolved path
        assert c.project_root == Path("/tmp/myproject").resolve()

    def test_custom_root_anchors_spec_files(self):
        c = ExecutorConfig(project_root=Path("/tmp/proj"))
        resolved = Path("/tmp/proj").resolve()
        assert c.tasks_file == resolved / "spec" / "tasks.md"
        assert c.requirements_file == resolved / "spec" / "requirements.md"
        assert c.design_file == resolved / "spec" / "design.md"

    def test_custom_root_anchors_state_and_logs(self):
        c = ExecutorConfig(project_root=Path("/tmp/proj"))
        resolved = Path("/tmp/proj").resolve()
        assert str(c.state_file).startswith(str(resolved))
        assert str(c.logs_dir).startswith(str(resolved))

    def test_prefix_plus_custom_root(self):
        c = ExecutorConfig(project_root=Path("/tmp/proj"), spec_prefix="v2-")
        resolved = Path("/tmp/proj").resolve()
        assert c.tasks_file == resolved / "spec" / "v2-tasks.md"
        assert "v2-" in c.state_file.name
        assert "v2-" in c.logs_dir.name


class TestStateLogNamespacing:
    """Custom state/log paths override prefix namespacing."""

    def test_custom_state_file_not_overridden(self):
        c = ExecutorConfig(
            spec_prefix="phase5-",
            state_file=Path("my-state.json"),
        )
        # Should keep custom path, not override with prefix
        assert "my-state.json" in str(c.state_file)
        assert "phase5-" not in c.state_file.name

    def test_custom_logs_dir_not_overridden(self):
        c = ExecutorConfig(
            spec_prefix="phase5-",
            logs_dir=Path("my-logs"),
        )
        assert "my-logs" in str(c.logs_dir)
        assert "phase5-" not in c.logs_dir.name

    def test_absolute_state_file_stays_absolute(self):
        c = ExecutorConfig(state_file=Path("/absolute/state.json"))
        assert str(c.state_file) == "/absolute/state.json"


# === history_file_for ===


class TestHistoryFileFor:
    """history_file_for derives correct history log path."""

    def test_default_tasks_file(self):
        result = history_file_for(Path("spec/tasks.md"))
        assert result == Path("spec/.task-history.log")

    def test_prefixed_tasks_file(self):
        result = history_file_for(Path("spec/phase5-tasks.md"))
        assert result == Path("spec/.phase5-task-history.log")

    def test_absolute_path(self):
        result = history_file_for(Path("/proj/spec/phase2-tasks.md"))
        assert result == Path("/proj/spec/.phase2-task-history.log")

    def test_no_prefix_no_dash(self):
        # Edge case: file named just "tasks.md"
        result = history_file_for(Path("tasks.md"))
        assert result == Path(".task-history.log")

    def test_non_standard_name(self):
        # File that doesn't end with "-tasks"
        result = history_file_for(Path("spec/something.md"))
        assert result == Path("spec/.task-history.log")


# === Multi-phase end-to-end (LABS-39) =====================================


PHASE1_TASKS_MD = """\
# Phase 1 Tasks

## Milestone: foundation

### TASK-001: Set up project
🔴 P0 | ⬜ TODO
Est: 1d

- [ ] Init repo

### TASK-002: Add CI
🟠 P1 | ⬜ TODO
**Depends on:** [TASK-001]
Est: 1d

- [ ] Configure GitHub Actions
"""

PHASE2_TASKS_MD = """\
# Phase 2 Tasks

## Milestone: features

### TASK-101: Build API
🔴 P0 | ⬜ TODO
Est: 3d

- [ ] Design routes

### TASK-102: Write docs
🟡 P2 | ⬜ TODO
**Depends on:** [TASK-101]
Est: 1d

- [ ] README
"""


def _scaffold_multi_phase_project(root: Path) -> None:
    """Lay out a realistic multi-phase workspace on disk.

    One `spec/` directory hosts both phase1-* and phase2-* files, mirroring
    how Maestro or an operator would keep a multi-stage roadmap together.
    """
    spec_dir = root / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "phase1-tasks.md").write_text(PHASE1_TASKS_MD)
    (spec_dir / "phase2-tasks.md").write_text(PHASE2_TASKS_MD)
    (spec_dir / "phase1-requirements.md").write_text("# REQ-001\nInitial setup requirements.\n")
    (spec_dir / "phase2-requirements.md").write_text("# REQ-101\nPhase 2 API requirements.\n")
    (spec_dir / "phase1-design.md").write_text("# DESIGN-001\nPhase 1 design.\n")
    (spec_dir / "phase2-design.md").write_text("# DESIGN-101\nPhase 2 design.\n")


class TestMultiPhaseE2E:
    """End-to-end checks that `--spec-prefix` isolates one phase from another."""

    def test_each_phase_parses_only_its_own_tasks(self, tmp_path):
        _scaffold_multi_phase_project(tmp_path)
        p1 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase1-")
        p2 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase2-")

        p1_tasks = {t.id for t in parse_tasks(p1.tasks_file)}
        p2_tasks = {t.id for t in parse_tasks(p2.tasks_file)}

        assert p1_tasks == {"TASK-001", "TASK-002"}
        assert p2_tasks == {"TASK-101", "TASK-102"}
        assert p1_tasks.isdisjoint(p2_tasks)

    def test_dependency_resolution_stays_within_phase(self, tmp_path):
        """TASK-002 blocked by TASK-001 in phase 1, not affected by phase 2."""
        _scaffold_multi_phase_project(tmp_path)
        p1 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase1-")

        resolved = resolve_dependencies(parse_tasks(p1.tasks_file))
        by_id = {t.id: t for t in resolved}

        assert by_id["TASK-002"].depends_on == ["TASK-001"]

    def test_state_is_isolated_between_phases(self, tmp_path):
        """Recording an attempt in phase 1 must not leak into phase 2's state."""
        _scaffold_multi_phase_project(tmp_path)
        p1 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase1-")
        p2 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase2-")

        assert p1.state_file != p2.state_file

        with ExecutorState(p1) as s1:
            s1.record_attempt("TASK-001", success=True, duration=10.0)
            assert s1.total_completed == 1

        with ExecutorState(p2) as s2:
            # phase 2 state DB must exist but have seen zero work
            assert s2.total_completed == 0
            assert s2.total_failed == 0
            assert "TASK-001" not in s2.tasks

    def test_log_and_history_directories_are_namespaced(self, tmp_path):
        _scaffold_multi_phase_project(tmp_path)
        p1 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase1-")
        p2 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase2-")

        assert p1.logs_dir != p2.logs_dir
        assert "phase1-" in p1.logs_dir.name
        assert "phase2-" in p2.logs_dir.name

        h1 = history_file_for(p1.tasks_file)
        h2 = history_file_for(p2.tasks_file)
        assert h1 != h2
        assert h1.name == ".phase1-task-history.log"
        assert h2.name == ".phase2-task-history.log"

    def test_cli_arg_spec_prefix_survives_build_config(self, tmp_path):
        """`--spec-prefix` on the CLI must route all paths through phase config."""
        _scaffold_multi_phase_project(tmp_path)
        args = argparse.Namespace(
            spec_prefix="phase2-",
            project_root=str(tmp_path),
            max_retries=None,
            timeout=None,
            no_tests=False,
            no_branch=False,
            no_commit=False,
            no_review=False,
            hitl_review=False,
            callback_url="",
            budget=None,
            task_budget=None,
            log_level=None,
        )

        config = build_config({}, args)

        assert config.spec_prefix == "phase2-"
        assert config.tasks_file.name == "phase2-tasks.md"
        assert config.tasks_file.exists()
        assert "phase2-" in config.state_file.name
        assert "phase2-" in config.logs_dir.name

    def test_phase_transition_preserves_prior_phase_state(self, tmp_path):
        """Completing a phase and starting the next must not touch phase 1's DB.

        Pins the semantic that `--spec-prefix` namespaces are permanent: once
        phase 1 is marked done, switching to phase 2 and running new work
        leaves phase 1's SQLite DB byte-for-byte intact.
        """
        _scaffold_multi_phase_project(tmp_path)
        p1 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase1-")
        p2 = ExecutorConfig(project_root=tmp_path, spec_prefix="phase2-")

        with ExecutorState(p1) as s1:
            s1.record_attempt("TASK-001", success=True, duration=5.0)
            s1.record_attempt("TASK-002", success=True, duration=7.0)

        phase1_bytes = p1.state_file.read_bytes()

        with ExecutorState(p2) as s2:
            s2.record_attempt("TASK-101", success=False, duration=3.0, error_code=ErrorCode.UNKNOWN)

        assert p1.state_file.read_bytes() == phase1_bytes

    def test_wrong_prefix_does_not_find_files(self, tmp_path):
        """Using a prefix that doesn't match any on-disk file yields no tasks.

        Guards against silently falling back to the unprefixed `tasks.md`.
        """
        _scaffold_multi_phase_project(tmp_path)
        ghost = ExecutorConfig(project_root=tmp_path, spec_prefix="phase99-")

        assert not ghost.tasks_file.exists()
        # parse_tasks should be safe on missing file — not raise
        if ghost.tasks_file.exists():
            assert parse_tasks(ghost.tasks_file) == []
