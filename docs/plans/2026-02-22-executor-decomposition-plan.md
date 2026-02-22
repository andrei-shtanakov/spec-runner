# executor.py Decomposition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split executor.py (2313 lines) into 6 focused modules and add ~50 tests, with zero behavior changes.

**Architecture:** Extract code from executor.py into leaf-to-root modules (config → state → runner → prompt → hooks), then update executor.py imports. Each extraction is a standalone commit. Tests are written after each module extraction.

**Tech Stack:** Python 3.11+, pytest, mock/tmp_path fixtures, ruff for formatting.

**Design doc:** `docs/plans/2026-02-22-executor-decomposition-design.md`

---

### Task 1: Extract config.py

**Files:**
- Create: `src/spec_runner/config.py`
- Modify: `src/spec_runner/executor.py:1-247` (remove extracted code, add imports)
- Modify: `src/spec_runner/__init__.py` (update import paths)

**Step 1: Create config.py with code from executor.py**

Extract these sections from `executor.py`:
- Lines 113–141: `ExecutorLock` class
- Lines 144–146: `CONFIG_FILE`, `PROGRESS_FILE`, `ERROR_PATTERNS` constants
- Lines 149–156: `ERROR_PATTERNS` list
- Lines 159–247: `ExecutorConfig` dataclass
- Lines 331–429: `load_config_from_yaml()` and `build_config()`

The new file needs these imports:
```python
import contextlib
import fcntl
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
```

**Step 2: Update executor.py imports**

Replace the removed code in executor.py with:
```python
from .config import (
    CONFIG_FILE,
    ERROR_PATTERNS,
    PROGRESS_FILE,
    ExecutorConfig,
    ExecutorLock,
    build_config,
    load_config_from_yaml,
)
```

Remove now-unused imports from executor.py: `fcntl`, `yaml`, and the `import yaml` line. Keep `contextlib` (still used in state section).

**Step 3: Update `__init__.py`**

Change lines 20-30 — import `ExecutorConfig`, `build_config`, `load_config_from_yaml` from `.config` instead of `.executor`:
```python
from .config import ExecutorConfig, build_config, load_config_from_yaml
from .executor import (
    ExecutorState,
    TaskAttempt,
    TaskState,
    build_task_prompt,
    execute_task,
    run_with_retries,
)
```

**Step 4: Run existing tests**

