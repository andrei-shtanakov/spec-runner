# `spec-runner config` CLI Presets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `spec-runner config` subcommand that writes/updates the CLI
profile (exec + review CLIs) in `spec-runner.config.yaml` from a small library
of per-CLI preset fragments — supporting mono (`--preset X`) and multi
(`--exec X --review Y`) pipelines.

**Architecture:** Six slot-neutral YAML fragments (`presets/*.yaml`) describe how
to invoke each auto-detected CLI. A composer maps an (exec, review) fragment
pair into the 7 CLI-profile config keys. An applier writes a fresh flat v2.0
file (static text template) or surgically merges the 7 keys into an existing
config (PyYAML `safe_load`/`safe_dump`, shape-preserving), backing up to `.bak`.

**Tech Stack:** Python 3.10+, PyYAML, `importlib.resources`, argparse, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-config-presets-design.md` (rev 3).

**Branch:** `feat/config-presets` (already checked out).

---

## File Structure

- Create `src/spec_runner/preset_cmd.py` — `Fragment`, `load_fragment`,
  `list_presets`, `compose`, `apply_to_config`, `cmd_config`, helpers.
- Create `src/spec_runner/presets/{claude,codex,opencode,pi,ollama,llama-cli}.yaml`
  — fragment data.
- Modify `src/spec_runner/cli.py` — register the `config` subparser; add
  `cmd_config` to the dispatch table.
- Modify `src/spec_runner/validate.py` — validate flat configs, not just
  `executor:`-wrapped.
- Modify `pyproject.toml` — ship `presets/*.yaml` in the wheel.
- Create `tests/test_presets.py` — full coverage.
- Modify `README.md`, `CLAUDE.md`, `CHANGELOG.md` — docs.

---

## Task 1: Fragment library + loader

**Files:**
- Create: `src/spec_runner/preset_cmd.py`
- Create: `src/spec_runner/presets/claude.yaml`, `codex.yaml`, `opencode.yaml`,
  `pi.yaml`, `ollama.yaml`, `llama-cli.yaml`
- Modify: `pyproject.toml:59`
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_presets.py
import pytest

from spec_runner.preset_cmd import Fragment, list_presets, load_fragment


def test_list_presets_has_six_known_clis():
    assert list_presets() == [
        "claude", "codex", "opencode", "pi", "ollama", "llama-cli",
    ]


def test_load_fragment_claude_keeps_skip_permissions_true():
    frag = load_fragment("claude")
    assert frag == Fragment(command="claude", model="", skip_permissions=True, note="")


def test_load_fragment_codex_is_skip_permissions_false():
    assert load_fragment("codex").skip_permissions is False


def test_load_fragment_llama_cli_command_is_llama_cli():
    # bare "llama" would fall through auto-detect to the claude branch
    assert load_fragment("llama-cli").command == "llama-cli"


def test_load_fragment_pi_has_model_note():
    assert "pi --list-models" in load_fragment("pi").note


def test_load_fragment_unknown_raises():
    with pytest.raises(ValueError, match="Unknown preset"):
        load_fragment("nope")


def test_load_fragment_copilot_rejected_with_hint():
    with pytest.raises(ValueError, match="copilot is not supported"):
        load_fragment("copilot")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_presets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'spec_runner.preset_cmd'`

- [ ] **Step 3: Create the fragment YAML files**

```yaml
# src/spec_runner/presets/claude.yaml
command: claude
model: ""
skip_permissions: true
note: ""
```

```yaml
# src/spec_runner/presets/codex.yaml
command: codex
model: ""
skip_permissions: false
note: ""
```

```yaml
# src/spec_runner/presets/opencode.yaml
command: opencode
model: ""
skip_permissions: false
note: ""
```

```yaml
# src/spec_runner/presets/pi.yaml
command: pi
model: ""
skip_permissions: false
note: "Set claude_model to a model your pi install is authenticated for (pi --list-models)."
```

```yaml
# src/spec_runner/presets/ollama.yaml
command: ollama
model: ""
skip_permissions: false
note: "Blank model defaults to llama3 at runtime; set claude_model for a different model."
```

```yaml
# src/spec_runner/presets/llama-cli.yaml
command: llama-cli
model: ""
skip_permissions: false
note: ""
```

- [ ] **Step 4: Create `preset_cmd.py` with the loader**

```python
# src/spec_runner/preset_cmd.py
"""spec-runner config — apply CLI profile presets to spec-runner.config.yaml."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

CONFIG_FILE = Path("spec-runner.config.yaml")
LEGACY_CONFIG_FILE = Path("spec/executor.config.yaml")

# CLI names recognised by runner.build_cli_invocation auto-detect.
PRESET_NAMES = ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]

# The 7 CLI-profile keys the composer manages (top-level executor-mapping keys).
PROFILE_KEYS = [
    "claude_command",
    "claude_model",
    "command_template",
    "skip_permissions",
    "review_command",
    "review_model",
    "review_command_template",
]


@dataclass(frozen=True)
class Fragment:
    """Slot-neutral description of how to invoke one CLI."""

    command: str
    model: str = ""
    skip_permissions: bool = False
    note: str = ""


def list_presets() -> list[str]:
    """Return the available preset names."""
    return list(PRESET_NAMES)


def load_fragment(name: str) -> Fragment:
    """Load a preset fragment by CLI name from bundled package data."""
    if name == "copilot":
        raise ValueError(
            "copilot is not supported in v1 (no auto-detect); set "
            "command_template manually in spec-runner.config.yaml."
        )
    if name not in PRESET_NAMES:
        valid = ", ".join(PRESET_NAMES)
        raise ValueError(f"Unknown preset '{name}'. Valid presets: {valid}")
    resource = files("spec_runner") / "presets" / f"{name}.yaml"
    data = yaml.safe_load(resource.read_text()) or {}
    return Fragment(
        command=data["command"],
        model=data.get("model", ""),
        skip_permissions=bool(data.get("skip_permissions", False)),
        note=data.get("note", ""),
    )
```

- [ ] **Step 5: Add presets to package data**

In `pyproject.toml`, change line 59 from:

```toml
spec_runner = ["skills/**/*"]
```

to:

```toml
spec_runner = ["skills/**/*", "presets/*.yaml"]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_presets.py -q`
Expected: PASS (7 tests)

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/preset_cmd.py src/spec_runner/presets pyproject.toml tests/test_presets.py
git commit -m "feat(config): preset fragment library + loader"
```

