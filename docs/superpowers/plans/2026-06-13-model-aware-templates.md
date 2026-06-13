# Model-aware Templates (qwen/copilot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `--model` apply to the template-driven `qwen`/`copilot` presets, without the blank-`--model` trap.

**Architecture:** `Fragment` gains a `model_flag` field (`--model` for qwen/copilot, `""` for auto-detect presets). `compose` appends `{model_flag} {model}` to a slot's template only when the slot has a template, a `model_flag`, and a non-empty resolved model.

**Tech Stack:** Python 3.10+, PyYAML, argparse, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-config-presets-design.md` — **Revision 5** (authoritative for this increment).

**Branch:** `feat/model-aware-templates` (already checked out).

**Target version:** 2.7.0 (minor).

---

## File Structure

- Modify `src/spec_runner/preset_cmd.py` — `Fragment` (+`model_flag`), `load_fragment` (parse it), `compose` (`_apply_model_flag` helper).
- Modify `src/spec_runner/presets/qwen.yaml`, `src/spec_runner/presets/copilot.yaml` — add `model_flag: "--model"`, update `note`.
- Modify `tests/test_presets.py` — model-aware tests.
- Modify `CHANGELOG.md` — Added entry.

---

## Task 1: model_flag field + conditional append

**Files:**
- Modify: `src/spec_runner/preset_cmd.py`
- Modify: `src/spec_runner/presets/qwen.yaml`, `src/spec_runner/presets/copilot.yaml`
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_presets.py`)

```python
def test_qwen_copilot_fragments_have_model_flag():
    assert load_fragment("qwen").model_flag == "--model"
    assert load_fragment("copilot").model_flag == "--model"


def test_auto_detect_fragments_have_no_model_flag():
    for name in ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]:
        assert load_fragment(name).model_flag == ""


def test_compose_templated_preset_with_model_appends_flag():
    profile = compose(load_fragment("qwen"), load_fragment("qwen"), model_override="qwen-coder-plus")
    assert profile["command_template"].endswith("--model {model}")
    assert profile["review_command_template"].endswith("--model {model}")
    assert profile["claude_model"] == "qwen-coder-plus"
    assert profile["review_model"] == "qwen-coder-plus"


def test_compose_templated_preset_without_model_has_no_flag():
    # anti-trap regression: empty model must NOT produce a dangling --model
    profile = compose(load_fragment("copilot"), load_fragment("copilot"))
    assert "--model" not in profile["command_template"]
    assert "--model" not in profile["review_command_template"]


def test_compose_auto_detect_preset_with_model_keeps_empty_template():
    profile = compose(load_fragment("claude"), load_fragment("claude"), model_override="sonnet")
    assert profile["command_template"] == ""
    assert profile["review_command_template"] == ""
    assert profile["claude_model"] == "sonnet"  # model flows via auto-detect, not the template


def test_compose_multi_exec_qwen_review_claude_with_model():
    profile = compose(load_fragment("qwen"), load_fragment("claude"), model_override="qwen-coder-plus")
    assert profile["command_template"].endswith("--model {model}")   # exec qwen templated
    assert profile["review_command_template"] == ""                   # review claude auto-detect
    assert profile["review_model"] == "qwen-coder-plus"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_presets.py -k "model_flag or templated_preset or auto_detect_preset_with_model or multi_exec_qwen" -q`
Expected: FAIL — `Fragment` has no `model_flag`; templates don't get the flag appended.

- [ ] **Step 3: Add `model_flag` to `Fragment` and parse it**

In `src/spec_runner/preset_cmd.py`, add the field (after `review_template`):

```python
@dataclass(frozen=True)
class Fragment:
    """Slot-neutral description of how to invoke one CLI."""

    command: str
    model: str = ""
    skip_permissions: bool = False
    exec_template: str = ""
    review_template: str = ""
    model_flag: str = ""
    note: str = ""
```

In `load_fragment`, parse it (add alongside the other `data.get(...)` lines):

```python
        model_flag=data.get("model_flag", ""),
```

- [ ] **Step 4: Add `_apply_model_flag` and use it in `compose`**

Add the helper above `compose` (or just below the imports/constants):

```python
def _apply_model_flag(template: str, model_flag: str, model: str) -> str:
    """Append the model flag to a template only when there is a template, a flag,
    and a non-empty model — avoids a dangling `--model` with no value."""
    if template and model_flag and model:
        return f"{template} {model_flag} {{model}}"
    return template
```

In `compose`, change the two template values to use it:

```python
    exec_model = model_override or exec_frag.model
    review_model = review_model_override or model_override or review_frag.model
    return {
        "claude_command": exec_frag.command,
        "claude_model": exec_model,
        "command_template": _apply_model_flag(
            exec_frag.exec_template, exec_frag.model_flag, exec_model
        ),
        "skip_permissions": exec_frag.skip_permissions,
        "review_command": review_frag.command,
        "review_model": review_model,
        "review_command_template": _apply_model_flag(
            review_frag.review_template, review_frag.model_flag, review_model
        ),
    }
```