Run: `uv run pytest tests/test_spec_prefix.py -v`
Expected: all 41 tests PASS (test_spec_prefix.py imports `ExecutorConfig` from `spec_runner.executor` — the re-export via `__init__.py` keeps this working; direct import `from spec_runner.executor import ExecutorConfig` must also work via executor.py's own re-import)

**Important:** `test_spec_prefix.py` line 6 does `from spec_runner.executor import ExecutorConfig`. After extraction, executor.py must re-export: `from .config import ExecutorConfig`. This keeps existing imports working.

**Step 5: Verify CLI works**

Run: `spec-runner --help`
Expected: normal help output with all subcommands.

**Step 6: Lint and commit**

```bash
uv run ruff check src/spec_runner/config.py src/spec_runner/executor.py src/spec_runner/__init__.py --fix
uv run ruff format src/spec_runner/config.py src/spec_runner/executor.py src/spec_runner/__init__.py
git add src/spec_runner/config.py src/spec_runner/executor.py src/spec_runner/__init__.py
git commit -m "refactor: extract config.py from executor.py"
```

---

### Task 2: Write tests for config.py

**Files:**
- Create: `tests/test_config.py`

**Step 1: Write tests**

```python
"""Tests for spec_runner.config module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runner.config import (
    ERROR_PATTERNS,
    ExecutorConfig,
    ExecutorLock,
    build_config,
    load_config_from_yaml,
)


class TestExecutorConfig:
    """ExecutorConfig dataclass behavior."""

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
        assert str(c.state_file).endswith("spec/.executor-state.json")

    def test_logs_dir_resolved_to_absolute(self):
        c = ExecutorConfig()
        assert c.logs_dir.is_absolute()
        assert str(c.logs_dir).endswith("spec/.executor-logs")

    def test_spec_prefix_namespaces_state_file(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert "phase2-" in str(c.state_file)

    def test_spec_prefix_namespaces_logs_dir(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert "phase2-" in str(c.logs_dir)

    def test_spec_prefix_namespaces_tasks_file(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert c.tasks_file.name == "phase2-tasks.md"

    def test_spec_prefix_namespaces_requirements_file(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert c.requirements_file.name == "phase2-requirements.md"

    def test_spec_prefix_namespaces_design_file(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert c.design_file.name == "phase2-design.md"

    def test_stop_file_property(self):
        c = ExecutorConfig()
        assert c.stop_file == c.project_root / "spec" / ".executor-stop"


class TestLoadConfigFromYaml:
    """YAML config loading."""

    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        result = load_config_from_yaml(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_loads_yaml_values(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("max_retries: 5\nclaude_model: opus\n")
        result = load_config_from_yaml(cfg)
        assert result["max_retries"] == 5
        assert result["claude_model"] == "opus"

    def test_returns_empty_dict_for_invalid_yaml(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(": invalid: yaml: [")
        result = load_config_from_yaml(cfg)
        assert result == {}


class TestBuildConfig:
    """Three-level config precedence: defaults < YAML < CLI args."""

    def test_cli_overrides_yaml(self):
        from argparse import Namespace

        yaml_config = {"max_retries": 5}
        args = Namespace(max_retries=10, timeout=30, no_tests=False,
                         no_branch=False, no_commit=False, no_review=False,
                         callback_url="", spec_prefix="", project_root=None)
        config = build_config(yaml_config, args)
        assert config.max_retries == 10

    def test_yaml_overrides_defaults(self):
        from argparse import Namespace

        yaml_config = {"retry_delay_seconds": 30}
        args = Namespace(max_retries=3, timeout=30, no_tests=False,
                         no_branch=False, no_commit=False, no_review=False,
                         callback_url="", spec_prefix="", project_root=None)
        config = build_config(yaml_config, args)
        assert config.retry_delay_seconds == 30


class TestErrorPatterns:
    """ERROR_PATTERNS constant."""

    def test_contains_rate_limit(self):
        assert any("rate limit" in p.lower() for p in ERROR_PATTERNS)

    def test_contains_context_window(self):
        assert any("context window" in p.lower() for p in ERROR_PATTERNS)
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: all PASS

**Step 3: Run full test suite to check no regressions**

Run: `uv run pytest tests/ -v`
Expected: all tests PASS (test_spec_prefix.py + test_config.py)

**Step 4: Commit**

```bash
git add tests/test_config.py
git commit -m "test: add tests for config module"
```

---

### Task 3: Extract state.py

**Files:**
- Create: `src/spec_runner/state.py`
- Modify: `src/spec_runner/executor.py:432-581` (remove extracted code, add imports)
- Modify: `src/spec_runner/__init__.py` (update import paths)

**Step 1: Create state.py with code from executor.py**

Extract these sections from `executor.py`:
- Lines 435–443: `TaskAttempt` dataclass
- Lines 446–464: `TaskState` dataclass
- Lines 467–569: `ExecutorState` class
- Lines 572–580: `check_stop_requested()`, `clear_stop_file()`

The new file needs these imports:
```python
import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from .config import ExecutorConfig
```

**Step 2: Update executor.py imports**

Replace the removed code with:
```python
from .state import (
    ExecutorState,
    TaskAttempt,
    TaskState,
    check_stop_requested,
    clear_stop_file,
)
```

Remove now-unused imports from executor.py: `contextlib` (if no longer used).

**Step 3: Update `__init__.py`**

Import `ExecutorState`, `TaskAttempt`, `TaskState` from `.state` instead of `.executor`.

**Step 4: Run tests and verify CLI**

Run: `uv run pytest tests/ -v && spec-runner --help`
Expected: all tests PASS, help works.

**Step 5: Lint and commit**

```bash
uv run ruff check src/spec_runner/state.py src/spec_runner/executor.py src/spec_runner/__init__.py --fix
uv run ruff format src/spec_runner/state.py src/spec_runner/executor.py src/spec_runner/__init__.py
git add src/spec_runner/state.py src/spec_runner/executor.py src/spec_runner/__init__.py
git commit -m "refactor: extract state.py from executor.py"
```

---

### Task 4: Write tests for state.py

**Files:**
- Create: `tests/test_state.py`

**Step 1: Write tests**

```python
"""Tests for spec_runner.state module."""

import json

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import (
    ExecutorState,
    TaskAttempt,
    TaskState,
    check_stop_requested,
    clear_stop_file,
)


class TestTaskAttempt:
    def test_creation(self):
        a = TaskAttempt(timestamp="2026-01-01T00:00:00", success=True,
                        duration_seconds=10.5)
        assert a.success is True
        assert a.duration_seconds == 10.5
        assert a.error is None

    def test_with_error(self):
        a = TaskAttempt(timestamp="t", success=False, duration_seconds=5.0,
                        error="test failed")
        assert a.error == "test failed"


class TestTaskState:
    def test_attempt_count_empty(self):
        ts = TaskState(task_id="TASK-001", status="pending")
        assert ts.attempt_count == 0

    def test_attempt_count_with_attempts(self):
        ts = TaskState(task_id="TASK-001", status="running", attempts=[
            TaskAttempt(timestamp="t", success=False, duration_seconds=1.0),
            TaskAttempt(timestamp="t", success=True, duration_seconds=2.0),
        ])
        assert ts.attempt_count == 2

    def test_last_error_none_when_empty(self):
        ts = TaskState(task_id="TASK-001", status="pending")
        assert ts.last_error is None

    def test_last_error_returns_latest(self):
        ts = TaskState(task_id="TASK-001", status="failed", attempts=[
            TaskAttempt(timestamp="t", success=False, duration_seconds=1.0,
                        error="first"),
            TaskAttempt(timestamp="t", success=False, duration_seconds=1.0,
                        error="second"),
        ])
        assert ts.last_error == "second"


class TestExecutorState:
    def test_creates_empty_state(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
        )
        state = ExecutorState(config)
        assert state.tasks == {}
        assert state.consecutive_failures == 0

    def test_save_and_load(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
        )
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=True, duration=10.0)
        # Create new instance to verify load
        state2 = ExecutorState(config)
        assert "TASK-001" in state2.tasks
        assert state2.tasks["TASK-001"].status == "success"
        assert state2.total_completed == 1

    def test_record_failure_increments_consecutive(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
            max_retries=5,
        )
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=False, duration=5.0,
                             error="fail")
        assert state.consecutive_failures == 1

    def test_record_success_resets_consecutive(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
        )
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=False, duration=5.0,
                             error="fail")
        state.record_attempt("TASK-002", success=True, duration=5.0)
        assert state.consecutive_failures == 0

    def test_should_stop(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
            max_consecutive_failures=2,
            max_retries=5,
        )
        state = ExecutorState(config)
        state.record_attempt("T1", success=False, duration=1.0, error="e")
        assert state.should_stop() is False
        state.record_attempt("T2", success=False, duration=1.0, error="e")
        assert state.should_stop() is True

    def test_mark_running(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
        )
        state = ExecutorState(config)
        state.mark_running("TASK-001")
        assert state.tasks["TASK-001"].status == "running"
        assert state.tasks["TASK-001"].started_at is not None

    def test_load_corrupt_json_handled(self, tmp_path):
        config = ExecutorConfig(
            state_file=tmp_path / "state.json",
            project_root=tmp_path,
        )
        (tmp_path / "state.json").write_text("{invalid json")
        # Should not crash — either handle gracefully or raise clear error
        with pytest.raises((json.JSONDecodeError, Exception)):
            ExecutorState(config)


