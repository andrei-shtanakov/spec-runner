# `qwen` + `copilot` Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `qwen` (Qwen Code CLI) and `copilot` (GitHub Copilot CLI) as `spec-runner config` presets, by re-introducing per-fragment command templates for CLIs that are not auto-detected.

**Architecture:** The `Fragment` dataclass gains optional `exec_template` / `review_template` fields. `compose` maps them into `command_template` / `review_command_template` (empty for the 6 auto-detect presets, non-empty for qwen/copilot). Two new bundled fragment YAMLs carry the verified headless invocations. `copilot` is removed from `load_fragment`'s reject list.

**Tech Stack:** Python 3.10+, PyYAML, importlib.resources, argparse, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-config-presets-design.md` — see the **Revision 4** section (authoritative for this increment).

**Branch:** `feat/config-presets-qwen-copilot` (already checked out).

**Target version:** 2.6.0 (minor).

---

## File Structure

- Modify `src/spec_runner/preset_cmd.py` — `Fragment` (+2 fields), `load_fragment` (parse templates, drop copilot reject), `compose` (use templates), `PRESET_NAMES` (+qwen, +copilot).
- Create `src/spec_runner/presets/qwen.yaml`, `src/spec_runner/presets/copilot.yaml`.
- Modify `tests/test_presets.py` — update the copilot-reject test, add template/loading/compose tests.
- Modify `README.md`, `CHANGELOG.md`, `CLAUDE.md` — docs.

---

## Task 1: Fragment templates + qwen/copilot fragments

**Files:**
- Modify: `src/spec_runner/preset_cmd.py`
- Create: `src/spec_runner/presets/qwen.yaml`, `src/spec_runner/presets/copilot.yaml`
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write/adjust the failing tests**

First, the existing tests that assume copilot is rejected must change. In `tests/test_presets.py`:

- DELETE `test_load_fragment_copilot_rejected_with_hint` (copilot is now valid).
- REPLACE `test_config_copilot_exits_2` (it asserted copilot exits 2) with a test that copilot is now accepted through the parser (see below).
- UPDATE `test_list_presets_has_six_known_clis` → it must now expect 8 names; rename to `test_list_presets_has_known_clis`:

```python
def test_list_presets_has_known_clis():
    assert list_presets() == [
        "claude",
        "codex",
        "opencode",
        "pi",
        "ollama",
        "llama-cli",
        "qwen",
        "copilot",
    ]
```

Then APPEND the new tests:

```python
def test_load_fragment_qwen_has_templates():
    frag = load_fragment("qwen")
    assert frag.command == "qwen"
    assert "--approval-mode yolo" in frag.exec_template
    assert "--approval-mode plan" in frag.review_template


def test_load_fragment_copilot_has_templates_and_is_not_rejected():
    frag = load_fragment("copilot")
    assert frag.command == "copilot"
    assert "--allow-all-tools" in frag.exec_template
    assert "--allow-tool='shell'" in frag.review_template


def test_auto_detect_presets_have_empty_templates():
    for name in ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]:
        frag = load_fragment(name)
        assert frag.exec_template == ""
        assert frag.review_template == ""


def test_compose_exec_template_lands_in_command_template():
    profile = compose(load_fragment("qwen"), load_fragment("claude"))
    assert "--approval-mode yolo" in profile["command_template"]
    # review slot is an auto-detect CLI → template cleared
    assert profile["review_command_template"] == ""


def test_compose_review_template_lands_in_review_command_template():
    profile = compose(load_fragment("claude"), load_fragment("copilot"))
    assert profile["command_template"] == ""  # exec is auto-detect
    assert "--allow-tool='shell'" in profile["review_command_template"]


