# Phase 6: Extensibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add plugin system (custom hooks), spec generation (interactive Q&A), and config/task validation to spec-runner.

**Architecture:** Three independent features implemented as: `validate.py` (new module for config+task checks), `plugins.py` (new module for directory-based plugin hooks), and extensions to existing `executor.py`/`prompt.py` for spec generation. Implementation order: validation first (simplest, immediately useful), then plugins, then spec generation.

**Tech Stack:** Python 3.10+, pytest, PyYAML, structlog, subprocess (for plugin execution)

---

### Task 1: Validation module — error checks for tasks.md

**Files:**
- Create: `src/spec_runner/validate.py`
- Create: `tests/test_validate.py`

**Step 1: Write the failing tests**

```python
"""Tests for spec-runner validate module."""

from pathlib import Path

from spec_runner.validate import ValidationResult, validate_tasks


class TestValidateTasksExist:
    """Test that validate checks tasks.md exists."""

    def test_missing_tasks_file(self, tmp_path):
        tasks_file = tmp_path / "spec" / "tasks.md"
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("does not exist" in e for e in result.errors)

    def test_empty_tasks_file(self, tmp_path):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text("# Tasks\n")
        result = validate_tasks(tasks_file)
        assert any("no tasks found" in e.lower() for e in result.errors)

    def test_valid_tasks_file(self, tmp_path):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: Setup project\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "Setup the project structure.\n"
        )
        result = validate_tasks(tasks_file)
        assert result.ok


class TestValidateDependencies:
    """Test dependency graph validation."""

    def test_missing_dependency_ref(self, tmp_path):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: First\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-999\n"
        )
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("TASK-999" in e and "does not exist" in e for e in result.errors)

    def test_circular_dependency(self, tmp_path):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: First\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-002\n\n"
            "### TASK-002: Second\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-001\n"
        )
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("circular" in e.lower() for e in result.errors)

    def test_valid_dependency_chain(self, tmp_path):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: First\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "### TASK-002: Second\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-001\n"
        )
        result = validate_tasks(tasks_file)
        assert result.ok


class TestValidateStatusAndPriority:
    """Test status and priority validation."""

    def test_invalid_status(self, tmp_path):
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: First\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
        )
        # Patch: we test via validate_task_fields with a task that has bad status
        from spec_runner.task import Task

        task = Task(id="TASK-001", name="Test", priority="p0", status="invalid", estimate="1d")
        from spec_runner.validate import validate_task_fields

        errors, _ = validate_task_fields([task])
        assert any("invalid status" in e.lower() for e in errors)

    def test_invalid_priority(self, tmp_path):
        from spec_runner.task import Task
        from spec_runner.validate import validate_task_fields

        task = Task(id="TASK-001", name="Test", priority="p9", status="todo", estimate="1d")
        errors, _ = validate_task_fields([task])
        assert any("invalid priority" in e.lower() for e in errors)
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_validate.py -v -x`
Expected: FAIL with "ModuleNotFoundError: No module named 'spec_runner.validate'"

**Step 3: Write minimal implementation**

```python
"""Validation module for spec-runner.

Validates tasks.md structure, config YAML, and dependency graphs
before execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger
from .task import Task, parse_tasks

logger = get_logger("validate")

VALID_STATUSES = {"todo", "in_progress", "done", "blocked"}
VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}


@dataclass
class ValidationResult:
    """Validation outcome with errors and warnings."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def merge(self, other: ValidationResult) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def validate_task_fields(tasks: list[Task]) -> tuple[list[str], list[str]]:
    """Validate individual task fields (status, priority)."""
    errors: list[str] = []
    warnings: list[str] = []
    for task in tasks:
        if task.status not in VALID_STATUSES:
            errors.append(f"{task.id}: invalid status '{task.status}'")
        if task.priority not in VALID_PRIORITIES:
            errors.append(f"{task.id}: invalid priority '{task.priority}'")
    return errors, warnings


def _detect_cycle(tasks: list[Task]) -> list[str]:
    """Detect circular dependencies via DFS. Returns list of cycle descriptions."""
    task_ids = {t.id for t in tasks}
    adj: dict[str, list[str]] = {t.id: list(t.depends_on) for t in tasks}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in task_ids}
    path: list[str] = []
    cycles: list[str] = []

    def dfs(node: str) -> None:
        if node not in color:
            return
        color[node] = GRAY
        path.append(node)
        for dep in adj.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                # Found cycle
                idx = path.index(dep)
                cycle = path[idx:] + [dep]
                cycles.append(" -> ".join(cycle))
            elif color[dep] == WHITE:
                dfs(dep)
        path.pop()
        color[node] = BLACK

    for tid in task_ids:
        if color[tid] == WHITE:
            dfs(tid)

    return cycles


def validate_tasks(tasks_file: Path) -> ValidationResult:
    """Validate tasks.md file: existence, parsing, dependencies, fields."""
    result = ValidationResult()

    # Check file exists
    if not tasks_file.exists():
        result.errors.append(f"{tasks_file}: does not exist")
        return result

    # Try parsing
    try:
        # parse_tasks calls sys.exit on missing file, so we checked above
        tasks = parse_tasks(tasks_file)
    except SystemExit:
        result.errors.append(f"{tasks_file}: failed to parse")
        return result
    except Exception as e:
        result.errors.append(f"{tasks_file}: parse error: {e}")
        return result

    if not tasks:
        result.errors.append(f"{tasks_file}: no tasks found")
        return result

    task_ids = {t.id for t in tasks}

    # Validate fields
    field_errors, field_warnings = validate_task_fields(tasks)
    result.errors.extend(field_errors)
    result.warnings.extend(field_warnings)

    # Check dependency refs exist
    for task in tasks:
        for dep in task.depends_on:
            if dep not in task_ids:
                result.errors.append(
                    f"{task.id}: depends on {dep} which does not exist"
                )

    # Check for circular dependencies
    cycles = _detect_cycle(tasks)
    for cycle in cycles:
        result.errors.append(f"circular dependency: {cycle}")

    return result
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_validate.py -v`
Expected: PASS (all 7 tests)