class TestStopFile:
    def test_check_stop_not_requested(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path)
        (tmp_path / "spec").mkdir()
        assert check_stop_requested(config) is False

    def test_check_stop_requested(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path)
        (tmp_path / "spec").mkdir()
        config.stop_file.touch()
        assert check_stop_requested(config) is True

    def test_clear_stop_file(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path)
        (tmp_path / "spec").mkdir()
        config.stop_file.touch()
        clear_stop_file(config)
        assert not config.stop_file.exists()

    def test_clear_stop_file_noop_if_missing(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path)
        (tmp_path / "spec").mkdir()
        clear_stop_file(config)  # Should not raise
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_state.py -v`
Expected: all PASS (corrupt JSON test may need adjustment based on actual error handling)

**Step 3: Full suite**

Run: `uv run pytest tests/ -v`
Expected: all PASS

**Step 4: Commit**

```bash
git add tests/test_state.py
git commit -m "test: add tests for state module"
```

---

### Task 5: Extract runner.py

**Files:**
- Create: `src/spec_runner/runner.py`
- Modify: `src/spec_runner/executor.py:40-111,249-329` (remove extracted code, add imports)

**Step 1: Create runner.py with code from executor.py**

Extract:
- Lines 43–54: `log_progress()`
- Lines 57–63: `check_error_patterns()`
- Lines 66–110: `_send_callback()`
- Lines 249–329: `build_cli_command()`

The new file needs:
```python
import json
import shlex
from datetime import datetime
from pathlib import Path

