"""Tests for spec_runner.config module."""

from argparse import Namespace
from pathlib import Path

from spec_runner.config import (
    ERROR_PATTERNS,
    ExecutorConfig,
    ExecutorLock,
    build_config,
    load_config_from_yaml,
)


class TestExecutorConfig:
    def test_defaults(self):
        c = ExecutorConfig()
        assert c.max_retries == 3
        assert c.retry_delay_seconds == 5
        assert c.claude_command == "claude"
        assert c.on_task_failure == "skip"

    def test_project_root_resolved_to_absolute(self):
        c = ExecutorConfig(project_root=Path("."))
        assert c.project_root.is_absolute()

    def test_state_file_resolved_to_absolute(self):
        c = ExecutorConfig()
        assert c.state_file.is_absolute()
        assert str(c.state_file).endswith("spec/.executor-state.db")

    def test_logs_dir_resolved_to_absolute(self):
        c = ExecutorConfig()
        assert c.logs_dir.is_absolute()
        assert str(c.logs_dir).endswith("spec/.executor-logs")

    def test_spec_prefix_namespaces_state_file(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert "phase2-" in str(c.state_file)

    def test_spec_prefix_namespaces_tasks_file(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert c.tasks_file.name == "phase2-tasks.md"

    def test_stop_file_property(self):
        c = ExecutorConfig()
        assert c.stop_file == c.project_root / "spec" / ".executor-stop"


class TestConfigStateFileDefault:
    def test_default_state_file_is_db(self):
        c = ExecutorConfig()
        assert str(c.state_file).endswith(".executor-state.db")

    def test_spec_prefix_state_file_is_db(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert str(c.state_file).endswith(".executor-phase2-state.db")


class TestLoadConfigFromYaml:
    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        result = load_config_from_yaml(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_loads_yaml_values(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("executor:\n  max_retries: 5\n  claude_model: opus\n")
        result = load_config_from_yaml(cfg)
        assert result["max_retries"] == 5
        assert result["claude_model"] == "opus"

    def test_returns_empty_dict_for_invalid_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(": invalid: yaml: [")
        result = load_config_from_yaml(cfg)
        assert result == {}

    def test_loads_hooks_from_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "executor:\n"
            "  hooks:\n"
            "    pre_start:\n"
            "      create_git_branch: false\n"
            "    post_done:\n"
            "      run_tests: false\n"
            "      auto_commit: false\n"
        )
        result = load_config_from_yaml(cfg)
        assert result["create_git_branch"] is False
        assert result["run_tests_on_done"] is False
        assert result["auto_commit"] is False

    def test_loads_commands_from_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("executor:\n  commands:\n    test: pytest -x\n    lint: ruff check .\n")
        result = load_config_from_yaml(cfg)
        assert result["test_command"] == "pytest -x"
        assert result["lint_command"] == "ruff check ."

    def test_loads_plugins_dir_from_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("executor:\n  paths:\n    plugins: custom/plugins\n")
        result = load_config_from_yaml(cfg)
        assert result["plugins_dir"] == Path("custom/plugins")

    def test_plugins_dir_none_when_not_set(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("executor:\n  max_retries: 3\n")
        result = load_config_from_yaml(cfg)
        assert result.get("plugins_dir") is None


class TestBuildConfig:
    def _default_args(self, **overrides) -> Namespace:
        """Create a Namespace with default CLI arg values."""
        defaults = {
            "max_retries": 3,
            "timeout": 30,
            "no_tests": False,
            "no_branch": False,
            "no_commit": False,
            "no_review": False,
            "callback_url": "",
            "spec_prefix": "",
            "project_root": None,
        }
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_cli_overrides_yaml(self):
        yaml_config = {"max_retries": 5}
        args = self._default_args(max_retries=10)
        config = build_config(yaml_config, args)
        assert config.max_retries == 10

    def test_yaml_overrides_defaults(self):
        yaml_config = {"retry_delay_seconds": 30}
        args = self._default_args()
        config = build_config(yaml_config, args)
        assert config.retry_delay_seconds == 30

    def test_no_tests_flag(self):
        args = self._default_args(no_tests=True)
        config = build_config({}, args)
        assert config.run_tests_on_done is False

    def test_no_branch_flag(self):
        args = self._default_args(no_branch=True)
        config = build_config({}, args)
        assert config.create_git_branch is False

    def test_no_commit_flag(self):
        args = self._default_args(no_commit=True)
        config = build_config({}, args)
        assert config.auto_commit is False

    def test_spec_prefix_from_cli(self):
        args = self._default_args(spec_prefix="phase3-")
        config = build_config({}, args)
        assert config.spec_prefix == "phase3-"
        assert config.tasks_file.name == "phase3-tasks.md"


class TestExecutorLock:
    def test_acquire_and_release(self, tmp_path):
        lock = ExecutorLock(tmp_path / "test.lock")
        assert lock.acquire() is True
        assert lock.lock_path.exists()
        lock.release()

    def test_double_acquire_fails(self, tmp_path):
        lock1 = ExecutorLock(tmp_path / "test.lock")
        lock2 = ExecutorLock(tmp_path / "test.lock")
        assert lock1.acquire() is True
        assert lock2.acquire() is False
        lock1.release()


class TestBudgetConfig:
    def test_budget_defaults_none(self):
        config = ExecutorConfig()
        assert config.budget_usd is None
        assert config.task_budget_usd is None

    def test_budget_from_kwargs(self):
        config = ExecutorConfig(budget_usd=10.0, task_budget_usd=2.0)
        assert config.budget_usd == 10.0
        assert config.task_budget_usd == 2.0

    def test_max_concurrent_default(self):
        config = ExecutorConfig()
        assert config.max_concurrent == 3


class TestErrorPatterns:
    def test_contains_rate_limit(self):
        assert any("rate limit" in p.lower() for p in ERROR_PATTERNS)

    def test_contains_context_window(self):
        assert any("context window" in p.lower() for p in ERROR_PATTERNS)

    def test_is_non_empty_list(self):
        assert isinstance(ERROR_PATTERNS, list)
        assert len(ERROR_PATTERNS) > 0


class TestHitlReviewConfig:
    def test_hitl_review_default_false(self):
        config = ExecutorConfig()
        assert config.hitl_review is False

    def test_hitl_review_from_kwargs(self):
        config = ExecutorConfig(hitl_review=True)
        assert config.hitl_review is True

    def test_hitl_review_from_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("executor:\n  hitl_review: true\n")
        result = load_config_from_yaml(cfg)
        assert result["hitl_review"] is True

    def test_hitl_review_cli_override(self):
        args = Namespace(
            max_retries=3,
            timeout=30,
            no_tests=False,
            no_branch=False,
            no_commit=False,
            no_review=False,
            hitl_review=True,
            callback_url="",
            spec_prefix="",
            project_root=None,
        )
        config = build_config({}, args)
        assert config.hitl_review is True


class TestLoggingConfig:
    def test_log_level_default(self):
        config = ExecutorConfig()
        assert config.log_level == "info"

    def test_log_level_from_kwargs(self):
        config = ExecutorConfig(log_level="debug")
        assert config.log_level == "debug"


class TestLockDiagnostics:
    def test_read_lock_info_returns_pid_and_started(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        lock = ExecutorLock(lock_path)
        lock_path.write_text("PID: 12345\nStarted: 2026-02-23T10:00:00\n")
        info = lock._read_lock_info()
        assert info["pid"] == "12345"
        assert info["started"] == "2026-02-23T10:00:00"

    def test_read_lock_info_empty_file(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        lock = ExecutorLock(lock_path)
        lock_path.write_text("")
        info = lock._read_lock_info()
        assert info == {}

    def test_read_lock_info_missing_file(self, tmp_path):
        lock_path = tmp_path / "nonexistent.lock"
        lock = ExecutorLock(lock_path)
        info = lock._read_lock_info()
        assert info == {}

    def test_is_pid_alive_current_process(self):
        import os

        assert ExecutorLock._is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_dead_process(self):
        assert ExecutorLock._is_pid_alive(99999999) is False


class TestTimeoutConfig:
    def test_session_timeout_default_disabled(self):
        config = ExecutorConfig()
        assert config.session_timeout_minutes == 0

    def test_idle_timeout_default_disabled(self):
        config = ExecutorConfig()
        assert config.idle_timeout_minutes == 0

    def test_session_timeout_from_kwargs(self):
        config = ExecutorConfig(session_timeout_minutes=60)
        assert config.session_timeout_minutes == 60

    def test_idle_timeout_from_kwargs(self):
        config = ExecutorConfig(idle_timeout_minutes=15)
        assert config.idle_timeout_minutes == 15

    def test_timeouts_from_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "executor:\n  session_timeout_minutes: 120\n  idle_timeout_minutes: 30\n"
        )
        data = load_config_from_yaml(config_path)
        assert data["session_timeout_minutes"] == 120
        assert data["idle_timeout_minutes"] == 30


class TestConstitutionConfig:
    def test_constitution_file_property(self):
        config = ExecutorConfig()
        assert str(config.constitution_file).endswith("spec/constitution.md")

    def test_constitution_file_with_prefix(self):
        config = ExecutorConfig(spec_prefix="phase2-")
        assert str(config.constitution_file).endswith("spec/phase2-constitution.md")


class TestPersonaConfig:
    def test_personas_default_empty(self):
        config = ExecutorConfig()
        assert config.personas == {}

    def test_personas_from_kwargs(self):
        from spec_runner.config import Persona

        personas = {
            "implementer": Persona(system_prompt="You are a focused implementer", model="sonnet"),
            "reviewer": Persona(system_prompt="You are a code reviewer", model="haiku"),
        }
        config = ExecutorConfig(personas=personas)
        assert config.personas["implementer"].model == "sonnet"
        assert config.personas["reviewer"].system_prompt == "You are a code reviewer"

    def test_get_persona_returns_none_when_missing(self):
        config = ExecutorConfig()
        assert config.get_persona("implementer") is None

    def test_get_persona_returns_persona(self):
        from spec_runner.config import Persona

        config = ExecutorConfig(
            personas={"implementer": Persona(system_prompt="test", model="opus")}
        )
        p = config.get_persona("implementer")
        assert p is not None
        assert p.model == "opus"

    def test_get_model_for_role_uses_persona(self):
        from spec_runner.config import Persona

        config = ExecutorConfig(
            claude_model="default-model",
            personas={"implementer": Persona(model="persona-model")},
        )
        assert config.get_model_for_role("implementer") == "persona-model"

    def test_get_model_for_role_falls_back_to_claude_model(self):
        config = ExecutorConfig(claude_model="default-model")
        assert config.get_model_for_role("implementer") == "default-model"

    def test_get_model_for_role_persona_empty_model_falls_back(self):
        from spec_runner.config import Persona

        config = ExecutorConfig(
            claude_model="default-model",
            personas={"implementer": Persona(system_prompt="test", model="")},
        )
        assert config.get_model_for_role("implementer") == "default-model"

    def test_personas_from_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "executor:\n"
            "  personas:\n"
            "    implementer:\n"
            "      system_prompt: 'You are a focused implementer'\n"
            "      model: sonnet\n"
            "      focus: ['src/', 'tasks.md']\n"
            "    reviewer:\n"
            "      system_prompt: 'You are a code reviewer'\n"
            "      model: haiku\n"
        )
        data = load_config_from_yaml(config_path)
        personas = data["personas"]
        assert "implementer" in personas
        assert personas["implementer"].model == "sonnet"
        assert personas["implementer"].focus == ["src/", "tasks.md"]
        assert personas["reviewer"].model == "haiku"