Update the `compose` docstring to note the model flag is appended only when a model is set.

- [ ] **Step 5: Update the two fragment YAMLs**

`src/spec_runner/presets/qwen.yaml` — add `model_flag` and update `note`:

```yaml
command: qwen
model: ""
skip_permissions: false
exec_template: "{cmd} -p {prompt} --approval-mode yolo"
review_template: "{cmd} -p {prompt} --approval-mode plan"
model_flag: "--model"
note: "Qwen Code: --model now applies (e.g. --preset qwen --model qwen-coder-plus); or set it in ~/.qwen/settings.json (modelProviders) / QWEN_MODEL. yolo is required for headless edits; plan = read-only review."
```

`src/spec_runner/presets/copilot.yaml` — add `model_flag` and update `note`:

```yaml
command: copilot
model: ""
skip_permissions: false
exec_template: "{cmd} -p {prompt} -s --no-ask-user --allow-all-tools"
review_template: "{cmd} -p {prompt} -s --no-ask-user --allow-tool='shell'"
model_flag: "--model"
note: "GitHub Copilot CLI: needs Copilot access (gh auth / COPILOT_GITHUB_TOKEN). --model now applies (e.g. --preset copilot --model claude-haiku-4.5); or set COPILOT_MODEL. -s gives clean output so the TASK_COMPLETE marker is parseable."
```

- [ ] **Step 6: Add an end-to-end parser test** (append to `tests/test_presets.py`)

```python
def test_config_preset_qwen_with_model_through_parser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "qwen", "--model", "qwen-coder-plus"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert "--model {model}" in loaded["command_template"]
    assert loaded["claude_model"] == "qwen-coder-plus"
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_presets.py -q`
Expected: PASS (all existing + new).

- [ ] **Step 8: Lint / format / type-check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/spec_runner/preset_cmd.py`
Expected: clean. If `uv run` bumps `uv.lock`, revert it.

- [ ] **Step 9: Commit**

```bash
git add src/spec_runner/preset_cmd.py src/spec_runner/presets/qwen.yaml src/spec_runner/presets/copilot.yaml tests/test_presets.py
git commit -m "feat(config): model-aware templates for qwen/copilot (--model now applies)"
```

---

## Task 2: Docs + verification

**Files:**
- Modify: `CHANGELOG.md`, `README.md`

- [ ] **Step 1: CHANGELOG entry** — under `## [Unreleased]`, add (create `### Added` if absent):

```markdown
### Added

- **`--model` now applies to the `qwen` and `copilot` presets.** Previously these
  template-driven presets ignored `--model` (model was set in the CLI's own
  settings/env). Now `spec-runner config --preset qwen --model qwen-coder-plus`
  (or `copilot --model claude-haiku-4.5`) appends `--model <id>` to the generated
  `command_template`. A blank model still omits the flag (no dangling `--model`).
```

- [ ] **Step 2: README** — in the "Using Qwen" section, update the qwen/copilot bullets to show `--model`:

```markdown
- **Qwen Code CLI:** `spec-runner config --preset qwen --model qwen-coder-plus` (or set the model in `~/.qwen/settings.json`).
- **GitHub Copilot CLI:** `spec-runner config --preset copilot --model claude-haiku-4.5` (needs Copilot access).
```

(Adapt to the exact existing bullet wording.)

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs(config): document --model for qwen/copilot presets"
```

- [ ] **Step 4: Full verification**

Run:
```bash
uv run pytest tests/ -q -m "not slow"
uv run ruff check . && uv run ruff format --check . && uv run mypy src
uv run spec-runner config --preset qwen --model qwen-coder-plus --dry-run   # shows --model {model} in command_template
```
Expected: suite green; lint/mypy clean; dry-run shows the model flag appended.

---

## Self-Review (completed during authoring)

- **Spec coverage (Rev 5):** `model_flag` field (Task 1 Step 3); `_apply_model_flag` + composer use (Step 4); both YAMLs updated incl. notes (Step 5); anti-trap empty-model test (Step 1); auto-detect-unaffected test (Step 1); multi-CLI test (Step 1); end-to-end parser test (Step 6); docs (Task 2). All Rev-5 points map to a task.
- **Placeholder scan:** none — complete code in every step.
- **Type/name consistency:** `Fragment(model_flag)`, `_apply_model_flag`, `compose`, `load_fragment`, `cmd_config(args, config=None)`, `_build_parser`, `load_config_from_yaml` match the current v2.6.0 code. The appended `{{model}}` correctly yields a literal `{model}` runtime placeholder substituted by `build_cli_command` with the (guaranteed non-empty) `claude_model`/`review_model`.