from .config import ERROR_PATTERNS, PROGRESS_FILE
```

**Step 2: Update executor.py imports**

```python
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    _send_callback,
)
```

**Step 3: Run tests and verify CLI**

Run: `uv run pytest tests/ -v && spec-runner --help`
Expected: all PASS.

**Step 4: Lint and commit**

```bash
uv run ruff check src/spec_runner/runner.py src/spec_runner/executor.py --fix
uv run ruff format src/spec_runner/runner.py src/spec_runner/executor.py
git add src/spec_runner/runner.py src/spec_runner/executor.py
git commit -m "refactor: extract runner.py from executor.py"
```

---

### Task 6: Write tests for runner.py

**Files:**
- Create: `tests/test_runner.py`

**Step 1: Write tests**

```python
"""Tests for spec_runner.runner module."""

from pathlib import Path
from unittest.mock import patch

from spec_runner.runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
)


class TestBuildCliCommand:
    """CLI command builder for different backends."""

    def test_claude_default(self):
        result = build_cli_command("claude", "hello")
        assert result == ["claude", "-p", "hello"]

    def test_claude_with_permissions_skip(self):
        result = build_cli_command("claude", "hello", skip_permissions=True)
        assert "--dangerously-skip-permissions" in result

    def test_claude_with_model(self):
        result = build_cli_command("claude", "hello", model="opus")
        assert "--model" in result
        assert "opus" in result

    def test_codex_auto_detect(self):
        result = build_cli_command("codex", "hello")
        assert result[0] == "codex"
        assert "-p" in result

    def test_ollama_auto_detect(self):
        result = build_cli_command("ollama", "hello", model="llama3")
        assert result == ["ollama", "run", "llama3", "hello"]

    def test_llama_cli_auto_detect(self):
        result = build_cli_command("llama-cli", "hello")
        assert "--no-display-prompt" in result

    def test_custom_template(self):
        result = build_cli_command(
            "mycli", "hello",
            template="{cmd} --prompt {prompt}"
        )
        assert result[0] == "mycli"
        assert "--prompt" in result

    def test_prompt_file(self):
        result = build_cli_command(
            "claude", "hello",
            template="{cmd} --file {prompt_file}",
            prompt_file=Path("/tmp/prompt.txt"),
        )
        assert "/tmp/prompt.txt" in " ".join(result)