def test_compose_mono_copilot_fills_both_template_slots():
    profile = compose(load_fragment("copilot"), load_fragment("copilot"))
    assert "--allow-all-tools" in profile["command_template"]
    assert "--allow-tool='shell'" in profile["review_command_template"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_presets.py -k "qwen or copilot or template or known_clis" -q`
Expected: FAIL — qwen/copilot fragments don't exist, `Fragment` has no `exec_template`, `load_fragment("copilot")` still raises.

- [ ] **Step 3: Add the two fragment fields to `Fragment`**

In `src/spec_runner/preset_cmd.py`, change the dataclass:

```python
@dataclass(frozen=True)
class Fragment:
    """Slot-neutral description of how to invoke one CLI."""

    command: str
    model: str = ""
    skip_permissions: bool = False
    exec_template: str = ""
    review_template: str = ""
    note: str = ""
```

- [ ] **Step 4: Update `PRESET_NAMES` and `load_fragment`**

Change `PRESET_NAMES` to include the two new presets:

```python
# CLI names: the first six are auto-detected by runner.build_cli_invocation;
# qwen and copilot are template-driven (not auto-detected).
PRESET_NAMES = [
    "claude",
    "codex",
    "opencode",
    "pi",
    "ollama",
    "llama-cli",
    "qwen",
    "copilot",
]
```

In `load_fragment`, REMOVE the `if name == "copilot": raise ValueError(...)` block entirely, and parse the two new fields:

```python
def load_fragment(name: str) -> Fragment:
    """Load a preset fragment by CLI name from bundled package data."""
    if name not in PRESET_NAMES:
        valid = ", ".join(PRESET_NAMES)
        raise ValueError(f"Unknown preset '{name}'. Valid presets: {valid}")
    resource = files("spec_runner") / "presets" / f"{name}.yaml"
    data = yaml.safe_load(resource.read_text()) or {}
    if "command" not in data:
        raise ValueError(f"Preset file for '{name}' is missing required 'command' key")
    return Fragment(
        command=data["command"],
        model=data.get("model", ""),
        skip_permissions=bool(data.get("skip_permissions", False)),
        exec_template=data.get("exec_template", ""),
        review_template=data.get("review_template", ""),
        note=data.get("note", ""),
    )
```

- [ ] **Step 5: Update `compose` to use the templates**

Replace the two hard-coded `""` template values with the fragment templates (keep the docstring note about clearing — auto-detect fragments still yield `""`):

```python
    """Map an (exec, review) fragment pair into the 7 CLI-profile keys.

    `command_template` comes from the exec fragment, `review_command_template`
    from the review fragment. Auto-detect CLIs carry empty templates, which also
    clears any stale template from a previously configured CLI. Model precedence:
    per-slot override > shared --model override > fragment default.
    """
    exec_model = model_override or exec_frag.model
    review_model = review_model_override or model_override or review_frag.model
    return {
        "claude_command": exec_frag.command,
        "claude_model": exec_model,
        "command_template": exec_frag.exec_template,
        "skip_permissions": exec_frag.skip_permissions,
        "review_command": review_frag.command,
        "review_model": review_model,
        "review_command_template": review_frag.review_template,
    }
```

- [ ] **Step 6: Create the two fragment YAMLs**

```yaml
# src/spec_runner/presets/qwen.yaml
command: qwen
model: ""
skip_permissions: false
exec_template: "{cmd} -p {prompt} --approval-mode yolo"
review_template: "{cmd} -p {prompt} --approval-mode plan"
note: "Qwen Code: set the model in ~/.qwen/settings.json (modelProviders). yolo is required for headless edits; plan = read-only review."
```

```yaml
# src/spec_runner/presets/copilot.yaml
command: copilot
model: ""
skip_permissions: false
exec_template: "{cmd} -p {prompt} -s --no-ask-user --allow-all-tools"
review_template: "{cmd} -p {prompt} -s --no-ask-user --allow-tool='shell'"
note: "GitHub Copilot CLI: needs Copilot access (gh auth / COPILOT_GITHUB_TOKEN). Set the model via COPILOT_MODEL env or add --model <id> to command_template. -s gives clean output so the TASK_COMPLETE marker is parseable."
```

- [ ] **Step 7: Add the parser-level acceptance test for copilot**

Replace the deleted `test_config_copilot_exits_2` with a positive test (append to `tests/test_presets.py`):

```python
def test_config_preset_qwen_writes_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "qwen"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "qwen"
    assert "--approval-mode yolo" in loaded["command_template"]
    assert "--approval-mode plan" in loaded["review_command_template"]


def test_config_preset_copilot_no_longer_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "copilot"])
    cmd_config(args, None)  # must NOT raise SystemExit
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "copilot"
    assert "--allow-all-tools" in loaded["command_template"]
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_presets.py -q`
Expected: PASS (all existing + new tests; the copilot-reject test is gone).

- [ ] **Step 9: Lint, format, type-check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/spec_runner/preset_cmd.py`
Expected: all clean. If `uv run` bumps `uv.lock`, revert it.

- [ ] **Step 10: Commit**

