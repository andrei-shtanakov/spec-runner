"""Tests for spec_prefix support: path namespacing, history file derivation,
project_root resolution, and stop file property."""

from pathlib import Path

from spec_runner.executor import ExecutorConfig
from spec_runner.task import history_file_for

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
        assert str(c.state_file).endswith("spec/.executor-state.json")

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