class TestCheckErrorPatterns:
    def test_rate_limit_detected(self):
        assert check_error_patterns("Error: rate limit exceeded") is not None

    def test_context_window_detected(self):
        assert check_error_patterns("context window exceeded") is not None

    def test_normal_output_not_flagged(self):
        assert check_error_patterns("All tests passed") is None

    def test_case_insensitive(self):
        assert check_error_patterns("RATE LIMIT EXCEEDED") is not None


class TestLogProgress:
    def test_writes_to_file_and_stdout(self, tmp_path, capsys):
        with patch("spec_runner.runner.PROGRESS_FILE", tmp_path / "progress.txt"):
            log_progress("test message", task_id="TASK-001")
        captured = capsys.readouterr()
        assert "TASK-001" in captured.out
        assert "test message" in captured.out
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_runner.py -v`
Expected: all PASS

**Step 3: Full suite**

Run: `uv run pytest tests/ -v`
Expected: all PASS

**Step 4: Commit**

```bash
git add tests/test_runner.py
git commit -m "test: add tests for runner module"
```

---

### Task 7: Extract prompt.py

**Files:**
- Create: `src/spec_runner/prompt.py`
- Modify: `src/spec_runner/executor.py:583-896` (remove extracted code, add imports)
- Modify: `src/spec_runner/__init__.py` (update import for `build_task_prompt`)

**Step 1: Create prompt.py with code from executor.py**

Extract:
- Line 585: `PROMPTS_DIR` constant
- Lines 588–619: `load_prompt_template()`
- Lines 622–635: `_read_template()`
- Lines 638–655: `render_template()`
- Lines 658–712: `format_error_summary()`
- Lines 715–740: `extract_test_failures()`
- Lines 743–895: `build_task_prompt()`

The new file needs:
```python
import re
from pathlib import Path

from .config import ExecutorConfig
from .state import TaskAttempt
from .task import Task
```

**Step 2: Update executor.py imports**

```python
from .prompt import (
    build_task_prompt,
    format_error_summary,
    load_prompt_template,
    render_template,
)
```

**Step 3: Update `__init__.py`**

Import `build_task_prompt` from `.prompt` instead of `.executor`.

**Step 4: Run tests and verify CLI**

Run: `uv run pytest tests/ -v && spec-runner --help`
Expected: all PASS.

**Step 5: Lint and commit**

```bash
uv run ruff check src/spec_runner/prompt.py src/spec_runner/executor.py src/spec_runner/__init__.py --fix
uv run ruff format src/spec_runner/prompt.py src/spec_runner/executor.py src/spec_runner/__init__.py
git add src/spec_runner/prompt.py src/spec_runner/executor.py src/spec_runner/__init__.py
git commit -m "refactor: extract prompt.py from executor.py"
```

---

### Task 8: Write tests for prompt.py

**Files:**
- Create: `tests/test_prompt.py`

**Step 1: Write tests**

```python
"""Tests for spec_runner.prompt module."""

from pathlib import Path
from unittest.mock import MagicMock

from spec_runner.prompt import (
    build_task_prompt,
    extract_test_failures,
    format_error_summary,
    load_prompt_template,
    render_template,
)
from spec_runner.state import TaskAttempt


class TestRenderTemplate:
    def test_double_brace_substitution(self):
        result = render_template("Hello {{NAME}}", {"NAME": "World"})
        assert result == "Hello World"

    def test_dollar_substitution(self):
        result = render_template("Hello ${NAME}", {"NAME": "World"})
        assert result == "Hello World"

    def test_missing_variable_left_as_is(self):
        result = render_template("Hello {{NAME}}", {})
        assert "{{NAME}}" in result or "${NAME}" in result or "NAME" in result


