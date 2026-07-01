"""Tests for spec_runner.validate module."""

from pathlib import Path

from spec_runner.task import Task
from spec_runner.validate import (
    ValidationResult,
    _detect_cycle,
    format_results,
    validate_all,
    validate_config,
    validate_design,
    validate_requirements,
    validate_spec_stage,
    validate_task_fields,
    validate_tasks,
    verdict_from_result,
)


def _make_task(
    task_id: str = "TASK-001",
    name: str = "Test task",
    priority: str = "p0",
    status: str = "todo",
    depends_on: list[str] | None = None,
    estimate: str = "1d",
    traces_to: list[str] | None = None,
) -> Task:
    """Helper to create a Task with sensible defaults."""
    return Task(
        id=task_id,
        name=name,
        priority=priority,
        status=status,
        estimate=estimate,
        depends_on=depends_on or [],
        traces_to=traces_to or [],
    )


# --- Minimal tasks.md content for testing ---

VALID_TASKS_MD = """\
## Milestone 1: Core

### TASK-001: First task
🔴 P0 | ⬜ todo | Est: 1d

**Checklist:**
- [ ] Step one

**Depends on:** —
**Blocks:** [TASK-002]

### TASK-002: Second task
🟠 P1 | ⬜ todo | Est: 2d

**Depends on:** [TASK-001]
**Blocks:** —
"""

CIRCULAR_TASKS_MD = """\
### TASK-001: First task
🔴 P0 | ⬜ todo | Est: 1d

**Depends on:** [TASK-002]

### TASK-002: Second task
🟠 P1 | ⬜ todo | Est: 2d

**Depends on:** [TASK-001]
"""


class TestValidateTasksExist:
    """File existence and basic parsing checks."""

    def test_missing_file(self, tmp_path: Path) -> None:
        result = validate_tasks(tmp_path / "nonexistent.md")
        assert not result.ok
        assert any("not found" in e.lower() or "does not exist" in e.lower() for e in result.errors)

    def test_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "tasks.md"
        empty.write_text("")
        result = validate_tasks(empty)
        assert not result.ok
        assert any("no tasks" in e.lower() for e in result.errors)

    def test_valid_file(self, tmp_path: Path) -> None:
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(VALID_TASKS_MD)
        result = validate_tasks(tasks_file)
        assert result.ok
        assert result.errors == []


class TestValidateDependencies:
    """Dependency reference and cycle checks."""

    def test_missing_dep_ref(self) -> None:
        tasks = [
            _make_task("TASK-001", depends_on=["TASK-999"]),
        ]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("TASK-999" in e for e in result.errors)

    def test_circular_dep(self) -> None:
        tasks = [
            _make_task("TASK-001", depends_on=["TASK-002"]),
            _make_task("TASK-002", depends_on=["TASK-001"]),
        ]
        result = _detect_cycle(tasks)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)

    def test_valid_chain(self) -> None:
        tasks = [
            _make_task("TASK-001"),
            _make_task("TASK-002", depends_on=["TASK-001"]),
            _make_task("TASK-003", depends_on=["TASK-002"]),
        ]
        result = _detect_cycle(tasks)
        assert result.ok


class TestValidateStatusAndPriority:
    """Status and priority field validation."""

    def test_invalid_status(self) -> None:
        tasks = [_make_task("TASK-001", status="running")]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("status" in e.lower() for e in result.errors)

    def test_invalid_priority(self) -> None:
        tasks = [_make_task("TASK-001", priority="critical")]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("priority" in e.lower() for e in result.errors)

    def test_multiple_errors_accumulated(self) -> None:
        """A task with both invalid status AND priority produces 2 errors."""
        tasks = [_make_task("TASK-001", priority="p9", status="invalid")]
        result = validate_task_fields(tasks)
        assert len(result.errors) >= 2


class TestCircularDepFile:
    """Cycle detection via file-based validation."""

    def test_circular_dep_via_file(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(CIRCULAR_TASKS_MD)
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)


