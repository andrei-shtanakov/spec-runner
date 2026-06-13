# Design: `spec-runner config` — CLI profile presets

**Date:** 2026-06-12
**Status:** Approved (brainstorming) — ready for implementation plan
**Target version:** 2.5.0 (minor — new feature, no contract/schema change)
**Revision:** 4 (adds qwen + copilot presets in v2.6.0 — see the "Revision 4"
section at the end; re-introduces per-fragment templates for non-auto-detected
CLIs). Revisions 1–3 shipped as v2.5.0.

## Problem

Switching spec-runner between coding CLIs (claude / codex / pi / opencode /
ollama / llama) means hand-editing `spec-runner.config.yaml` and knowing the
non-obvious nested structure (`hooks.*`, `commands.*`) plus each CLI's
invocation quirks. There is no command to apply a known-good profile.

The runtime already supports **two independently configurable CLIs**:

- **exec / implementer stage:** `claude_command`, `claude_model`,
  `command_template`, `skip_permissions`
- **review stage:** `review_command`, `review_model`, `review_command_template`
  (`review.py:224-226`)

Both **mono** pipelines (everything on one CLI) and **multi** pipelines
(e.g. claude codes, codex reviews) are achievable today via manual YAML
surgery. This feature packages that as presets.

Out of scope: a third LLM stage for tests. Tests remain a shell command
(`test_command`, e.g. `pytest`); the parallel review roles
(`quality`/`implementation`/`testing`) are review *lenses* through the single
`review_command`, not separate CLIs.

## Goals

- One command to write/update the CLI profile in `spec-runner.config.yaml`.
- Cover mono (`--preset X`) and multi (`--exec X --review Y`) from a small
  per-CLI fragment library — no N×N preset files.
- Preserve the user's other settings (budgets, notifications, personas) when a
  config already exists.
- Never emit or modify secrets.

## Non-goals (v1)

- `copilot` CLI — not auto-detected by `runner.build_cli_invocation`; would need
  a hand-written `command_template`. Deferred.
- Per-CLI **tool restriction** (e.g. read-only `--tools read,grep,find` for the
  review slot) — would require non-empty templates and is inconsistent with the
  other CLIs (whose reviewers are not tool-restricted either). Deferred as a
  future per-CLI capability.
- Comment/format preservation on merge (PyYAML normalizes). Acceptable for v1;
  ruamel.yaml is a future upgrade if needed.

## Command surface

New subcommand on the main CLI (`spec-runner`), distinct from the existing
`spec-runner-init` binary (which installs skills):

```bash
spec-runner config --preset codex                   # mono: exec+review = codex
spec-runner config --exec claude --review codex      # multi
spec-runner config --exec pi --review claude          # multi
spec-runner config --preset codex --model o3           # override model (both slots)
spec-runner config --exec pi --review-model gpt-5      # per-slot model override
spec-runner config --list-presets                    # list available fragments
spec-runner config --preset codex --dry-run           # print the 7 keys, write nothing
spec-runner config --preset codex --apply             # update CLI profile in existing config
```

- `--preset X` is sugar for `--exec X --review X`.
- `--model M` sets both slots' model; `--review-model M` overrides review only.
- Invoking with none of `--preset` / `--exec` / `--review` / `--list-presets`
  is an error with a usage hint.
- Unknown CLI name → error listing valid presets.
- `copilot` → explicit error: "not supported in v1 (no auto-detect); set
  `command_template` manually."

## Fragment library (composition engine)

`src/spec_runner/presets/<cli>.yaml`, one per supported CLI: `claude`, `codex`,
`opencode`, `pi`, `ollama`, `llama-cli`. A fragment is **slot-neutral** — it
describes how to invoke that CLI; the composer places it into the exec or
review slot.

The preset is named **`llama-cli`** (not `llama`), and its `command` is
`llama-cli`: `build_cli_invocation` only auto-detects `llama-cli` / `llama.cpp`
/ `llama-server` (`runner.py:296-308`); a bare `llama` falls through to the
claude branch and would be invoked wrongly.

Fragment schema (v1 — minimal, all CLIs rely on auto-detect):

```yaml
command: llama-cli           # CLI binary (must match an auto-detect name)
model: ""                    # default model ("" = CLI/runtime default)
skip_permissions: false      # see per-fragment values below
note: ""                     # optional, printed after writing
```

- **No `command_template` field in v1.** Every supported CLI is handled by
  `build_cli_invocation` auto-detect (`runner.py:296-356`), including pi, which
  already adds `--model` only when a model is set (`runner.py:333-342`) — so the
  "blank model breaks pi" trap does not apply. The composer therefore writes
  `command_template: ""` / `review_command_template: ""` to **clear** any stale
  template from a previously configured CLI.