class TestFormatErrorSummary:
    def test_truncates_long_error(self):
        error = "x" * 5000
        result = format_error_summary(error, max_lines=10)
        assert len(result) < len(error)

    def test_includes_error_text(self):
        result = format_error_summary("ImportError: no module named foo")
        assert "ImportError" in result


class TestExtractTestFailures:
    def test_extracts_failure_lines(self):
        output = """
FAILED tests/test_foo.py::test_bar - AssertionError
FAILED tests/test_foo.py::test_baz - TypeError
1 passed, 2 failed
"""
        result = extract_test_failures(output)
        assert result is not None
        assert "FAILED" in result

    def test_returns_none_for_no_failures(self):
        result = extract_test_failures("All 10 tests passed")
        # May return None or empty string depending on implementation
        assert not result or "FAILED" not in result


class TestLoadPromptTemplate:
    def test_returns_none_for_missing_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr("spec_runner.prompt.PROMPTS_DIR", tmp_path)
        result = load_prompt_template("nonexistent")
        assert result is None

    def test_loads_md_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr("spec_runner.prompt.PROMPTS_DIR", tmp_path)
        (tmp_path / "task.md").write_text("# Task {{TASK_ID}}")
        result = load_prompt_template("task")
        assert result == "# Task {{TASK_ID}}"

    def test_cli_specific_template_priority(self, tmp_path, monkeypatch):
        monkeypatch.setattr("spec_runner.prompt.PROMPTS_DIR", tmp_path)
        (tmp_path / "review.md").write_text("generic")
        (tmp_path / "review.codex.md").write_text("codex-specific")
        result = load_prompt_template("review", cli_name="codex")
        assert result == "codex-specific"


class TestBuildTaskPrompt:
    def test_includes_task_id(self, tmp_path):
        from spec_runner.config import ExecutorConfig

        task = MagicMock()
        task.id = "TASK-001"
        task.name = "Test task"
        task.priority = "p0"
        task.estimate = "1h"
        task.milestone = ""
        task.checklist = []
        task.traces_to = []
        task.depends_on = []

        config = ExecutorConfig(project_root=tmp_path)
        result = build_task_prompt(task, config)
        assert "TASK-001" in result
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_prompt.py -v`
Expected: all PASS

**Step 3: Full suite**

Run: `uv run pytest tests/ -v`
Expected: all PASS

**Step 4: Commit**

```bash
git add tests/test_prompt.py
git commit -m "test: add tests for prompt module"
```

---

### Task 9: Extract hooks.py

**Files:**
- Create: `src/spec_runner/hooks.py`
- Modify: `src/spec_runner/executor.py:898-1462` (remove extracted code, add imports)

**Step 1: Create hooks.py with code from executor.py**

Extract:
- Lines 901–904: `get_task_branch_name()`
- Lines 907–954: `get_main_branch()`
- Lines 957–984: `_ensure_on_main_branch()`
- Lines 987–1079: `pre_start_hook()`
- Lines 1082–1159: `build_review_prompt()`
- Lines 1162–1267: `run_code_review()`
- Lines 1270–1461: `post_done_hook()`

The new file needs:
```python
import re
import subprocess

from .config import ExecutorConfig
from .prompt import load_prompt_template, render_template
from .runner import build_cli_command, check_error_patterns, log_progress
from .task import Task
```

**Step 2: Update executor.py imports**

```python
from .hooks import (
    _ensure_on_main_branch,
    get_task_branch_name,
    post_done_hook,
    pre_start_hook,
    run_code_review,
)
```

**Step 3: Run tests and verify CLI**

Run: `uv run pytest tests/ -v && spec-runner --help`
Expected: all PASS.

**Step 4: Lint and commit**

```bash
uv run ruff check src/spec_runner/hooks.py src/spec_runner/executor.py --fix
uv run ruff format src/spec_runner/hooks.py src/spec_runner/executor.py
git add src/spec_runner/hooks.py src/spec_runner/executor.py
git commit -m "refactor: extract hooks.py from executor.py"
```

---

### Task 10: Write tests for hooks.py

**Files:**
- Create: `tests/test_hooks.py`

**Step 1: Write tests**

```python
"""Tests for spec_runner.hooks module."""

