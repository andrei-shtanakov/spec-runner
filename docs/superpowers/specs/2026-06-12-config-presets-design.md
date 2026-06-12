# Design: `spec-runner config` — CLI profile presets

**Date:** 2026-06-12
**Status:** Approved (brainstorming) — ready for implementation plan
**Target version:** 2.5.0 (minor — new feature, no contract/schema change)

## Problem

Switching spec-runner between coding CLIs (claude / codex / pi / opencode /
ollama / llama) means hand-editing `spec-runner.config.yaml` and knowing the
non-obvious nested structure (`executor.hooks.*`, `executor.commands.*`) plus
each CLI's invocation quirks (codex uses `exec` not `-p`; pi needs a
`command_template`). There is no command to apply a known-good profile.

The runtime already supports **two independently configurable CLIs**:

- **exec / implementer stage:** `claude_command`, `claude_model`,
  `command_template`, `skip_permissions`
- **review stage:** `review_command`, `review_model`, `review_command_template`
  (`review.py:224-226`)

So both **mono** pipelines (everything on one CLI) and **multi** pipelines
(e.g. claude codes, codex reviews) are achievable today — they just require
manual YAML surgery. This feature packages that as presets.

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
- Never write secrets.

## Non-goals (v1)

- `copilot` CLI — not auto-detected by `runner.build_cli_invocation`; would need
  a hand-written `command_template`. Deferred.
- Comment/format preservation on merge (PyYAML normalizes). Acceptable for v1;
  ruamel.yaml is a future upgrade if needed.
- A new test/QA LLM stage with its own CLI.

## Command surface

New subcommand on the main CLI (`spec-runner`), distinct from the existing
`spec-runner-init` binary (which installs skills):

```bash
spec-runner config --preset codex                  # mono: exec+review = codex
spec-runner config --exec claude --review codex     # multi
spec-runner config --exec pi --review claude         # multi
spec-runner config --preset codex --model o3          # override model (both slots)
spec-runner config --exec pi --review-model ...       # per-slot model override
spec-runner config --list-presets                   # list available fragments
spec-runner config --preset codex --force           # overwrite CLI profile in existing config
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
`opencode`, `pi`, `ollama`, `llama`. A fragment is **slot-neutral** — it
describes how to invoke that CLI, and the composer places it into the exec or
review slot.

Fragment schema:

```yaml
command: pi                  # CLI binary
model: ""                    # default model ("" = CLI default)
exec_template: "{cmd} -p --tools read,bash,edit,write,grep,find,ls {prompt}"
review_template: "{cmd} -p --tools read,grep,find {prompt}"   # read-only
skip_permissions: false
note: "Set claude_model to a model your pi install is authenticated for (pi --list-models)."
```

- Auto-detected CLIs (claude, codex, opencode, ollama, llama): `exec_template`
  and `review_template` are **empty** — `build_cli_invocation` auto-detect
  handles the argv. Only `pi` carries non-empty templates.
- `pi` templates deliberately omit `--model {model}` (a blank model + bare
  `--model` breaks pi) and omit `--skill .pi/...` (those skills are not
  guaranteed to exist in the user's project — portability).
- `note` (optional) is printed after writing.

## Composition → config keys

| Fragment field (exec slot E) | → config key |
|---|---|
| `E.command` | `claude_command` |
| `E.model` (or `--model`) | `claude_model` |
| `E.exec_template` | `command_template` |
| `E.skip_permissions` | `skip_permissions` |

| Fragment field (review slot R) | → config key |
|---|---|
| `R.command` | `review_command` |
| `R.model` (or `--review-model`/`--model`) | `review_model` |
| `R.review_template` | `review_command_template` |

These are the **CLI profile keys**. All other config keys are untouched.

## Merge semantics

1. **No config file** (`spec-runner.config.yaml` and legacy both absent) →
   write a fresh `spec-runner.config.yaml`:
   - `executor:` with the composed CLI profile keys
   - a commented scaffold of common knobs (budgets, notifications) as inert
     examples to guide the user
2. **Config exists, no `--force`** → refuse:
   `"spec-runner.config.yaml exists. Re-run with --force to overwrite the CLI
   profile (other settings preserved), or edit manually."`