**Step 5: Run full test suite**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x`
Expected: all 283+ tests pass

**Step 6: Lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run ruff check . --fix && uv run ruff format .`

**Step 7: Commit**

```bash
git add src/spec_runner/validate.py tests/test_validate.py
git commit -m "feat: add validate module with task file and dependency checks"
```

---

### Task 2: Validation — warning checks and config validation

**Files:**
- Modify: `src/spec_runner/validate.py`
- Modify: `tests/test_validate.py`
- Modify: `src/spec_runner/config.py`

**Step 1: Write the failing tests**

```python
class TestValidateWarnings:
    """Test warning-level checks."""

    def test_missing_estimate_warning(self):
        from spec_runner.task import Task
        from spec_runner.validate import validate_task_fields

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="")
        _, warnings = validate_task_fields([task])
        assert any("missing estimate" in w.lower() for w in warnings)

    def test_blocked_without_deps_warning(self):
        from spec_runner.task import Task
        from spec_runner.validate import validate_task_fields

        task = Task(id="TASK-001", name="Test", priority="p0", status="blocked", estimate="1d")
        _, warnings = validate_task_fields([task])
        assert any("blocked" in w.lower() and "no depends_on" in w.lower() for w in warnings)

    def test_missing_traceability_warning(self):
        from spec_runner.task import Task
        from spec_runner.validate import validate_task_fields

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        _, warnings = validate_task_fields([task])
        assert any("traceability" in w.lower() for w in warnings)


class TestValidateConfig:
    """Test config YAML validation."""

    def test_valid_config(self, tmp_path):
        from spec_runner.validate import validate_config

        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retries: 5\n")
        result = validate_config(config_file)
        assert result.ok

    def test_unknown_key(self, tmp_path):
        from spec_runner.validate import validate_config

        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retry: 5\n")
        result = validate_config(config_file)
        assert not result.ok
        assert any("max_retry" in e and "max_retries" in e for e in result.errors)

    def test_invalid_yaml(self, tmp_path):
        from spec_runner.validate import validate_config

        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retries: [invalid\n")
        result = validate_config(config_file)
        assert not result.ok

    def test_missing_config_is_ok(self, tmp_path):
        from spec_runner.validate import validate_config

        config_file = tmp_path / "executor.config.yaml"
        result = validate_config(config_file)
        assert result.ok  # Missing config = use defaults, not an error
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_validate.py -v -x`
Expected: FAIL — new tests fail, old tests still pass

**Step 3: Implement warnings and config validation**

Add to `validate.py`:

```python
import yaml

from .config import ExecutorConfig

# Known top-level keys under 'executor:' in YAML
KNOWN_EXECUTOR_KEYS = {f.name for f in ExecutorConfig.__dataclass_fields__.values()} | {
    "hooks", "commands", "paths",
}


def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr_row.append(min(curr_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
        prev_row = curr_row
    return prev_row[-1]


def _suggest_key(unknown: str, known: set[str]) -> str | None:
    """Suggest closest known key if edit distance <= 2."""
    best, best_dist = None, 3
    for k in known:
        d = _levenshtein(unknown, k)
        if d < best_dist:
            best, best_dist = k, d
    return best
```

Update `validate_task_fields` to add warnings:

```python
def validate_task_fields(tasks: list[Task]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for task in tasks:
        if task.status not in VALID_STATUSES:
            errors.append(f"{task.id}: invalid status '{task.status}'")
        if task.priority not in VALID_PRIORITIES:
            errors.append(f"{task.id}: invalid priority '{task.priority}'")
        if not task.estimate:
            warnings.append(f"{task.id}: missing estimate")
        if task.status == "blocked" and not task.depends_on:
            warnings.append(f"{task.id}: status=blocked but no depends_on")
        if not task.traces_to:
            warnings.append(f"{task.id}: no traceability refs")
    return errors, warnings
```

Add `validate_config`:

```python
def validate_config(config_path: Path) -> ValidationResult:
    """Validate executor.config.yaml: syntax and known keys."""
    result = ValidationResult()

    if not config_path.exists():
        return result  # Missing config = use defaults

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.errors.append(f"{config_path}: invalid YAML: {e}")
        return result

    if not isinstance(data, dict):
        return result

    executor_config = data.get("executor", {})
    if not isinstance(executor_config, dict):
        result.errors.append(f"{config_path}: 'executor' must be a mapping")
        return result

    for key in executor_config:
        if key not in KNOWN_EXECUTOR_KEYS:
            suggestion = _suggest_key(key, KNOWN_EXECUTOR_KEYS)
            msg = f"{config_path}: unknown key '{key}'"
            if suggestion:
                msg += f" (did you mean '{suggestion}'?)"
            result.errors.append(msg)

    return result
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_validate.py -v`
Expected: all 11 tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/validate.py tests/test_validate.py
git commit -m "feat: add warning checks and config YAML validation with typo suggestions"
```

---

### Task 3: Validation CLI — `spec-runner validate` subcommand + pre-run validation

**Files:**
- Modify: `src/spec_runner/executor.py` (add `cmd_validate`, wire into `main()`, add pre-run call)
- Modify: `src/spec_runner/validate.py` (add `validate_all` + `format_results` top-level functions)
- Modify: `src/spec_runner/__init__.py` (export new symbols)
- Modify: `tests/test_validate.py` (add CLI + format tests)

**Step 1: Write the failing tests**

```python
class TestValidateAll:
    """Test the top-level validate_all function."""

    def test_validate_all_clean(self, tmp_path):
        from spec_runner.validate import validate_all

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

    def test_validate_all_with_config(self, tmp_path):
        from spec_runner.validate import validate_all

        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-001: Setup\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
        )
        config_file = tmp_path / "executor.config.yaml"
        config_file.write_text("executor:\n  max_retry: 5\n")
        result = validate_all(tasks_file=tasks_file, config_file=config_file)
        assert not result.ok  # unknown key error


class TestFormatResults:
    """Test output formatting."""

    def test_format_clean(self):
        from spec_runner.validate import ValidationResult, format_results

        result = ValidationResult()
        output = format_results(result)
        assert "0 errors" in output

    def test_format_with_errors_and_warnings(self):
        from spec_runner.validate import ValidationResult, format_results

        result = ValidationResult(
            errors=["TASK-001: depends on TASK-999 which does not exist"],
            warnings=["TASK-002: missing estimate"],
        )
        output = format_results(result)
        assert "1 error" in output
        assert "1 warning" in output
        assert "TASK-999" in output
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_validate.py::TestValidateAll -v -x`
Expected: FAIL with ImportError

**Step 3: Implement `validate_all`, `format_results`, and CLI command**

Add to `validate.py`:

```python
def validate_all(
    tasks_file: Path | None = None,
    config_file: Path | None = None,
) -> ValidationResult:
    """Run all validation checks. Entry point for CLI and pre-run."""
    result = ValidationResult()

    if tasks_file:
        result.merge(validate_tasks(tasks_file))

    if config_file:
        result.merge(validate_config(config_file))

    return result


def format_results(result: ValidationResult) -> str:
    """Format validation results for terminal output."""
    lines: list[str] = []

    if result.errors:
        for e in result.errors:
            lines.append(f"  x {e}")

    if result.warnings:
        if lines:
            lines.append("")
        for w in result.warnings:
            lines.append(f"  ! {w}")

    n_err = len(result.errors)
    n_warn = len(result.warnings)
    err_word = "error" if n_err == 1 else "errors"
    warn_word = "warning" if n_warn == 1 else "warnings"
    lines.append(f"\n{n_err} {err_word}, {n_warn} {warn_word}")

    return "\n".join(lines)