from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import (
    get_main_branch,
    get_task_branch_name,
)


class TestGetTaskBranchName:
    def test_basic_format(self):
        task = MagicMock()
        task.id = "TASK-001"
        task.name = "Add login page"
        result = get_task_branch_name(task)
        assert result == "task/task-001-add-login-page"

    def test_truncates_long_names(self):
        task = MagicMock()
        task.id = "TASK-002"
        task.name = "A" * 50
        result = get_task_branch_name(task)
        # Name part truncated to 30 chars
        assert len(result.split("-", 2)[-1]) <= 31  # account for dashes

    def test_replaces_slashes(self):
        task = MagicMock()
        task.id = "TASK-003"
        task.name = "Fix input/output handling"
        result = get_task_branch_name(task)
        assert "/" not in result.split("/", 1)[1]  # no slashes after "task/"


class TestGetMainBranch:
    def test_config_override(self, tmp_path):
        config = ExecutorConfig(main_branch="develop", project_root=tmp_path)
        result = get_main_branch(config)
        assert result == "develop"

    def test_fallback_when_no_git(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path)
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = get_main_branch(config)
            # Should fallback to "main" or similar default
            assert result in ("main", "master")

    def test_detects_from_remote_head(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path)
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="refs/remotes/origin/main\n",
                stderr="",
            )
            result = get_main_branch(config)
            assert result == "main"
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_hooks.py -v`
Expected: all PASS

**Step 3: Full suite**

Run: `uv run pytest tests/ -v`
Expected: all PASS

**Step 4: Commit**

```bash
git add tests/test_hooks.py
git commit -m "test: add tests for hooks module"
```

---

### Task 11: Final cleanup of executor.py + integration tests

**Files:**
- Modify: `src/spec_runner/executor.py` (verify remaining code, clean up imports)
- Modify: `src/spec_runner/__init__.py` (final import verification)
- Create: `tests/test_execution.py`

**Step 1: Verify executor.py only contains orchestration code**

After all extractions, executor.py should contain only:
- `execute_task()` (lines 1467-1612)
- `run_with_retries()` (lines 1615-1684)
- `cmd_run()`, `_run_tasks()`, `cmd_status()`, `cmd_retry()`, `cmd_logs()`, `cmd_stop()`, `cmd_reset()`, `cmd_plan()`
- `main()` + argparse

Clean up any now-unused imports. Verify all needed imports from new modules are present.

**Step 2: Write execution/retry tests**

```python
"""Tests for spec_runner.executor orchestration logic."""

from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


class TestExecuteTask:
    """Test execute_task return values and flow."""

    @patch("spec_runner.executor.subprocess.run")
    @patch("spec_runner.executor.pre_start_hook")
    @patch("spec_runner.executor.post_done_hook")
    def test_success_returns_true(self, mock_post, mock_pre, mock_run, tmp_path):
        from spec_runner.executor import execute_task

        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.json",
            logs_dir=tmp_path / "logs",
            create_git_branch=False,
        )
        state = ExecutorState(config)
        task = MagicMock()
        task.id = "TASK-001"
        task.name = "Test"
        task.priority = "p1"
        task.estimate = ""
        task.milestone = ""
        task.checklist = []
        task.traces_to = []
        task.depends_on = []

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="TASK_COMPLETE",
            stderr="",
        )
        mock_pre.return_value = None
        mock_post.return_value = True

        with patch("spec_runner.executor.build_task_prompt", return_value="prompt"):
            with patch("spec_runner.executor.build_cli_command", return_value=["echo"]):
                with patch("spec_runner.executor.log_progress"):
                    with patch("spec_runner.executor.update_task_status"):
                        with patch("spec_runner.executor.mark_all_checklist_done"):
                            result = execute_task(task, config, state)

        assert result is True

    @patch("spec_runner.executor.subprocess.run")
    @patch("spec_runner.executor.pre_start_hook")
    def test_api_error_returns_api_error(self, mock_pre, mock_run, tmp_path):
        from spec_runner.executor import execute_task

        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.json",
            logs_dir=tmp_path / "logs",
            create_git_branch=False,
        )
        state = ExecutorState(config)
        task = MagicMock()
        task.id = "TASK-001"
        task.name = "Test"
        task.priority = "p1"
        task.estimate = ""
        task.milestone = ""
        task.checklist = []
        task.traces_to = []
        task.depends_on = []

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Error: rate limit exceeded",
            stderr="",
        )
        mock_pre.return_value = None

        with patch("spec_runner.executor.build_task_prompt", return_value="prompt"):
            with patch("spec_runner.executor.build_cli_command", return_value=["echo"]):
                with patch("spec_runner.executor.log_progress"):
                    with patch("spec_runner.executor.check_error_patterns",
                               return_value="rate limit exceeded"):
                        result = execute_task(task, config, state)

        assert result == "API_ERROR"