class TestDfsCycleRegression:
    """Regression tests for DFS cycle detection on complex graphs."""

    def test_complex_cycle_no_crash(self, tmp_path: Path) -> None:
        """Regression: DFS must not crash on complex graphs with cycles."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-A: A\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-B, TASK-C\n\n"
            "### TASK-B: B\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-D\n\n"
            "### TASK-C: C\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-D\n\n"
            "### TASK-D: D\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-B\n"
        )
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)


class TestValidateWarnings:
    """Warning-level checks in validate_task_fields."""

    def test_missing_estimate_warning(self) -> None:
        """Tasks with empty estimate should produce a warning."""
        tasks = [_make_task("TASK-001", estimate="")]
        result = validate_task_fields(tasks)
        assert result.ok  # warnings don't affect ok
        assert len(result.warnings) >= 1
        assert any("estimate" in w.lower() for w in result.warnings)

    def test_blocked_without_deps_warning(self) -> None:
        """Blocked task with no depends_on should produce a warning."""
        tasks = [_make_task("TASK-001", status="blocked", depends_on=[])]
        result = validate_task_fields(tasks)
        assert result.ok
        assert len(result.warnings) >= 1
        assert any("blocked" in w.lower() and "depend" in w.lower() for w in result.warnings)

    def test_missing_traceability_warning(self) -> None:
        """Tasks with empty traces_to should produce a warning."""
        tasks = [_make_task("TASK-001", traces_to=[])]
        result = validate_task_fields(tasks)
        assert result.ok
        assert len(result.warnings) >= 1
        assert any("trace" in w.lower() or "traceability" in w.lower() for w in result.warnings)

    def test_no_warnings_when_all_fields_present(self) -> None:
        """A fully-specified task should not produce warnings."""
        tasks = [
            _make_task(
                "TASK-001",
                estimate="2d",
                traces_to=["REQ-001"],
                depends_on=["TASK-002"],
                status="blocked",
            ),
            _make_task("TASK-002", estimate="1d", traces_to=["REQ-002"]),
        ]
        result = validate_task_fields(tasks)
        assert result.ok
        assert result.warnings == []


class TestDuplicateTaskIds:
    """Duplicate task IDs should be flagged as errors."""

    def test_duplicate_id_is_error(self) -> None:
        tasks = [
            _make_task("TASK-001", name="First"),
            _make_task("TASK-001", name="Duplicate"),
            _make_task("TASK-002", name="Other"),
        ]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("duplicate task ID" in e for e in result.errors)

    def test_unique_ids_no_error(self) -> None:
        tasks = [
            _make_task("TASK-001"),
            _make_task("TASK-002"),
        ]
        result = validate_task_fields(tasks)
        assert not any("duplicate" in e for e in result.errors)


class TestBlocksDependsSymmetry:
    """Asymmetric blocks/depends_on should produce warnings."""

    def test_asymmetric_blocks_warns(self) -> None:
        t1 = _make_task("TASK-001")
        t1.blocks = ["TASK-002"]
        t2 = _make_task("TASK-002")  # depends_on is empty — asymmetric
        result = validate_task_fields([t1, t2])
        assert any(
            "blocks TASK-002" in w and "does not list TASK-001" in w for w in result.warnings
        )

    def test_symmetric_blocks_no_warning(self) -> None:
        t1 = _make_task("TASK-001")
        t1.blocks = ["TASK-002"]
        t2 = _make_task("TASK-002", depends_on=["TASK-001"])
        result = validate_task_fields([t1, t2])
        assert not any("blocks" in w and "does not list" in w for w in result.warnings)


class TestValidateConfig:
    """Config YAML validation with unknown key detection."""

    def test_valid_config(self, tmp_path: Path) -> None:
        """Config with known keys should pass without errors."""
        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text(
            "executor:\n  max_retries: 5\n  hooks:\n    pre_start:\n      create_git_branch: true\n"
        )
        result = validate_config(config_file)
        assert result.ok
        assert result.errors == []

    def test_unknown_key(self, tmp_path: Path) -> None:
        """Unknown key under executor: should produce error with suggestion."""
        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text(
            "executor:\n  max_retires: 5\n"  # typo: retires instead of retries
        )
        result = validate_config(config_file)
        assert not result.ok
        assert any("max_retires" in e for e in result.errors)
        assert any("did you mean" in e.lower() for e in result.errors)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        """Malformed YAML should produce an error."""
        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retries: [invalid\n")
        result = validate_config(config_file)
        assert not result.ok
        assert any("yaml" in e.lower() or "parse" in e.lower() for e in result.errors)

    def test_missing_config_is_ok(self, tmp_path: Path) -> None:
        """Missing config file should be ok (defaults are used)."""
        result = validate_config(tmp_path / "nonexistent.yaml")
        assert result.ok
        assert result.errors == []


class TestValidateAll:
    """Tests for validate_all orchestrator function."""

    def test_validate_all_clean(self, tmp_path: Path) -> None:
        """Clean tasks file with no config should pass."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: Setup\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Traces to:** [REQ-001]\n"
        )
        result = validate_all(tasks_file=tasks_file)
        assert result.ok

    def test_validate_all_with_bad_config(self, tmp_path: Path) -> None:
        """Tasks file + config with unknown key should fail."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text("# Tasks\n\n### TASK-001: Setup\n🔴 P0 | ⬜ todo | Est: 1d\n\n")
        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retry: 5\n")
        result = validate_all(tasks_file=tasks_file, config_file=config_file)
        assert not result.ok

    def test_validate_all_tasks_only(self, tmp_path: Path) -> None:
        """Only tasks_file provided, no config."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(VALID_TASKS_MD)
        result = validate_all(tasks_file=tasks_file)
        assert result.ok

    def test_validate_all_config_only(self, tmp_path: Path) -> None:
        """Only config_file provided, no tasks."""
        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retries: 5\n")
        result = validate_all(config_file=config_file)
        assert result.ok

    def test_validate_all_nothing(self) -> None:
        """No files provided returns clean result."""
        result = validate_all()
        assert result.ok