- **`skip_permissions` is per-fragment.** `claude.yaml` sets `true` — it maps to
  claude's `--dangerously-skip-permissions` (`runner.py:347`) and the config
  default is already `True` (`config.py:138`), so a `claude` preset must
  preserve it or it silently regresses to permission prompts. All other
  fragments set `false` (the flag is claude-only and ignored elsewhere).
- `ollama` fragment carries a `note`: a blank `model` makes the runtime default
  to `llama3` (`runner.py:312`); set `claude_model` if a different model is
  wanted.
- `pi` fragment carries a `note`: set `claude_model` to a model your pi install
  is authenticated for (`pi --list-models`).

Fragments are loaded via `importlib.resources.files("spec_runner") /
"presets" / f"{name}.yaml"` (robust for installed wheels), not
`Path(__file__)`.

## Composition → config keys

The composer always emits these **7 CLI-profile keys** (stale templates cleared
to `""`):

| Source (exec slot E) | → config key |
|---|---|
| `E.command` | `claude_command` |
| `E.model` (or `--model`) | `claude_model` |
| `""` (auto-detect) | `command_template` |
| `E.skip_permissions` | `skip_permissions` |

| Source (review slot R) | → config key |
|---|---|
| `R.command` | `review_command` |
| `R.model` (or `--review-model`/`--model`) | `review_model` |
| `""` (auto-detect) | `review_command_template` |

All 7 are **top-level keys of the executor mapping** — none live under
`hooks` / `commands` / `paths`. Every other key is untouched.

## Canonical output shape

`load_config_from_yaml` treats the root `spec-runner.config.yaml` flat form
(no `executor:` wrapper) as **v2.0 canonical**, and the `executor:`-wrapped
form as legacy. Therefore:

- **Fresh write** → **flat** v2.0: the 7 keys plus a commented scaffold of
  common knobs (budgets, notifications) at the document top level. No
  `executor:` wrapper is introduced. Because PyYAML `safe_dump` cannot emit
  comments, the fresh file is produced from a **static text template** with the
  7 key values interpolated — `safe_dump` is used only for the merge path.
- **Existing file** → **preserve its shape**: if the document has an `executor:`
  key, update the 7 keys inside that mapping; otherwise update them at the top
  level. A flat config never gains an `executor:` wrapper; a wrapped config
  keeps its wrapper.

## Merge semantics

1. **No config file** (root and legacy both absent) → write a fresh flat
   `spec-runner.config.yaml` (see Canonical output shape).
2. **`--dry-run`** (any case) → print the 7 keys that would be written; write
   nothing; exit 0.
3. **Config exists, no `--apply`** → refuse: print the 7 keys that *would*
   change and the message `"spec-runner.config.yaml exists. Re-run with --apply
   to update the CLI profile (other settings preserved), or --dry-run to
   preview."`; exit 1; file unchanged.
4. **Config exists, `--apply`** → surgical merge:
   - back up current file to `spec-runner.config.yaml.bak`
   - `yaml.safe_load` the existing document — **`cmd_config` reads the file
     itself with raw `safe_load`**, not via `load_config_from_yaml` (which
     swallows parse errors and returns `{}`, `config.py`). A `yaml.YAMLError`
     here aborts before any write (see Error handling).
   - select the target mapping: `data["executor"]` if an `executor` key exists,
     else `data` (mirrors `load_config_from_yaml`)
   - overwrite **only** the 7 CLI-profile keys in that mapping; leave everything
     else (budgets, notifications, personas, hooks, commands, paths, and any
     secrets) intact as opaque values
   - `yaml.safe_dump` back, preserving the document's flat/wrapped shape
   - ⚠️ Caveat: comments and key ordering are normalized by PyYAML.

**Secrets** (`telegram_bot_token`, `webhook_headers`, etc.) are never generated,
logged, printed, or modified. On `--apply` they are preserved as opaque YAML
values carried through `safe_load`/`safe_dump`.

## Behavior details

- After a successful write, print the resolved profile (exec CLI/model, review
  CLI/model) and: `Run 'spec-runner doctor' to verify the CLI profile.`
- Legacy config location (`spec/executor.config.yaml`): if only the legacy file
  exists, `--apply` updates it in place (the normal load path still emits its
  deprecation warning); a fresh write always targets the v2.0 root location.

## Components