class TestRunWithRetries:
    @patch("spec_runner.executor.execute_task")
    def test_returns_true_on_first_success(self, mock_exec, tmp_path):
        from spec_runner.executor import run_with_retries

        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.json",
            max_retries=3,
        )
        state = ExecutorState(config)
        task = MagicMock()
        task.id = "TASK-001"

        mock_exec.return_value = True
        with patch("spec_runner.executor.log_progress"):
            result = run_with_retries(task, config, state)
        assert result is True

    @patch("spec_runner.executor.execute_task")
    def test_api_error_stops_immediately(self, mock_exec, tmp_path):
        from spec_runner.executor import run_with_retries

        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.json",
            max_retries=3,
        )
        state = ExecutorState(config)
        task = MagicMock()
        task.id = "TASK-001"

        mock_exec.return_value = "API_ERROR"
        with patch("spec_runner.executor.log_progress"):
            result = run_with_retries(task, config, state)
        assert result == "API_ERROR"
        assert mock_exec.call_count == 1  # no retries on API error
```

**Step 3: Run full suite**

Run: `uv run pytest tests/ -v`
Expected: ALL tests PASS

**Step 4: Final verification**

Run: `spec-runner --help && spec-runner status --help && spec-task --help`
Expected: normal help output for all commands.

**Step 5: Lint everything**

```bash
uv run ruff check src/spec_runner/ --fix
uv run ruff format src/spec_runner/
uv run ruff check tests/ --fix
uv run ruff format tests/
```

**Step 6: Commit**

```bash
git add src/spec_runner/executor.py src/spec_runner/__init__.py tests/test_execution.py
git commit -m "refactor: finalize executor.py decomposition, add execution tests"
```

---

### Task 12: Update CLAUDE.md and verify

**Files:**
- Modify: `CLAUDE.md` (update Source Layout section)

**Step 1: Update CLAUDE.md Source Layout**

Update the architecture section to reflect the new module structure:

```markdown
### Source Layout

All code is in `src/spec_runner/`:

| Module | Purpose | ~Lines |
|---|---|---|
| `executor.py` | CLI entry point, main loop, retry orchestration | ~500 |
| `config.py` | ExecutorConfig, YAML loading, build_config | ~300 |
| `state.py` | ExecutorState, TaskState, TaskAttempt, JSON persistence | ~200 |
| `prompt.py` | Prompt building, templates, error formatting | ~350 |
| `hooks.py` | Pre/post hooks, git ops, code review | ~600 |
| `runner.py` | CLI command building, subprocess exec, progress logging | ~200 |
| `task.py` | Task parsing, dependency resolution, status management | ~780 |
| `init_cmd.py` | Install bundled Claude Code skills | ~100 |
```

**Step 2: Run full test suite one last time**

Run: `uv run pytest tests/ -v`
Expected: all tests PASS

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for new module structure"
```