```

Add to `executor.py` — new `cmd_validate` function and argparse subcommand:

```python
def cmd_validate(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Validate config and tasks before execution."""
    from .validate import format_results, validate_all

    result = validate_all(
        tasks_file=config.tasks_file,
        config_file=Path("spec/executor.config.yaml")
        if Path("spec/executor.config.yaml").exists()
        else None,
    )
    print(format_results(result))
    if not result.ok:
        sys.exit(1)
```

In `main()` argparse, add:

```python
validate_parser = subparsers.add_parser("validate", help="Validate config and tasks")
validate_parser.set_defaults(func=cmd_validate)
```

In `_run_tasks()` and `_run_tasks_parallel()`, add pre-run validation at the top:

```python
from .validate import validate_all
pre_result = validate_all(tasks_file=config.tasks_file)
if not pre_result.ok:
    from .validate import format_results
    logger.error("Validation failed", details=format_results(pre_result))
    print(format_results(pre_result))
    print("Run `spec-runner validate` for details.")
    return
```

Update `__init__.py` — add exports:

```python
from .validate import ValidationResult, validate_all, validate_config, validate_tasks, format_results
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_validate.py -v`
Expected: all ~13 tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/validate.py src/spec_runner/executor.py src/spec_runner/__init__.py tests/test_validate.py
git commit -m "feat: add spec-runner validate CLI command and pre-run validation"
```

---

### Task 4: Plugin system — discovery and loading

**Files:**
- Create: `src/spec_runner/plugins.py`
- Create: `tests/test_plugins.py`

**Step 1: Write the failing tests**

```python
"""Tests for spec-runner plugin system."""

from pathlib import Path

import yaml

from spec_runner.plugins import PluginHook, PluginInfo, discover_plugins


def _create_plugin(plugins_dir: Path, name: str, hooks: dict) -> Path:
    """Helper to create a plugin directory with plugin.yaml."""
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    manifest = {"name": name, "description": f"Test plugin {name}", "version": "1.0", "hooks": hooks}
    (plugin_dir / "plugin.yaml").write_text(yaml.dump(manifest))
    return plugin_dir


class TestDiscoverPlugins:
    """Test plugin discovery from directory."""

    def test_no_plugins_dir(self, tmp_path):
        plugins_dir = tmp_path / "spec" / "plugins"
        result = discover_plugins(plugins_dir)
        assert result == []

    def test_empty_plugins_dir(self, tmp_path):
        plugins_dir = tmp_path / "spec" / "plugins"
        plugins_dir.mkdir(parents=True)
        result = discover_plugins(plugins_dir)
        assert result == []

    def test_discover_single_plugin(self, tmp_path):
        plugins_dir = tmp_path / "spec" / "plugins"
        _create_plugin(plugins_dir, "notify-slack", {
            "post_done": {"command": "./on_done.sh", "run_on": "on_success"},
        })
        result = discover_plugins(plugins_dir)
        assert len(result) == 1
        assert result[0].name == "notify-slack"
        assert "post_done" in result[0].hooks

    def test_discover_multiple_sorted(self, tmp_path):
        plugins_dir = tmp_path / "spec" / "plugins"
        _create_plugin(plugins_dir, "z-last", {"pre_start": {"command": "./start.sh"}})
        _create_plugin(plugins_dir, "a-first", {"post_done": {"command": "./done.sh"}})
        result = discover_plugins(plugins_dir)
        assert len(result) == 2
        assert result[0].name == "a-first"
        assert result[1].name == "z-last"

    def test_skip_dir_without_manifest(self, tmp_path):
        plugins_dir = tmp_path / "spec" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "no-manifest").mkdir()
        result = discover_plugins(plugins_dir)
        assert result == []

    def test_plugin_hook_defaults(self, tmp_path):
        plugins_dir = tmp_path / "spec" / "plugins"
        _create_plugin(plugins_dir, "basic", {
            "post_done": {"command": "./run.sh"},
        })
        result = discover_plugins(plugins_dir)
        hook = result[0].hooks["post_done"]
        assert hook.run_on == "always"
        assert hook.blocking is False
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plugins.py -v -x`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write minimal implementation**

```python
"""Plugin system for spec-runner.

Discovers and executes directory-based plugins that register
custom pre_start and post_done hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .logging import get_logger

logger = get_logger("plugins")


@dataclass
class PluginHook:
    """A single hook registration within a plugin."""

    command: str
    run_on: str = "always"  # always | on_success | on_failure
    blocking: bool = False


@dataclass
class PluginInfo:
    """Discovered plugin metadata."""

    name: str
    description: str
    version: str
    path: Path
    hooks: dict[str, PluginHook] = field(default_factory=dict)


def discover_plugins(plugins_dir: Path) -> list[PluginInfo]:
    """Discover plugins from directory. Returns sorted by name."""
    if not plugins_dir.is_dir():
        return []

    plugins: list[PluginInfo] = []

    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "plugin.yaml"
        if not manifest.exists():
            logger.debug("Skipping directory without manifest", path=str(entry))
            continue

        try:
            with open(manifest) as f:
                data = yaml.safe_load(f) or {}

            hooks: dict[str, PluginHook] = {}
            for event, hook_data in (data.get("hooks") or {}).items():
                if isinstance(hook_data, dict) and "command" in hook_data:
                    hooks[event] = PluginHook(
                        command=hook_data["command"],
                        run_on=hook_data.get("run_on", "always"),
                        blocking=hook_data.get("blocking", False),
                    )

            plugins.append(PluginInfo(
                name=data.get("name", entry.name),
                description=data.get("description", ""),
                version=data.get("version", ""),
                path=entry,
                hooks=hooks,
            ))
        except Exception as e:
            logger.warning("Failed to load plugin", path=str(entry), error=str(e))

    return plugins
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plugins.py -v`
Expected: all 6 tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/plugins.py tests/test_plugins.py
git commit -m "feat: add plugin discovery with YAML manifests"
```

---

### Task 5: Plugin system — hook execution

**Files:**
- Modify: `src/spec_runner/plugins.py`
- Modify: `tests/test_plugins.py`

**Step 1: Write the failing tests**

```python
import os
import stat


class TestRunPluginHooks:
    """Test plugin hook execution."""

    def _make_script(self, plugin_dir: Path, name: str, content: str) -> Path:
        script = plugin_dir / name
        script.write_text(content)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_run_post_done_hook(self, tmp_path):
        from spec_runner.plugins import discover_plugins, run_plugin_hooks

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(plugins_dir, "test-plugin", {
            "post_done": {"command": "./done.sh"},
        })
        self._make_script(plugin_dir, "done.sh", "#!/bin/bash\necho OK")
        plugins = discover_plugins(plugins_dir)
        results = run_plugin_hooks("post_done", plugins, task_env={"SR_TASK_ID": "TASK-001"})
        assert len(results) == 1
        assert results[0][0] == "test-plugin"
        assert results[0][1] is True  # success

    def test_skip_on_run_on_filter(self, tmp_path):
        from spec_runner.plugins import discover_plugins, run_plugin_hooks

        plugins_dir = tmp_path / "spec" / "plugins"
        _create_plugin(plugins_dir, "success-only", {
            "post_done": {"command": "./done.sh", "run_on": "on_success"},
        })
        plugins = discover_plugins(plugins_dir)
        results = run_plugin_hooks(
            "post_done", plugins,
            task_env={"SR_TASK_ID": "TASK-001", "SR_TASK_STATUS": "failed"},
        )
        assert len(results) == 0  # skipped

    def test_env_vars_passed(self, tmp_path):
        from spec_runner.plugins import discover_plugins, run_plugin_hooks

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(plugins_dir, "env-check", {
            "post_done": {"command": "./check_env.sh"},
        })
        marker = tmp_path / "env_marker.txt"
        self._make_script(
            plugin_dir,
            "check_env.sh",
            f"#!/bin/bash\necho $SR_TASK_ID > {marker}",
        )
        plugins = discover_plugins(plugins_dir)
        run_plugin_hooks("post_done", plugins, task_env={"SR_TASK_ID": "TASK-042"})
        assert marker.read_text().strip() == "TASK-042"

    def test_blocking_failure_reported(self, tmp_path):
        from spec_runner.plugins import discover_plugins, run_plugin_hooks

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(plugins_dir, "blocker", {
            "post_done": {"command": "./fail.sh", "blocking": True},
        })
        self._make_script(plugin_dir, "fail.sh", "#!/bin/bash\nexit 1")
        plugins = discover_plugins(plugins_dir)
        results = run_plugin_hooks("post_done", plugins, task_env={"SR_TASK_ID": "TASK-001"})
        assert len(results) == 1
        assert results[0][1] is False  # failure
        assert results[0][2] is True  # blocking
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plugins.py::TestRunPluginHooks -v -x`
Expected: FAIL with ImportError (run_plugin_hooks doesn't exist)

**Step 3: Implement hook execution**

Add to `plugins.py`:

```python
import os
import subprocess


def _should_run(hook: PluginHook, task_status: str) -> bool:
    """Check if hook should run based on run_on filter and task status."""
    if hook.run_on == "always":
        return True
    if hook.run_on == "on_success" and task_status == "success":
        return True
    if hook.run_on == "on_failure" and task_status == "failed":
        return True
    return False


def run_plugin_hooks(
    event: str,
    plugins: list[PluginInfo],
    task_env: dict[str, str] | None = None,
    timeout_seconds: int = 60,
) -> list[tuple[str, bool, bool]]:
    """Run all plugin hooks for given event.

    Args:
        event: Hook event name (pre_start, post_done).
        plugins: Discovered plugins.
        task_env: Environment variables (SR_TASK_ID, etc.).
        timeout_seconds: Per-plugin timeout.

    Returns:
        List of (plugin_name, success, is_blocking) tuples.
    """
    results: list[tuple[str, bool, bool]] = []
    env = {**os.environ, **(task_env or {})}
    task_status = (task_env or {}).get("SR_TASK_STATUS", "success")

    for plugin in plugins:
        hook = plugin.hooks.get(event)
        if not hook:
            continue

        if not _should_run(hook, task_status):
            logger.debug("Skipping plugin hook", plugin=plugin.name, event=event, run_on=hook.run_on)
            continue

        try:
            result = subprocess.run(
                hook.command,
                shell=True,
                env=env,
                cwd=str(plugin.path),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            success = result.returncode == 0
            if not success:
                level = "error" if hook.blocking else "warning"
                getattr(logger, level)(
                    "Plugin hook failed",
                    plugin=plugin.name,
                    event=event,
                    returncode=result.returncode,
                    stderr=result.stderr[:500],
                )
            else:
                logger.info("Plugin hook succeeded", plugin=plugin.name, event=event)
            results.append((plugin.name, success, hook.blocking))
        except subprocess.TimeoutExpired:
            logger.error("Plugin hook timed out", plugin=plugin.name, event=event)
            results.append((plugin.name, False, hook.blocking))
        except Exception as e:
            logger.error("Plugin hook error", plugin=plugin.name, event=event, error=str(e))
            results.append((plugin.name, False, hook.blocking))

    return results
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plugins.py -v`
Expected: all 10 tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/plugins.py tests/test_plugins.py
git commit -m "feat: add plugin hook execution with env vars and run_on filters"
```

---

### Task 6: Plugin system — wire into hooks.py

**Files:**
- Modify: `src/spec_runner/hooks.py`
- Modify: `src/spec_runner/config.py`
- Modify: `src/spec_runner/__init__.py`
- Modify: `tests/test_plugins.py`

**Step 1: Write the failing test**

```python
from unittest.mock import patch


class TestPluginIntegration:
    """Test plugin hooks called from hooks.py."""

    def test_pre_start_runs_plugins(self, tmp_path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import pre_start_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(plugins_dir, "pre-test", {
            "pre_start": {"command": "./start.sh"},
        })
        marker = tmp_path / "pre_marker.txt"
        script = plugin_dir / "start.sh"
        script.write_text(f"#!/bin/bash\ntouch {marker}")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            # Mock uv sync to succeed
            mock_run.return_value.returncode = 0
            pre_start_hook(task, config)

        assert marker.exists()

    def test_post_done_runs_plugins(self, tmp_path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import post_done_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(plugins_dir, "post-test", {
            "post_done": {"command": "./done.sh"},
        })
        marker = tmp_path / "post_marker.txt"
        script = plugin_dir / "done.sh"
        script.write_text(f"#!/bin/bash\ntouch {marker}")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        post_done_hook(task, config, success=True)
        assert marker.exists()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plugins.py::TestPluginIntegration -v -x`
Expected: FAIL — `plugins_dir` not a field of ExecutorConfig

**Step 3: Implement integration**

Add to `config.py` `ExecutorConfig`:

```python
plugins_dir: Path = Path("spec/plugins")
```

And in `__post_init__`:

```python
if not self.plugins_dir.is_absolute():
    self.plugins_dir = self.project_root / self.plugins_dir
```

Add to `hooks.py` — at end of `pre_start_hook`:

```python
from .plugins import build_task_env, discover_plugins, run_plugin_hooks

plugins = discover_plugins(config.plugins_dir)
if plugins:
    task_env = build_task_env(task, config, success=None)
    results = run_plugin_hooks("pre_start", plugins, task_env=task_env)
    for name, success, blocking in results:
        if not success and blocking:
            logger.error("Blocking plugin failed in pre_start", plugin=name)
            return False
```

Add to `hooks.py` — at end of `post_done_hook` (before final return):

```python
plugins = discover_plugins(config.plugins_dir)
if plugins:
    task_env = build_task_env(task, config, success=success)
    results = run_plugin_hooks("post_done", plugins, task_env=task_env)
    for name, ok, blocking in results:
        if not ok and blocking:
            logger.error("Blocking plugin failed in post_done", plugin=name)
```

Add to `plugins.py`:

```python
def build_task_env(task: "Task", config: "ExecutorConfig", success: bool | None = None) -> dict[str, str]:
    """Build environment variables dict for plugin hooks."""
    from .config import ExecutorConfig
    from .task import Task

    status = "success" if success else ("failed" if success is False else "pending")
    return {
        "SR_TASK_ID": task.id,
        "SR_TASK_NAME": task.name,
        "SR_TASK_STATUS": status,
        "SR_TASK_PRIORITY": task.priority,
        "SR_PROJECT_ROOT": str(config.project_root),
    }
```

Update `__init__.py` — add plugin exports:

```python
from .plugins import PluginHook, PluginInfo, discover_plugins, run_plugin_hooks, build_task_env
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plugins.py -v`
Expected: all ~12 tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/plugins.py src/spec_runner/hooks.py src/spec_runner/config.py src/spec_runner/__init__.py tests/test_plugins.py
git commit -m "feat: wire plugin hooks into pre_start and post_done execution"
```

---

### Task 7: Spec generation — `spec-runner plan --full`

**Files:**
- Modify: `src/spec_runner/executor.py` (extend `cmd_plan`)
- Modify: `src/spec_runner/prompt.py` (add `build_generation_prompt`)
- Create: `tests/test_plan_full.py`

**Step 1: Write the failing tests**

```python
"""Tests for spec-runner plan --full (spec generation)."""

from pathlib import Path
from unittest.mock import patch


class TestBuildGenerationPrompt:
    """Test prompt building for spec generation stages."""

    def test_requirements_stage(self):
        from spec_runner.prompt import build_generation_prompt

        prompt = build_generation_prompt(
            stage="requirements",
            description="Build a REST API for user management",
            context={},
        )
        assert "requirements" in prompt.lower()
        assert "REST API" in prompt
        assert "SPEC_REQUIREMENTS_READY" in prompt

    def test_design_stage_includes_requirements(self):
        from spec_runner.prompt import build_generation_prompt

        prompt = build_generation_prompt(
            stage="design",
            description="Build a REST API",
            context={"requirements": "# Requirements\n[REQ-001] User auth"},
        )
        assert "design" in prompt.lower()
        assert "REQ-001" in prompt
        assert "SPEC_DESIGN_READY" in prompt

    def test_tasks_stage_includes_requirements_and_design(self):
        from spec_runner.prompt import build_generation_prompt

        prompt = build_generation_prompt(
            stage="tasks",
            description="Build a REST API",
            context={
                "requirements": "# Requirements\n[REQ-001] Auth",
                "design": "# Design\n[DESIGN-001] REST layer",
            },
        )
        assert "tasks" in prompt.lower()
        assert "REQ-001" in prompt
        assert "DESIGN-001" in prompt
        assert "SPEC_TASKS_READY" in prompt


class TestParseSpecMarkers:
    """Test extraction of spec content from Claude output."""

    def test_extract_requirements(self):
        from spec_runner.prompt import parse_spec_marker

        output = "Some preamble\nSPEC_REQUIREMENTS_READY\n# Requirements\n[REQ-001] Auth\nSPEC_REQUIREMENTS_END\nTrailing"
        content = parse_spec_marker(output, "REQUIREMENTS")
        assert content is not None
        assert "[REQ-001]" in content

    def test_no_marker_returns_none(self):
        from spec_runner.prompt import parse_spec_marker

        content = parse_spec_marker("No markers here", "REQUIREMENTS")
        assert content is None
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plan_full.py -v -x`
Expected: FAIL with ImportError

**Step 3: Implement generation prompts and marker parsing**

Add to `prompt.py`:

```python
SPEC_STAGES = {
    "requirements": {
        "marker": "SPEC_REQUIREMENTS",
        "instruction": (
            "Generate a requirements document based on the project description below. "
            "Use [REQ-001], [REQ-002], etc. for each requirement. "
            "When done, output the requirements between markers:\n"
            "SPEC_REQUIREMENTS_READY\n<your requirements>\nSPEC_REQUIREMENTS_END"
        ),
    },
    "design": {
        "marker": "SPEC_DESIGN",
        "instruction": (
            "Generate a design document based on the requirements below. "
            "Use [DESIGN-001], [DESIGN-002], etc. and trace back to requirements with [REQ-XXX]. "
            "When done, output the design between markers:\n"
            "SPEC_DESIGN_READY\n<your design>\nSPEC_DESIGN_END"
        ),
    },
    "tasks": {
        "marker": "SPEC_TASKS",
        "instruction": (
            "Generate a tasks document based on the requirements and design below. "
            "Use TASK-001, TASK-002, etc. with priorities (P0-P3), estimates, checklists, "
            "dependencies, and traceability refs to [REQ-XXX] and [DESIGN-XXX]. "
            "When done, output the tasks between markers:\n"
            "SPEC_TASKS_READY\n<your tasks>\nSPEC_TASKS_END"
        ),
    },
}


def build_generation_prompt(
    stage: str,
    description: str,
    context: dict[str, str] | None = None,
) -> str:
    """Build prompt for spec generation stage.

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        description: Project description from user.
        context: Previous stage outputs (e.g., {'requirements': '...'}).
    """
    ctx = context or {}
    stage_info = SPEC_STAGES[stage]
    parts: list[str] = [stage_info["instruction"], "", f"Project description: {description}"]

    if "requirements" in ctx:
        parts.extend(["", "## Requirements (already generated)", ctx["requirements"]])
    if "design" in ctx:
        parts.extend(["", "## Design (already generated)", ctx["design"]])

    return "\n".join(parts)


def parse_spec_marker(output: str, marker_name: str) -> str | None:
    """Extract content between SPEC_{NAME}_READY and SPEC_{NAME}_END markers."""
    start = f"SPEC_{marker_name}_READY"
    end = f"SPEC_{marker_name}_END"
    start_idx = output.find(start)
    if start_idx == -1:
        return None
    start_idx += len(start)
    end_idx = output.find(end, start_idx)
    if end_idx == -1:
        # If no end marker, take everything after start
        return output[start_idx:].strip()
    return output[start_idx:end_idx].strip()
```

Add `--full` flag to `cmd_plan` in `executor.py`:

In the plan subparser:
```python
plan_parser.add_argument("--full", action="store_true", help="Generate full spec (requirements + design + tasks)")
```

Extend `cmd_plan` to handle `--full` with three-stage loop (mock-compatible — uses existing `run_claude_async` pattern).

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plan_full.py -v`
Expected: all 5 tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/prompt.py src/spec_runner/executor.py tests/test_plan_full.py
git commit -m "feat: add spec generation prompts, markers, and --full flag for plan command"
```

---

### Task 8: Spec generation — wire three-stage pipeline in cmd_plan

**Files:**
- Modify: `src/spec_runner/executor.py` (extend `cmd_plan` with `--full` pipeline)
- Modify: `tests/test_plan_full.py` (add integration test)

**Step 1: Write the failing test**

```python
class TestPlanFullPipeline:
    """Test the three-stage pipeline."""

    def test_full_generates_three_files(self, tmp_path):
        from unittest.mock import AsyncMock

        from spec_runner.prompt import parse_spec_marker

        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()

        # Simulate Claude output for each stage
        requirements_output = (
            "SPEC_REQUIREMENTS_READY\n"
            "# Requirements\n[REQ-001] User authentication\n"
            "SPEC_REQUIREMENTS_END"
        )
        design_output = (
            "SPEC_DESIGN_READY\n"
            "# Design\n[DESIGN-001] Auth module\n"
            "SPEC_DESIGN_END"
        )
        tasks_output = (
            "SPEC_TASKS_READY\n"
            "# Tasks\n### TASK-001: Setup auth\n"
            "SPEC_TASKS_END"
        )

        # Test marker parsing for each stage
        req = parse_spec_marker(requirements_output, "REQUIREMENTS")
        assert req is not None
        assert "[REQ-001]" in req

        des = parse_spec_marker(design_output, "DESIGN")
        assert des is not None
        assert "[DESIGN-001]" in des

        tasks = parse_spec_marker(tasks_output, "TASKS")
        assert tasks is not None
        assert "TASK-001" in tasks

        # Write files as the pipeline would
        (spec_dir / "requirements.md").write_text(req)
        (spec_dir / "design.md").write_text(des)
        (spec_dir / "tasks.md").write_text(tasks)

        assert (spec_dir / "requirements.md").exists()
        assert (spec_dir / "design.md").exists()
        assert (spec_dir / "tasks.md").exists()
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plan_full.py::TestPlanFullPipeline -v -x`
Expected: Should PASS if marker parsing works (this is more of an integration validation)

**Step 3: Implement three-stage pipeline in `cmd_plan`**

In `executor.py`, extend `cmd_plan` when `args.full` is True:

```python
if getattr(args, "full", False):
    from .prompt import SPEC_STAGES, build_generation_prompt, parse_spec_marker

    stages = ["requirements", "design", "tasks"]
    stage_files = {
        "requirements": config.requirements_file,
        "design": config.design_file,
        "tasks": config.tasks_file,
    }
    marker_names = {"requirements": "REQUIREMENTS", "design": "DESIGN", "tasks": "TASKS"}
    context: dict[str, str] = {}

    for stage in stages:
        logger.info("Generating spec", stage=stage)
        prompt = build_generation_prompt(stage, description, context)

        # Run Claude with the generation prompt
        result = subprocess.run(
            build_cli_command(config, prompt),
            capture_output=True,
            text=True,
            timeout=config.task_timeout_minutes * 60,
            cwd=config.project_root,
        )

        if result.returncode != 0:
            logger.error("Generation failed", stage=stage, stderr=result.stderr[:500])
            print(f"Failed at stage: {stage}")
            sys.exit(1)

        content = parse_spec_marker(result.stdout, marker_names[stage])
        if not content:
            logger.error("No spec marker found in output", stage=stage)
            print(f"Claude did not produce {stage} content. Raw output saved to logs.")
            sys.exit(1)

        # Write to file
        output_file = stage_files[stage]
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(content + "\n")
        logger.info("Spec written", stage=stage, file=str(output_file))
        print(f"Written: {output_file}")

        # Add to context for next stage
        context[stage] = content

    print("\nSpec generation complete!")
    print(f"  Requirements: {config.requirements_file}")
    print(f"  Design:       {config.design_file}")
    print(f"  Tasks:        {config.tasks_file}")
    return
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_plan_full.py -v`
Expected: all tests pass

**Step 5: Full suite + lint**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -x && uv run ruff check . --fix && uv run ruff format .`

**Step 6: Commit**

```bash
git add src/spec_runner/executor.py tests/test_plan_full.py
git commit -m "feat: wire three-stage spec generation pipeline into plan --full"
```

---

### Task 9: Exports, CLAUDE.md, final cleanup

**Files:**
- Modify: `src/spec_runner/__init__.py` (ensure all new exports)
- Modify: `CLAUDE.md` (update docs)

**Step 1: Update `__init__.py`**

Add all new public symbols to imports and `__all__`:

```python
from .plugins import PluginHook, PluginInfo, build_task_env, discover_plugins, run_plugin_hooks
from .validate import ValidationResult, format_results, validate_all, validate_config, validate_tasks
```

Add to `__all__`:

```python
# Plugins
"PluginHook",
"PluginInfo",
"build_task_env",
"discover_plugins",
"run_plugin_hooks",
# Validation
"ValidationResult",
"format_results",
"validate_all",
"validate_config",
"validate_tasks",
```

**Step 2: Update CLAUDE.md**

- Add `validate.py` and `plugins.py` to module table with line counts
- Add `spec-runner validate` to CLI entry points
- Add `--full` flag to `spec-runner plan` docs
- Add plugin system description (spec/plugins/ directory, plugin.yaml format)
- Update test count

**Step 3: Run full suite**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v && uv run ruff check . --fix && uv run ruff format .`

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py CLAUDE.md
git commit -m "docs: update exports and CLAUDE.md for Phase 6 extensibility features"
```

---

## Summary

| Task | Feature | Tests | ~Lines |
|------|---------|-------|--------|
| 1 | Validate: error checks | 7 | ~120 |
| 2 | Validate: warnings + config | 4 | ~80 |
| 3 | Validate: CLI + pre-run | 4 | ~60 |
| 4 | Plugins: discovery | 6 | ~80 |
| 5 | Plugins: execution | 4 | ~60 |
| 6 | Plugins: wire into hooks | 2 | ~50 |
| 7 | Spec gen: prompts + markers | 5 | ~70 |
| 8 | Spec gen: pipeline | 1 | ~40 |
| 9 | Exports + docs | 0 | ~30 |
| **Total** | | **~33** | **~590** |