---

## Task 2: Compose fragments into the 7 profile keys

**Files:**
- Modify: `src/spec_runner/preset_cmd.py`
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_presets.py
from spec_runner.preset_cmd import compose


def test_compose_mono_codex():
    frag = load_fragment("codex")
    profile = compose(frag, frag)
    assert profile == {
        "claude_command": "codex",
        "claude_model": "",
        "command_template": "",
        "skip_permissions": False,
        "review_command": "codex",
        "review_model": "",
        "review_command_template": "",
    }


def test_compose_multi_claude_exec_codex_review():
    profile = compose(load_fragment("claude"), load_fragment("codex"))
    assert profile["claude_command"] == "claude"
    assert profile["skip_permissions"] is True  # from exec (claude)
    assert profile["review_command"] == "codex"


def test_compose_clears_templates():
    profile = compose(load_fragment("pi"), load_fragment("claude"))
    assert profile["command_template"] == ""
    assert profile["review_command_template"] == ""


def test_compose_model_override_applies_to_both_slots():
    profile = compose(load_fragment("codex"), load_fragment("codex"), model_override="o3")
    assert profile["claude_model"] == "o3"
    assert profile["review_model"] == "o3"


def test_compose_review_model_override_targets_review_only():
    profile = compose(
        load_fragment("claude"),
        load_fragment("codex"),
        model_override="sonnet",
        review_model_override="o3",
    )
    assert profile["claude_model"] == "sonnet"
    assert profile["review_model"] == "o3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_presets.py -k compose -q`