3. **Config exists, `--force`** → surgical merge:
   - back up current file to `spec-runner.config.yaml.bak`
   - `yaml.safe_load` the existing document
   - locate the `executor:` mapping (flat-format `data` if no `executor` key,
     matching `load_config_from_yaml`'s `data.get("executor", data)`)
   - overwrite **only** the seven CLI-profile keys; leave everything else
     (budgets, notifications, personas, hooks, commands, paths) intact as data
   - `yaml.safe_dump` back
   - ⚠️ Documented caveat: comments and key ordering are normalized by PyYAML.

Secrets (`telegram_bot_token`, `webhook_*`, etc.) are never read or written.

## Behavior details

- After a successful write, print the resolved profile (exec CLI/model, review
  CLI/model) and: `Run 'spec-runner doctor' to verify the CLI profile.`
- Legacy config location (`spec/executor.config.yaml`): if only the legacy file
  exists, `--force` updates it in place (and emits the existing deprecation
  warning via the normal load path); a fresh write always targets the v2.0
  root location.

## Components

- `src/spec_runner/presets/*.yaml` — 6 fragment files (data only).
- `src/spec_runner/preset_cmd.py`:
  - `load_fragment(name) -> Fragment` (reads bundled YAML; raises on unknown /
    copilot)
  - `list_presets() -> list[str]`
  - `compose(exec_frag, review_frag, model_override, review_model_override) ->
    dict` (returns the 7 CLI-profile keys)
  - `apply_to_config(profile, *, force, config_path) -> Path` (the merge logic)
  - `cmd_config(args) -> int` (CLI entry: parses, composes, applies, prints)
- `src/spec_runner/cli.py` `_build_parser()` — register the `config` subparser
  and dispatch to `cmd_config`.

`Fragment` is a small frozen dataclass mirroring the YAML schema.

## Data flow

```
args ──> load_fragment(exec) ─┐
     ──> load_fragment(review)┼─> compose() ─> profile dict (7 keys)
                              ┘                      │
                                                     v
                          apply_to_config(force) ─> write spec-runner.config.yaml
                                                     │
                                                     v
                                  print profile summary + doctor hint
```

## Error handling

- Unknown preset / CLI name → `SystemExit(2)` with valid names listed.
- `copilot` → `SystemExit(2)` with the manual-template message.
- Config exists without `--force` → `SystemExit(1)` with the re-run hint.
- Malformed existing YAML on `--force` → abort without writing, keep `.bak`
  untouched, surface the parse error.
- Bundled fragment file missing/corrupt → `SystemExit(2)` (packaging error).

## Testing (`tests/test_presets.py`)

- `load_fragment` returns expected fields for each of the 6 CLIs.
- `compose` mono (exec==review) and multi produce the correct 7 keys.
- `--model` / `--review-model` overrides land in the right slot.
- Fresh write: file created, `load_config_from_yaml` round-trips the profile.
- `--force` merge: pre-existing `budget_usd` / `telegram_*` / `personas`
  survive; CLI keys updated; `.bak` created.
- Refuse-without-force path returns the right exit code and leaves file
  unchanged.
- Unknown preset and `copilot` raise the documented errors.
- `--list-presets` lists all 6 and not `copilot`.
- pi fragment: `command_template` has no `--model` and no `--skill`.

Mock nothing external — pure file/YAML; fast, no `@pytest.mark.slow`.

## Documentation

- README + CLAUDE.md CLI-entry-points table: add the `config` subcommand.
- CHANGELOG `[Unreleased]` → Added, with the preset list and merge caveat.
- Version bump to 2.5.0 at release time.

## Open questions

None blocking. (ruamel-based comment preservation is a deliberate future
upgrade, not a v1 requirement.)