class TestFormatResults:
    """Tests for format_results terminal formatting."""

    def test_format_clean(self) -> None:
        """Clean result shows 0 errors, 0 warnings."""
        result = ValidationResult()
        output = format_results(result)
        assert "0 errors" in output
        assert "0 warnings" in output

    def test_format_with_errors_and_warnings(self) -> None:
        """Result with errors and warnings formats correctly."""
        result = ValidationResult(
            errors=["TASK-001: depends on TASK-999 which does not exist"],
            warnings=["TASK-002: missing estimate"],
        )
        output = format_results(result)
        assert "1 error" in output
        assert "1 warning" in output
        assert "TASK-999" in output

    def test_format_singular_words(self) -> None:
        """Singular error/warning when count is 1."""
        result = ValidationResult(
            errors=["one error"],
            warnings=["one warning"],
        )
        output = format_results(result)
        assert "1 error," in output
        assert "1 warning" in output
        # Must NOT say "errors" (plural) for count 1
        assert "1 errors" not in output
        assert "1 warnings" not in output

    def test_format_plural_words(self) -> None:
        """Plural errors/warnings when count > 1."""
        result = ValidationResult(
            errors=["err1", "err2"],
            warnings=["w1", "w2", "w3"],
        )
        output = format_results(result)
        assert "2 errors" in output
        assert "3 warnings" in output

    def test_format_errors_prefixed_with_x(self) -> None:
        """Errors are prefixed with 'x'."""
        result = ValidationResult(errors=["some error"])
        output = format_results(result)
        assert "  x some error" in output

    def test_format_warnings_prefixed_with_bang(self) -> None:
        """Warnings are prefixed with '!'."""
        result = ValidationResult(warnings=["some warning"])
        output = format_results(result)
        assert "  ! some warning" in output


class TestValidateFlatConfig:
    """Flat v2.0 config validation (no executor: wrapper)."""

    def test_validate_config_flags_unknown_key_in_flat_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text("claude_command: codex\nnonsense_key: 1\n")
        result = validate_config(cfg)
        assert any("nonsense_key" in e for e in result.errors)

    def test_validate_config_accepts_known_flat_keys(self, tmp_path: Path) -> None:
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text(
            "claude_command: codex\n"
            "review_command: claude\n"
            "budget_usd: 5.0\n"
            "hooks:\n"
            "  pre_start:\n"
            "    create_git_branch: true\n"
        )
        result = validate_config(cfg)
        assert result.ok

    def test_validate_config_flat_dead_section_is_warning_not_error(self, tmp_path: Path) -> None:
        cfg = tmp_path / "spec-runner.config.yaml"
        cfg.write_text("claude_command: codex\nexecution_order: [a, b]\n")
        result = validate_config(cfg)
        assert not any("execution_order" in e for e in result.errors)
        assert any("execution_order" in w for w in result.warnings)