Expected: FAIL — `ImportError: cannot import name 'compose'`

- [ ] **Step 3: Implement `compose`**

```python
# append to src/spec_runner/preset_cmd.py
def compose(
    exec_frag: Fragment,
    review_frag: Fragment,
    model_override: str = "",
    review_model_override: str = "",
) -> dict:
    """Map an (exec, review) fragment pair into the 7 CLI-profile keys.

    `command_template` / `review_command_template` are always cleared to "" so a
    stale template from a previously configured CLI does not leak. Model
    precedence: per-slot override > shared --model override > fragment default.
    """
    exec_model = model_override or exec_frag.model
    review_model = review_model_override or model_override or review_frag.model
    return {
        "claude_command": exec_frag.command,
        "claude_model": exec_model,
        "command_template": "",
        "skip_permissions": exec_frag.skip_permissions,
        "review_command": review_frag.command,
        "review_model": review_model,
        "review_command_template": "",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_presets.py -k compose -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/preset_cmd.py tests/test_presets.py
git commit -m "feat(config): compose fragments into 7 profile keys"
```

---

## Task 3: Fresh write (no existing config → flat v2.0 file)

**Files:**
- Modify: `src/spec_runner/preset_cmd.py`
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_presets.py
from spec_runner.config import load_config_from_yaml
from spec_runner.preset_cmd import apply_to_config


def test_fresh_write_creates_flat_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    written = apply_to_config(profile, apply_changes=False, dry_run=False)
    assert written == Path("spec-runner.config.yaml")
    text = written.read_text()
    # flat v2.0 — no executor: wrapper
    assert "executor:" not in text
    assert "claude_command:" in text
    # round-trips through the real loader
    loaded = load_config_from_yaml(written)
    assert loaded["claude_command"] == "codex"
    assert loaded["review_command"] == "codex"