- `src/spec_runner/presets/*.yaml` — 6 fragment files (data only).
- `src/spec_runner/preset_cmd.py`:
  - `Fragment` — frozen dataclass (`command`, `model`, `skip_permissions`,
    `note`).
  - `load_fragment(name) -> Fragment` — `importlib.resources` read; raises on
    unknown / `copilot`.
  - `list_presets() -> list[str]`.
  - `compose(exec_frag, review_frag, model_override, review_model_override) ->
    dict` — returns the 7 CLI-profile keys.
  - `apply_to_config(profile, *, apply_changes, dry_run, config_path) ->
    Path | None` — fresh-write (static template) / refuse / surgical-merge
    (`safe_load` + `safe_dump`) logic. Reads YAML with raw `safe_load` so
    malformed input raises rather than being swallowed.
  - `cmd_config(args) -> int` — CLI entry: parse, compose, apply, print.
- `src/spec_runner/cli.py` `_build_parser()` — register the `config` subparser
  and dispatch to `cmd_config`.
- `src/spec_runner/validate.py` `validate_config()` — small in-scope fix: select
  the target mapping as `data["executor"] if "executor" in data else data`, so
  the now-canonical **flat** v2.0 config is validated for unknown keys instead of
  being silently skipped (`validate.py:224-225`).

## Data flow

```
args ──> load_fragment(exec) ─┐
     ──> load_fragment(review)┼─> compose() ─> profile dict (7 keys)
                              ┘                      │
                                                     v
                       apply_to_config(apply, dry_run) ─> write / refuse / dry-run
                                                     │
                                                     v
                                  print profile summary + doctor hint
```

## Error handling

- Unknown preset / CLI name → `SystemExit(2)` with valid names listed.
- `copilot` → `SystemExit(2)` with the manual-template message.
- Config exists without `--apply` (and not `--dry-run`) → `SystemExit(1)` with
  the re-run hint; file unchanged.
- Malformed existing YAML on `--apply` → abort without writing; surface the
  parse error (raw `safe_load` in `cmd_config`, not the error-swallowing
  `load_config_from_yaml`).
- Bundled fragment file missing/corrupt → `SystemExit(2)` (packaging error).

## Packaging

- `pyproject.toml` `[tool.setuptools.package-data]`: add `"presets/*.yaml"`
  alongside the existing `"skills/**/*"`, so fragments ship in the wheel.

## Testing (`tests/test_presets.py`)

- `load_fragment` returns expected fields for each of the 6 CLIs, via
  `importlib.resources` (so the test exercises installed-resource loading, not
  `Path(__file__)`).
- `compose` mono (exec==review) and multi produce the correct 7 keys, with
  `command_template`/`review_command_template` cleared to `""`.
- `--model` / `--review-model` overrides land in the right slot.
- Fresh write: flat file created (no `executor:` wrapper);
  `load_config_from_yaml` round-trips the profile.
- `--apply` merge: pre-existing `budget_usd` / `telegram_bot_token` /
  `personas` survive unchanged; CLI keys updated; `.bak` created; flat stays
  flat and wrapped stays wrapped (two cases).
- Refuse-without-`--apply` path returns exit 1 and leaves the file byte-identical.
- `--dry-run` writes nothing and prints the 7 keys.
- Unknown preset and `copilot` raise the documented errors.
- `--list-presets` lists all 6 and not `copilot`.
- **CLI dispatch:** `spec-runner config --list-presets` is reachable through the
  argparse parser built by `_build_parser()` and routes to `cmd_config`.
- **Malformed YAML on `--apply`** raises/aborts and leaves the file unchanged.
- `claude` preset writes `skip_permissions: true`; a non-claude preset writes
  `false`.
- `llama-cli` preset's `command` is `llama-cli` (round-trips through
  `build_cli_invocation` to the llama branch, not the claude fallback).
- **`validate_config` on a flat config** flags an unknown top-level key
  (regression test for the in-scope `validate.py` fix).

Mock nothing external — pure file/YAML; fast, no `@pytest.mark.slow`.

## Documentation

- README + CLAUDE.md CLI-entry-points table: add the `config` subcommand.
- CHANGELOG `[Unreleased]` → Added, with the preset list and merge caveat.
- Version bump to 2.5.0 at release time.

## Open questions

None blocking. (ruamel-based comment preservation and per-CLI tool restriction
are deliberate future upgrades, not v1 requirements.)

---

# Revision 4 — `qwen` + `copilot` presets (v2.6.0)

**Date:** 2026-06-13
**Target version:** 2.6.0 (minor — adds two presets + a fragment-schema field)

## Goal

Add `qwen` (Qwen Code CLI) and `copilot` (GitHub Copilot CLI) as presets. Both
were deferred in v1 because neither is auto-detected by
`runner.build_cli_invocation` — they need an explicit `command_template`. This
revision re-introduces per-fragment template fields (removed in Revision 2 when
all presets were auto-detect-only) and ships the two fragments.

Also: document the already-working Qwen paths (no code) — Qwen in the cloud via
the `opencode` preset, and locally via the `ollama` preset.

## Fragment schema change

The fragment gains two optional fields:

```yaml
command: qwen
model: ""
skip_permissions: false
exec_template: ""      # NEW — → command_template (empty = rely on auto-detect)
review_template: ""    # NEW — → review_command_template
note: ""
```

- The six auto-detect presets (claude/codex/opencode/pi/ollama/llama-cli) leave
  both template fields empty → composer continues to emit `command_template: ""`
  / `review_command_template: ""` (auto-detect path, unchanged behavior).
- Only `qwen` and `copilot` set non-empty templates.

## Composer change

`compose` no longer hard-codes the two template keys to `""`. Instead:

- `command_template` = `exec_frag.exec_template` (default `""`)
- `review_command_template` = `review_frag.review_template` (default `""`)

This keeps the stale-template-clearing property (auto-detect fragments emit `""`,
overwriting any previous CLI's template) while letting qwen/copilot inject their
required invocation. The 7-key output shape is unchanged.

## New fragments

**`presets/qwen.yaml`** (Qwen Code CLI — `qwen -p` headless; verified against
qwen-code docs):

```yaml
command: qwen
model: ""
skip_permissions: false
exec_template: "{cmd} -p {prompt} --approval-mode yolo"
review_template: "{cmd} -p {prompt} --approval-mode plan"
note: "Qwen Code: set the model in ~/.qwen/settings.json (modelProviders). yolo is required for headless edits; plan = read-only review."
```

**`presets/copilot.yaml`** (GitHub Copilot CLI — `copilot -p` programmatic;
verified against github/docs copilot-cli reference):

```yaml
command: copilot
model: ""
skip_permissions: false
exec_template: "{cmd} -p {prompt} -s --no-ask-user --allow-all-tools"
review_template: "{cmd} -p {prompt} -s --no-ask-user --allow-tool='shell'"
note: "GitHub Copilot CLI: needs Copilot access (gh auth / COPILOT_GITHUB_TOKEN). Set the model via COPILOT_MODEL env or add --model <id> to command_template. -s gives clean output so the TASK_COMPLETE marker is parseable."
```

Rationale for the flags:
- qwen headless with default approval *fails* on file edits → `--approval-mode
  yolo` for exec; `--approval-mode plan` (analysis only) for the read-only review
  gate.
- copilot headless needs an approval bypass or tool calls block → `--allow-all-tools`
  for exec; `--allow-tool='shell'` (read via shell, no `write`) for review.
  `-s` (silent) strips session metadata so `TASK_COMPLETE` is visible in stdout;
  `--no-ask-user` stops it pausing for clarifying questions.

## Deliberate trade-off: model-less templates

The qwen/copilot templates omit `{model}`. A non-empty `{model}` placeholder with
an empty model produces a broken `--model ` argument (the same trap pi avoids).
Consequence: `spec-runner config --preset copilot --model X` sets `claude_model`
in the config but the template ignores it for these two CLIs — the model is set
in the CLI's own settings/env (documented in each fragment's `note`). A
model-aware composer (append `--model` only when a model is given) is a deliberate
follow-up, not part of this revision.

## Other changes

- `load_fragment` no longer special-cases `copilot` as rejected; `copilot` is now
  a valid preset. The unknown-preset path still rejects truly unknown names.
- `PRESET_NAMES` → `[claude, codex, opencode, pi, ollama, llama-cli, qwen, copilot]`
  (8). `--list-presets` lists all 8.
- README: add a "Qwen — cheap in the cloud (OpenCode) and local (Ollama)" section
  with the `opencode`/`ollama` preset examples, and mention the new `qwen`/`copilot`
  presets. CHANGELOG `[Unreleased]` → Added.

## Testing (extends `tests/test_presets.py`)

- `load_fragment("qwen")` / `load_fragment("copilot")` return fragments with the
  expected non-empty `exec_template` / `review_template`.
- `copilot` is no longer rejected (the old `test_..._copilot...` reject test is
  updated/removed; a new test asserts copilot loads).
- `compose` with a templated exec fragment puts the template in `command_template`;
  with a templated review fragment puts it in `review_command_template`; mixing a
  templated CLI with an auto-detect CLI leaves the other slot's template `""`
  (e.g. `--exec copilot --review claude` → `command_template` = copilot's,
  `review_command_template` = `""`).
- The six auto-detect presets still produce empty templates (regression guard).
- `--list-presets` returns all 8 including qwen and copilot.
- End-to-end through `_build_parser()`: `config --preset qwen` writes a config
  whose `command_template` is the qwen exec template.

## Out of scope (still)

- A model-aware composer (`--model` wired into qwen/copilot templates).
- Auto-detection of qwen/copilot in `runner.build_cli_invocation` (templates are
  the supported mechanism; auto-detect would be a separate change).