GOOD_REQ = """# Requirements

## Out of Scope
- nothing yet

#### REQ-001: Widget spins
**Acceptance Criteria:**
GIVEN a widget WHEN started THEN it spins
"""

BAD_REQ_NO_SCOPE = """#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""


def test_validate_requirements_ok(tmp_path: Path) -> None:
    p = tmp_path / "requirements.md"
    p.write_text(GOOD_REQ)
    assert validate_requirements(p).ok


def test_validate_requirements_missing_out_of_scope(tmp_path: Path) -> None:
    p = tmp_path / "requirements.md"
    p.write_text(BAD_REQ_NO_SCOPE)
    r = validate_requirements(p)
    assert not r.ok
    assert any("Out of Scope" in e for e in r.errors)


def test_validate_design_dangling_req(tmp_path: Path) -> None:
    (tmp_path / "requirements.md").write_text(GOOD_REQ)
    design = tmp_path / "design.md"
    design.write_text("### DESIGN-001: C\ntraces to [REQ-999]\n")
    r = validate_design(design)
    assert not r.ok
    assert any("REQ-999" in e for e in r.errors)


def test_verdict_levels() -> None:
    ok = ValidationResult()
    assert verdict_from_result(ok) == "pass"
    warn = ValidationResult(warnings=["w"])
    assert verdict_from_result(warn) == "warn"
    fail = ValidationResult(errors=["e"])
    assert verdict_from_result(fail) == "fail"


class TestValidateSpecStageDispatch:
    """Tests for validate_spec_stage dispatcher function."""

    @staticmethod
    def _stage_cfg(tmp_path: Path):
        """Create a mock config with stage file paths."""
        from types import SimpleNamespace

        return SimpleNamespace(
            requirements_file=tmp_path / "requirements.md",
            design_file=tmp_path / "design.md",
            tasks_file=tmp_path / "tasks.md",
        )

    def test_validate_spec_stage_requirements_dispatch(self, tmp_path: Path) -> None:
        """Test dispatch to validate_requirements for 'requirements' stage."""
        cfg = self._stage_cfg(tmp_path)
        cfg.requirements_file.write_text(GOOD_REQ)
        result = validate_spec_stage("requirements", cfg)
        assert result.ok, f"requirements validation should pass: {result.errors}"

    def test_validate_spec_stage_design_dispatch(self, tmp_path: Path) -> None:
        """Test dispatch to validate_design for 'design' stage."""
        cfg = self._stage_cfg(tmp_path)
        # Write requirements with REQ-001 so design can reference it
        cfg.requirements_file.write_text(GOOD_REQ)
        # Design with valid DESIGN ID and REQ reference that exists
        cfg.design_file.write_text("### DESIGN-001: Component\ntraces to [REQ-001]\n")
        result = validate_spec_stage("design", cfg)
        assert result.ok, f"design validation should pass: {result.errors}"

    def test_validate_spec_stage_tasks_dispatch(self, tmp_path: Path) -> None:
        """Test dispatch to validate_tasks for 'tasks' stage."""
        cfg = self._stage_cfg(tmp_path)
        # Write a minimal valid task
        cfg.tasks_file.write_text(
            "### TASK-001: First task\n🔴 P0 | ⬜ todo | Est: 1d\n\n**Traces to:** [REQ-001]\n"
        )
        result = validate_spec_stage("tasks", cfg)
        # Dispatch succeeded if we got a ValidationResult without exception
        assert isinstance(result, ValidationResult)
        assert isinstance(result.errors, list)

    def test_validate_spec_stage_unknown_raises_error(self, tmp_path: Path) -> None:
        """Test that unknown stage raises ValueError."""
        import pytest

        cfg = self._stage_cfg(tmp_path)
        with pytest.raises(ValueError, match="unknown stage"):
            validate_spec_stage("bogus", cfg)