def test_fresh_write_renders_skip_permissions_bool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = compose(load_fragment("claude"), load_fragment("claude"))
    written = apply_to_config(profile, apply_changes=False, dry_run=False)
    assert load_config_from_yaml(written)["skip_permissions"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_presets.py -k fresh_write -q`
Expected: FAIL — `ImportError: cannot import name 'apply_to_config'`

- [ ] **Step 3: Implement `apply_to_config` fresh-write path + helpers**

```python
# append to src/spec_runner/preset_cmd.py
_FRESH_HEADER = """\
# spec-runner configuration (v2.0 flat format)
# Generated by `spec-runner config`. Edit freely.

# --- CLI profile (managed by `spec-runner config`) ---
"""

_FRESH_SCAFFOLD = """\

# --- Common knobs (uncomment to use) ---
# budget_usd: 10.0
# task_budget_usd: 2.0
# max_concurrent: 3
# telegram_bot_token: ""      # secret — keep out of version control
# telegram_chat_id: ""
# webhook_url: ""
"""


def _fmt_scalar(value: object) -> str:
    """Render a profile value as a YAML scalar for the static fresh template."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return '""' if value == "" else f'"{value}"'


def _render_fresh(profile: dict) -> str:
    """Build a flat v2.0 config file as static text (preserves comments)."""
    lines = "\n".join(f"{k}: {_fmt_scalar(profile[k])}" for k in PROFILE_KEYS)
    return _FRESH_HEADER + lines + "\n" + _FRESH_SCAFFOLD


def apply_to_config(
    profile: dict,
    *,
    apply_changes: bool,
    dry_run: bool,
    config_path: Path = CONFIG_FILE,
) -> Path | None:
    """Write a fresh config, preview (dry-run), refuse, or surgically merge.

    Returns the written path, or None for a dry-run.
    """
    target_path = config_path
    if not target_path.exists() and LEGACY_CONFIG_FILE.exists():
        target_path = LEGACY_CONFIG_FILE

    if dry_run:
        _print_profile(profile)
        return None

    if not target_path.exists():
        config_path.write_text(_render_fresh(profile))
        return config_path

    # merge path implemented in Task 5
    raise NotImplementedError


def _print_profile(profile: dict) -> None:
    """Print the 7 keys that would be written (stdout)."""
    for key in PROFILE_KEYS:
        print(f"{key}: {profile[key]!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_presets.py -k fresh_write -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/preset_cmd.py tests/test_presets.py
git commit -m "feat(config): fresh flat v2.0 write from static template"
```

---

## Task 4: Dry-run preview and refuse-without-apply

**Files:**
- Modify: `src/spec_runner/preset_cmd.py` (no change needed — verify behaviour)
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_presets.py
def test_dry_run_writes_nothing_and_prints_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    result = apply_to_config(profile, apply_changes=False, dry_run=True)
    assert result is None
    assert not Path("spec-runner.config.yaml").exists()
    out = capsys.readouterr().out
    assert "claude_command:" in out
    assert "review_command_template:" in out


def test_refuse_without_apply_exits_1_and_leaves_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("claude_command: claude\nbudget_usd: 5.0\n")
    original = cfg.read_text()
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    with pytest.raises(SystemExit) as exc:
        apply_to_config(profile, apply_changes=False, dry_run=False)
    assert exc.value.code == 1
    assert cfg.read_text() == original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_presets.py -k "dry_run or refuse" -q`
Expected: FAIL — `test_refuse...` raises `NotImplementedError`, not `SystemExit`

- [ ] **Step 3: Add the refuse branch to `apply_to_config`**

Replace the `# merge path implemented in Task 5` block with:

```python
    if not apply_changes:
        _print_profile(profile)
        print(
            f"{target_path} exists. Re-run with --apply to update the CLI "
            "profile (other settings preserved), or --dry-run to preview.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return _merge_into_existing(profile, target_path)
```

And add a temporary stub so the module imports (replaced in Task 5):

```python
def _merge_into_existing(profile: dict, target_path: Path) -> Path:
    raise NotImplementedError  # implemented in Task 5
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_presets.py -k "dry_run or refuse" -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/preset_cmd.py tests/test_presets.py
git commit -m "feat(config): dry-run preview and refuse-without-apply"
```

---

## Task 5: Surgical merge into existing config (`--apply`)

**Files:**
- Modify: `src/spec_runner/preset_cmd.py`
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_presets.py
def test_apply_merges_flat_preserving_other_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text(
        "claude_command: claude\nbudget_usd: 10.0\ntelegram_bot_token: secret123\n"
    )
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    apply_to_config(profile, apply_changes=True, dry_run=False)
    loaded = load_config_from_yaml(cfg)
    assert loaded["claude_command"] == "codex"
    assert loaded["review_command"] == "codex"
    assert loaded["budget_usd"] == 10.0
    assert loaded["telegram_bot_token"] == "secret123"
    assert "executor:" not in cfg.read_text()  # flat stays flat
    assert Path("spec-runner.config.yaml.bak").exists()


def test_apply_merges_wrapped_preserving_wrapper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("executor:\n  claude_command: claude\n  budget_usd: 7.0\n")
    profile = compose(load_fragment("pi"), load_fragment("claude"))
    apply_to_config(profile, apply_changes=True, dry_run=False)
    assert "executor:" in cfg.read_text()  # wrapped stays wrapped
    loaded = load_config_from_yaml(cfg)
    assert loaded["claude_command"] == "pi"
    assert loaded["review_command"] == "claude"
    assert loaded["budget_usd"] == 7.0


def test_apply_malformed_yaml_aborts_without_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Path("spec-runner.config.yaml")
    cfg.write_text("claude_command: [unclosed\n")
    original = cfg.read_text()
    profile = compose(load_fragment("codex"), load_fragment("codex"))
    with pytest.raises(SystemExit):
        apply_to_config(profile, apply_changes=True, dry_run=False)
    assert cfg.read_text() == original
    assert not Path("spec-runner.config.yaml.bak").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_presets.py -k "apply_merges or malformed" -q`
Expected: FAIL — `_merge_into_existing` raises `NotImplementedError`

- [ ] **Step 3: Implement `_merge_into_existing`**

Replace the Task-4 stub with:

```python
import shutil


def _merge_into_existing(profile: dict, target_path: Path) -> Path:
    """Overwrite only the 7 CLI-profile keys, preserving shape and other keys."""
    raw = target_path.read_text()
    try:
        existing = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SystemExit(f"{target_path}: cannot parse YAML: {exc}") from exc
    if existing is None:
        existing = {}
    if not isinstance(existing, dict):
        raise SystemExit(f"{target_path}: expected a top-level YAML mapping")

    # Select target mapping; mirror load_config_from_yaml's flat/wrapped rule.
    target = existing["executor"] if "executor" in existing else existing
    if not isinstance(target, dict):
        raise SystemExit(f"{target_path}: 'executor' is not a mapping")

    backup = target_path.parent / (target_path.name + ".bak")
    shutil.copyfile(target_path, backup)
    target.update(profile)
    target_path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
    return target_path
```

Move the `import shutil` to the top-of-file import block (next to `import sys`)
rather than inline.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_presets.py -k "apply_merges or malformed" -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full preset test file**

Run: `uv run pytest tests/test_presets.py -q`
Expected: PASS (all tasks 1–5 tests)

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/preset_cmd.py tests/test_presets.py
git commit -m "feat(config): shape-preserving surgical merge with .bak backup"
```

---

## Task 6: `cmd_config` entry point + argparse wiring

**Files:**
- Modify: `src/spec_runner/preset_cmd.py` (add `cmd_config`)
- Modify: `src/spec_runner/cli.py` (import, subparser, dispatch)
- Test: `tests/test_presets.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_presets.py
from spec_runner.cli import _build_parser
from spec_runner.preset_cmd import cmd_config


def test_config_subcommand_parses_and_lists(capsys):
    parser = _build_parser()
    args = parser.parse_args(["config", "--list-presets"])
    assert args.command == "config"
    cmd_config(args, None)
    out = capsys.readouterr().out.split()
    assert out == ["claude", "codex", "opencode", "pi", "ollama", "llama-cli"]


def test_config_requires_a_cli_selection(capsys):
    parser = _build_parser()
    args = parser.parse_args(["config"])
    with pytest.raises(SystemExit) as exc:
        cmd_config(args, None)
    assert exc.value.code == 2


def test_config_preset_writes_mono(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "codex"])
    cmd_config(args, None)
    loaded = load_config_from_yaml(Path("spec-runner.config.yaml"))
    assert loaded["claude_command"] == "codex"
    assert loaded["review_command"] == "codex"


def test_config_copilot_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    args = parser.parse_args(["config", "--preset", "copilot"])
    with pytest.raises(SystemExit) as exc:
        cmd_config(args, None)
    assert exc.value.code == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_presets.py -k "config_" -q`
Expected: FAIL — `ImportError: cannot import name 'cmd_config'`

- [ ] **Step 3: Implement `cmd_config`**

```python
# append to src/spec_runner/preset_cmd.py
def cmd_config(args, config=None) -> None:
    """CLI entry for `spec-runner config`."""
    if args.list_presets:
        for name in list_presets():
            print(name)
        return

    exec_name = args.exec_cli or args.preset
    review_name = args.review_cli or args.preset
    if not exec_name or not review_name:
        print(
            "Specify --preset X (mono) or --exec X --review Y (multi). "
            "See --list-presets.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        exec_frag = load_fragment(exec_name)
        review_frag = load_fragment(review_name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    profile = compose(
        exec_frag,
        review_frag,
        model_override=args.model or "",
        review_model_override=args.review_model or "",
    )
    written = apply_to_config(profile, apply_changes=args.apply, dry_run=args.dry_run)
    if written is not None:
        for note in dict.fromkeys(n for n in (exec_frag.note, review_frag.note) if n):
            print(f"note: {note}")
        print(f"Wrote {written}. Run 'spec-runner doctor' to verify the CLI profile.")
```

- [ ] **Step 4: Wire the subparser into `cli.py`**

In `src/spec_runner/cli.py`, add the import near the other command imports
(after `from .cli_plan import cmd_plan`):

```python
from .preset_cmd import cmd_config  # noqa: E402, F401
```

After the `validate` subparser registration (`cli.py:906`), add:

```python
    config_parser = subparsers.add_parser(
        "config", parents=[common], help="Apply a CLI profile preset to config"
    )
    config_parser.add_argument("--preset", help="CLI for both exec and review (mono)")
    config_parser.add_argument(
        "--exec", dest="exec_cli", help="CLI for the exec/implementer stage"
    )
    config_parser.add_argument(
        "--review", dest="review_cli", help="CLI for the review stage"
    )
    config_parser.add_argument("--model", help="Model for both slots")
    config_parser.add_argument(
        "--review-model", dest="review_model", help="Model for the review slot only"
    )
    config_parser.add_argument(
        "--list-presets", action="store_true", help="List available presets"
    )
    config_parser.add_argument(
        "--dry-run", action="store_true", help="Print keys that would change; write nothing"
    )
    config_parser.add_argument(
        "--apply", action="store_true", help="Update the CLI profile in an existing config"
    )
```

In the `commands` dispatch dict inside `main()` (after `"doctor": cmd_doctor,`),
add:

```python
        "config": cmd_config,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_presets.py -k "config_" -q`
Expected: PASS (4 tests)

- [ ] **Step 6: Smoke-test the real CLI**

Run: `uv run spec-runner config --list-presets`
Expected: prints the six preset names, one per line.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/preset_cmd.py src/spec_runner/cli.py tests/test_presets.py
git commit -m "feat(config): cmd_config entry point + argparse wiring"
```

---

## Task 7: Validate flat configs (in-scope `validate.py` fix)

**Files:**
- Modify: `src/spec_runner/validate.py:224`
- Test: `tests/test_validate.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_validate.py
from spec_runner.validate import validate_config


def test_validate_config_flags_unknown_key_in_flat_config(tmp_path):
    cfg = tmp_path / "spec-runner.config.yaml"
    cfg.write_text("claude_command: codex\nnonsense_key: 1\n")
    result = validate_config(cfg)
    assert any("nonsense_key" in e for e in result.errors)


def test_validate_config_accepts_known_flat_keys(tmp_path):
    cfg = tmp_path / "spec-runner.config.yaml"
    cfg.write_text("claude_command: codex\nreview_command: claude\nbudget_usd: 5.0\n")
    result = validate_config(cfg)
    assert result.ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validate.py -k flat_config -q`
Expected: FAIL — `test_validate_config_flags_unknown_key_in_flat_config` finds no
error (flat configs are silently skipped today).

- [ ] **Step 3: Apply the fix**

In `src/spec_runner/validate.py`, change:

```python
    executor_section = data.get("executor")
    if not isinstance(executor_section, dict):
        return result
```

to:

```python
    # Canonical v2.0 is flat (no executor: wrapper); legacy uses the wrapper.
    executor_section = data["executor"] if "executor" in data else data
    if not isinstance(executor_section, dict):
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validate.py -k flat_config -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full validate suite (guard against regressions)**

Run: `uv run pytest tests/test_validate.py -q`
Expected: PASS (existing wrapped-config tests still green)

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/validate.py tests/test_validate.py
git commit -m "fix(validate): validate flat v2.0 config, not just executor: wrapper"
```

---

## Task 8: Documentation

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `CLAUDE.md` (CLI entry-points table)
- Modify: `README.md`

- [ ] **Step 1: Add the CHANGELOG entry**

Under `## [Unreleased]`, add an `### Added` section (create it if absent):

```markdown
### Added

- **`spec-runner config`** — apply a CLI profile preset to
  `spec-runner.config.yaml`. `--preset X` sets both the exec and review CLI
  (mono); `--exec X --review Y` mixes them (multi). Presets: claude, codex,
  opencode, pi, ollama, llama-cli. `--model` / `--review-model` override the
  model; `--list-presets` lists them; `--dry-run` previews; `--apply` updates an
  existing config (surgical merge of the 7 CLI-profile keys, other settings
  preserved, backed up to `.bak`). Note: on `--apply`, PyYAML normalises
  comments and key ordering.

### Fixed

- **`validate` now checks flat v2.0 configs**, not only `executor:`-wrapped
  ones, so unknown top-level keys in `spec-runner.config.yaml` are caught.
```

- [ ] **Step 2: Add `config` to the CLI table in `CLAUDE.md`**

In the "CLI entry points" code block, add after the `doctor` lines:

```bash
spec-runner config --preset codex          # Set exec+review CLI (mono)
spec-runner config --exec claude --review codex  # Mixed CLIs (multi)
spec-runner config --list-presets          # List available CLI presets
```

- [ ] **Step 3: Add a short README section**

In `README.md`, after the configuration/usage section, add:

```markdown
### Switching CLI (claude / codex / pi / ...)

Apply a preset instead of hand-editing `spec-runner.config.yaml`:

​```bash
spec-runner config --preset codex                 # everything on codex
spec-runner config --exec claude --review codex    # claude codes, codex reviews
spec-runner config --list-presets                  # claude codex opencode pi ollama llama-cli
spec-runner config --preset pi --apply             # update an existing config
​```

Tests stay on your `test_command` (e.g. pytest); presets only set the exec and
review CLIs. Run `spec-runner doctor` afterwards to verify the profile.
```

(Remove the zero-width spaces around the inner code fence when pasting.)

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md CLAUDE.md README.md
git commit -m "docs(config): document spec-runner config presets"
```

---

## Task 9: Full verification

- [ ] **Step 1: Run the whole non-slow suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: PASS (769 prior tests + new preset/validate tests)

- [ ] **Step 2: Lint and format**

Run: `uv run ruff format . && uv run ruff check . --fix`
Expected: clean (no remaining errors)

- [ ] **Step 3: Type-check**

Run: `uv run mypy src/spec_runner/preset_cmd.py`
Expected: no errors. (Fix annotations if any surface — e.g. `_print_profile`,
`apply_to_config` return type.)

- [ ] **Step 4: Verify the wheel ships the presets**

Run: `uv build && python -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]); print([n for n in z.namelist() if 'presets/' in n])"`
Expected: lists all six `spec_runner/presets/*.yaml` entries.

- [ ] **Step 5: Final commit (if lint/format changed anything)**

```bash
git add -A
git commit -m "chore(config): lint/format pass for config presets"
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** command surface (Task 6), fragment library (Task 1),
  composition + template clearing (Task 2), fresh flat write via static
  template (Task 3), dry-run + refuse (Task 4), shape-preserving merge + .bak +
  malformed-abort (Task 5), packaging (Task 1 step 5), importlib.resources
  loading (Task 1), flat validation (Task 7), docs + version note (Task 8). pi
  auto-detect / no custom template (Task 1–2). skip_permissions per fragment
  (Task 1). All spec sections map to a task.
- **Placeholders:** none — every code step shows complete code; the Task-4 stub
  is explicitly replaced in Task 5.
- **Type/name consistency:** `Fragment`, `load_fragment`, `list_presets`,
  `compose`, `apply_to_config(apply_changes=, dry_run=, config_path=)`,
  `_merge_into_existing`, `cmd_config(args, config=None)`, `PROFILE_KEYS`,
  `PRESET_NAMES` are used identically across tasks. Dispatch signature
  `cmd_func(args, config)` matches `cli.py:1116`.