```bash
git add src/spec_runner/preset_cmd.py src/spec_runner/presets/qwen.yaml src/spec_runner/presets/copilot.yaml tests/test_presets.py
git commit -m "feat(config): add qwen and copilot presets (template-driven)"
```

---

## Task 2: Documentation

**Files:**
- Modify: `CHANGELOG.md`, `CLAUDE.md`, `README.md`

- [ ] **Step 1: CHANGELOG entry**

Under `## [Unreleased]`, add (create `### Added` if absent):

```markdown
### Added

- **`config` presets for `qwen` and `copilot`.** `spec-runner config --preset
  qwen` (Qwen Code CLI) and `--preset copilot` (GitHub Copilot CLI) now write the
  correct headless `command_template` / `review_command_template` (these CLIs are
  not auto-detected). qwen uses `--approval-mode yolo` for exec and `plan` for the
  read-only review; copilot uses `-s --no-ask-user --allow-all-tools` for exec and
  `--allow-tool='shell'` for review. The model is configured in each CLI's own
  settings/env (see the printed note); `--model` does not apply to these two.
  Preset list is now: claude, codex, opencode, pi, ollama, llama-cli, qwen, copilot.
```

- [ ] **Step 2: CLAUDE.md CLI table**

After the existing `spec-runner config --list-presets` line, add:

```bash
spec-runner config --preset qwen           # Qwen Code CLI (template-driven)
spec-runner config --preset copilot        # GitHub Copilot CLI (template-driven)
```

- [ ] **Step 3: README — Qwen section**

In the "Switching CLI" area of `README.md`, add a subsection (use a real ```bash fence):

```markdown
#### Using Qwen

Qwen works two cheap ways, both already supported:

- **Cloud (cheap), via OpenCode:** `spec-runner config --preset opencode --model "openrouter/qwen/qwen3-coder"` (any OpenCode-supported Qwen provider/model string).
- **Local, via Ollama:** `ollama pull qwen2.5-coder:32b` then `spec-runner config --preset ollama --model "qwen2.5-coder:32b"`.

Or use the official agents directly:

- **Qwen Code CLI:** `spec-runner config --preset qwen` (set the model in `~/.qwen/settings.json`).
- **GitHub Copilot CLI:** `spec-runner config --preset copilot` (needs Copilot access; set the model via `COPILOT_MODEL`).

Run `spec-runner doctor` afterwards to confirm the chosen CLI is READY.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md CLAUDE.md README.md
git commit -m "docs(config): document qwen/copilot presets and Qwen usage paths"
```

---

## Task 3: Full verification

- [ ] **Step 1: Whole non-slow suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: PASS (no regressions; new preset tests included).

- [ ] **Step 2: Lint / format / mypy**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: clean.

- [ ] **Step 3: Real-CLI smoke + wheel ships new fragments**

Run:
```bash
uv run spec-runner config --list-presets   # expect 8 names incl qwen, copilot
uv build --wheel && python3 -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]); print(sorted(n for n in z.namelist() if 'presets/' in n))"
```
Expected: `--list-presets` prints 8 names; wheel lists 8 `presets/*.yaml` including `qwen.yaml` and `copilot.yaml`. Then `rm -rf dist/*.whl`.

- [ ] **Step 4: Commit any lint/format fixups (if needed)**

```bash
git add -A && git commit -m "chore(config): lint/format pass for qwen/copilot presets"
```

---

## Self-Review (completed during authoring)

- **Spec coverage (Revision 4):** template fields on `Fragment` (Task 1 Step 3); composer uses templates (Step 5); qwen + copilot fragments with verified flags (Step 6); copilot reject removed (Step 4); PRESET_NAMES → 8 (Step 4); model-less templates (fragments carry no `{model}`); docs incl. OpenCode/Ollama Qwen paths (Task 2 Step 3); packaging verified (Task 3 Step 3 — `package-data` already globs `presets/*.yaml`, so new files ship automatically). All Revision-4 points map to a task.
- **Placeholder scan:** none — complete code/YAML in every step; the only deletions (copilot-reject test, `test_config_copilot_exits_2`) are explicit.
- **Type/name consistency:** `Fragment(exec_template, review_template)`, `compose`, `load_fragment`, `PRESET_NAMES`, `PROFILE_KEYS`, `cmd_config(args, config=None)`, `_build_parser`, `load_config_from_yaml` used consistently with the existing v2.5.0 code (verified against current `preset_cmd.py`).
