# Profile-Aware Spec Surface Implementation Plan (PR 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every config-aware spec call site resolve stages from the configured profile, closing a governance-gate bypass and a `plan --gated` crash that both affect any non-`lite` profile.

**Architecture:** `read_spec_meta(path, stages)` returns `None` (= unmanaged) when `spec_stage` is not in `stages`, and `None` passes the governance gate. Seven call sites omit `stages` and silently fall back to the `lite` tuple, so under a custom profile a managed draft reads as unmanaged and runs under `--strict`. Separately, `cli_plan._MARKER`/`_UPSTREAM` are hardcoded `lite` dicts that `KeyError` on a custom stage. Both are fixed by resolving from `config.resolve_spec_profile()` / `StageDef`, which already carry the needed data.

**Tech Stack:** Python 3.10+, pytest, ruff (line length 100), mypy strict, uv.

## Global Constraints

- Ruff line length **100** (not 88); rules E, F, W, I, UP, B, C4, SIM with E501 ignored.
- Type annotations required everywhere; mypy strict must stay clean.
- Run `uv run pytest tests/ -v -m "not slow"` — the baseline is **1129 passed**.
- The Maestro interop contract (`.executor-state.db` schema, `--json-result`) must not change.
- No change to `SpecMeta`'s shape in this PR — that is PR 2.
- Regression gate: existing tests stay green; a test may only have its expectation changed when it explicitly asserts the old fail-open behaviour, and then only together with a new regression test and a written rationale in the commit message.
- Commit style: `<type>(<scope>): <subject>`, ending with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Branch `fix/profile-aware-spec-surface`; direct commits to `master` are forbidden; the user merges the PR.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/spec_runner/cli.py` | `spec_run_gate_ok` — the governance gate | Pass profile stage names |
| `src/spec_runner/spec_commands.py` | `spec approve/reject/check` handlers | Pass profile stage names (4 calls) |
| `src/spec_runner/cli_plan.py` | Gated generation | Replace `_MARKER`/`_UPSTREAM` with profile lookups; pass stage names (2 calls) |
| `tests/test_run_gate.py` | Gate behaviour | Add custom-profile regression test; extend the config double |
| `tests/test_spec_commands.py` | `spec` family | Add custom-profile tests |
| `tests/test_gated_plan.py` | Gated generation | Add custom-profile test |
| `tests/fixtures/profiles/acceptance.yaml` | A non-`lite` profile for tests | Create |
| `CHANGELOG.md` | Release notes | Add `[Unreleased] / Fixed` entries |

---

### Task 1: Test fixture — a non-`lite` stage profile

**Files:**
- Create: `tests/fixtures/profiles/acceptance.yaml`
- Test: `tests/test_stage_profile.py`

**Interfaces:**
- Consumes: `spec_runner.spec.load_profile`, `StageProfile`
- Produces: a YAML profile file whose stages are `requirements → design → acceptance`, used by every later task in this plan. The stage name `acceptance` is deliberately absent from `lite`, which is what makes the bugs observable.

- [ ] **Step 1: Read the bundled profile to copy its exact schema**

Run: `cat src/spec_runner/profiles/lite.yaml`

Match the key names exactly (`name`, `template`, `marker_prefix`, `validator_key`, `requires`/`upstream`, `prompt_text`). Do not invent keys — `load_profile` rejects unknown `requires` refs and cycles.

- [ ] **Step 2: Write the fixture**

Create `tests/fixtures/profiles/acceptance.yaml` with three stages. Reuse `lite`'s template filenames and validator keys for `requirements` and `design`; for `acceptance` reuse the `tasks` template and validator key so no new bundled template is needed:

```yaml
name: acceptance
stages:
  - name: requirements
    template: requirements.md
    marker_prefix: REQUIREMENTS
    validator_key: requirements
  - name: design
    template: design.md
    marker_prefix: DESIGN
    validator_key: design
    requires: [requirements]
  - name: acceptance
    template: tasks.md
    marker_prefix: TASKS
    validator_key: tasks
    requires: [requirements, design]
```

Adjust the `template:` values to the real filenames printed in Step 1 if they differ.

- [ ] **Step 3: Write a test that the fixture loads**

Add to `tests/test_stage_profile.py`:

```python
def test_acceptance_fixture_profile_loads(tmp_path):
    """The test fixture profile is a valid, non-lite stage chain."""
    from pathlib import Path

    from spec_runner.spec import load_profile

    fixture = Path(__file__).parent / "fixtures" / "profiles" / "acceptance.yaml"
    profile = load_profile(fixture)
    assert profile.names() == ("requirements", "design", "acceptance")
    assert "acceptance" not in ("requirements", "design", "tasks")
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_stage_profile.py::test_acceptance_fixture_profile_loads -v`
Expected: PASS.

If `load_profile` takes a profile *name* rather than a path, read its signature (`grep -n "def load_profile" -A 20 src/spec_runner/spec.py`) and adapt: either point it at the fixture directory or place the fixture where `load_profile` looks. Do not change `load_profile` itself in this task.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/profiles/acceptance.yaml tests/test_stage_profile.py
git commit -m "test: add a non-lite stage profile fixture

The 'acceptance' stage is absent from lite, which is what makes the
profile-blind call sites observable in the tests that follow.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Close the governance-gate bypass

**Files:**
- Modify: `src/spec_runner/cli.py:203` (`spec_run_gate_ok`)
- Test: `tests/test_run_gate.py`

**Interfaces:**
- Consumes: the fixture profile from Task 1; `ExecutorConfig.resolve_spec_profile()` returning a `StageProfile` with `.names() -> tuple[str, ...]`.
- Produces: `spec_run_gate_ok(config) -> tuple[bool, str]` — signature unchanged.

**Note on the existing test double:** `tests/test_run_gate.py` builds config as a `SimpleNamespace` with only `spec_governance` and `tasks_file`. Adding a `resolve_spec_profile()` call means the double needs that method. Extending a test double is not rewriting an assertion — the existing gate assertions stay exactly as they are.

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_run_gate.py`:

```python
def test_gate_strict_blocks_draft_under_custom_profile(tmp_path: Path):
    """A managed draft on a non-lite stage must not read as unmanaged (bypass)."""
    from pathlib import Path as _Path

    from spec_runner.spec import load_profile

    fixture = _Path(__file__).parent / "fixtures" / "profiles" / "acceptance.yaml"
    profile = load_profile(fixture)

    cfg = _cfg(tmp_path, "strict")
    cfg.resolve_spec_profile = lambda: profile
    write_spec(cfg.tasks_file, SpecMeta("acceptance", "draft"), "x\n")

    ok, reason = spec_run_gate_ok(cfg)
    assert not ok, "draft on a custom-profile stage bypassed the governance gate"
    assert "draft" in reason.lower()
```

- [ ] **Step 2: Extend the shared config double**

In the same file, change `_cfg` so every test carries a profile resolver defaulting to `lite`:

```python
def _cfg(tmp_path, governance):
    from spec_runner.spec import LITE

    spec = tmp_path / "spec"
    return SimpleNamespace(
        spec_governance=governance,
        tasks_file=spec / "tasks.md",
        resolve_spec_profile=lambda: LITE,
    )
```

- [ ] **Step 3: Run the new test to verify it fails**

Run: `uv run pytest tests/test_run_gate.py::test_gate_strict_blocks_draft_under_custom_profile -v`
Expected: FAIL — the assertion `not ok` fails, because `read_spec_meta` returns `None` for the `acceptance` stage under the default `lite` tuple and the gate allows the run.

- [ ] **Step 4: Fix the call site**

In `src/spec_runner/cli.py`, inside `spec_run_gate_ok`, replace:

```python
    meta = read_spec_meta(config.tasks_file)
```

with:

```python
    meta = read_spec_meta(config.tasks_file, config.resolve_spec_profile().names())
```

- [ ] **Step 5: Run the gate tests**

Run: `uv run pytest tests/test_run_gate.py -v`
Expected: PASS, including the four pre-existing gate tests whose assertions are untouched.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: 1130 passed (1129 baseline + 1 new).

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/cli.py tests/test_run_gate.py
git commit -m "fix(spec): close governance-gate bypass under a custom stage profile

spec_run_gate_ok read tasks.md with read_spec_meta's default lite stage
tuple, so a managed draft whose stage is not in lite resolved to None
(= unmanaged) and passed --strict. Resolve stages from the configured
profile instead.

The test double in test_run_gate.py gains a resolve_spec_profile stub;
no existing assertion changed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Profile-aware `spec approve` / `reject` / `check`

**Files:**
- Modify: `src/spec_runner/spec_commands.py:77`, `:89` (`cmd_spec_approve`), `:99` (`cmd_spec_reject`), `:151` (`cmd_spec_check`)
- Test: `tests/test_spec_commands.py`

**Interfaces:**
- Consumes: `config.resolve_spec_profile().names()`; the fixture profile from Task 1.
- Produces: no signature changes — `cmd_spec_approve/reject/check(args, config) -> int` keep returning `0` success, `1` validation failure, `2` unmanaged.

- [ ] **Step 1: Add a private helper next to `_metas`**

In `src/spec_runner/spec_commands.py`, directly below `_metas`, add:

```python
def _stage_names(config: ExecutorConfig) -> tuple[str, ...]:
    """Stage names of the configured profile, for profile-aware meta reads."""
    return config.resolve_spec_profile().names()
```

- [ ] **Step 2: Write the failing test for `reject`**

`reject` is the cheapest of the three to exercise (no validator run). Add to `tests/test_spec_commands.py`:

```python
def test_spec_reject_sees_custom_profile_stage(tmp_path, monkeypatch, capsys):
    """A managed custom-profile stage must not be reported as unmanaged."""
    from argparse import Namespace
    from pathlib import Path

    from spec_runner.spec import SpecMeta, load_profile, write_spec
    from spec_runner.spec_commands import cmd_spec_reject

    fixture = Path(__file__).parent / "fixtures" / "profiles" / "acceptance.yaml"
    profile = load_profile(fixture)

    config = _config(tmp_path)          # existing helper in this file
    config.spec_profile = "acceptance"
    monkeypatch.setattr(type(config), "resolve_spec_profile", lambda self: profile)

    path = stage_path(config, "acceptance")
    write_spec(path, SpecMeta("acceptance", "approved"), "body\n")

    rc = cmd_spec_reject(Namespace(stage="acceptance"), config)
    out = capsys.readouterr().out

    assert rc == 0, f"custom-profile stage reported unmanaged: {out}"
    assert "re-opened as draft" in out
```

Read the top of `tests/test_spec_commands.py` first and reuse its existing config helper and imports rather than inventing new ones; the helper name above (`_config`) must be replaced with whatever that file actually defines.

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_spec_commands.py::test_spec_reject_sees_custom_profile_stage -v`
Expected: FAIL with `rc == 2` and output `acceptance: unmanaged`.

- [ ] **Step 4: Fix all four call sites**

In `src/spec_runner/spec_commands.py` replace each bare read:

```python
    meta = read_spec_meta(path)
```

with:

```python
    meta = read_spec_meta(path, _stage_names(config))
```

at lines 77 (`cmd_spec_approve`), 99 (`cmd_spec_reject`) and 151 (`cmd_spec_check`), and the post-approval re-read at line 89:

```python
    new_meta = read_spec_meta(path, _stage_names(config))
```

Verify none remain: `grep -n "read_spec_meta(path)" src/spec_runner/spec_commands.py` must print nothing.

- [ ] **Step 5: Run the test and the file's suite**

Run: `uv run pytest tests/test_spec_commands.py -v`
Expected: PASS, new test included.

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/spec_commands.py tests/test_spec_commands.py
git commit -m "fix(spec): make spec approve/reject/check profile-aware

All four read_spec_meta calls in the spec family used the default lite
stage tuple, so a managed stage from a custom profile was reported as
unmanaged and the command refused to act on it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Profile-driven markers and upstreams in gated generation

**Files:**
- Modify: `src/spec_runner/cli_plan.py:41-46` (delete `_MARKER`/`_UPSTREAM`), `:97`, `:98`, `:129`, `:135`
- Test: `tests/test_gated_plan.py`

**Interfaces:**
- Consumes: `spec_runner.prompt._stage_def(stage, profile) -> StageDef` with fields `.name`, `.template`, `.marker_prefix`, `.validator_key`, `.upstream: tuple[str, ...]`, `.prompt_text`; `config.resolve_spec_profile() -> StageProfile`.
- Produces: `_generate_stage_draft(stage, description, config, invoke) -> int` — signature unchanged; return codes unchanged (0 success, 1 generation failure, 2 upstream gate).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gated_plan.py`:

```python
def test_gated_generation_handles_custom_profile_stage(tmp_path, monkeypatch):
    """A custom stage must not KeyError on the hardcoded lite marker/upstream maps."""
    from pathlib import Path

    from spec_runner.cli_plan import _generate_stage_draft
    from spec_runner.spec import SpecMeta, load_profile, stage_path, write_spec

    fixture = Path(__file__).parent / "fixtures" / "profiles" / "acceptance.yaml"
    profile = load_profile(fixture)

    config = _config(tmp_path)          # existing helper in this file
    monkeypatch.setattr(type(config), "resolve_spec_profile", lambda self: profile)

    # Both upstreams approved so the gate lets generation proceed.
    for upstream in ("requirements", "design"):
        write_spec(stage_path(config, upstream), SpecMeta(upstream, "approved"), "up\n")

    def fake_invoke(cmd, **kwargs):
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 0, stdout="TASKS_READY\nbody\nTASKS_END\n", stderr="")

    rc = _generate_stage_draft("acceptance", "desc", config, invoke=fake_invoke)
    assert rc == 0
```

Read the existing tests in `tests/test_gated_plan.py` first and reuse their config helper, their fake-invoke shape and their marker text; the fake stdout above must match whatever `parse_spec_marker` expects for the `TASKS` prefix in that file's existing tests.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_gated_plan.py::test_gated_generation_handles_custom_profile_stage -v`
Expected: FAIL with `KeyError: 'acceptance'` raised from `_UPSTREAM[stage]`.

- [ ] **Step 3: Delete the hardcoded maps**

In `src/spec_runner/cli_plan.py`, delete lines 41-46 entirely:

```python
_MARKER = {"requirements": "REQUIREMENTS", "design": "DESIGN", "tasks": "TASKS"}
_UPSTREAM: dict[str, list[str]] = {
    "requirements": [],
    "design": ["requirements"],
    "tasks": ["requirements", "design"],
}
```

- [ ] **Step 4: Resolve the stage definition once per call**

In `_generate_stage_draft`, immediately before the upstream loop, add:

```python
    from .prompt import _stage_def

    profile = config.resolve_spec_profile()
    stage_def = _stage_def(stage, profile)
    stage_names = profile.names()
```

Then replace the loop header:

```python
    for upstream in stage_def.upstream:
```

the upstream meta read:

```python
        meta = read_spec_meta(stage_path(config, upstream), stage_names)
```

the marker parse:

```python
    body = parse_spec_marker(result.stdout, stage_def.marker_prefix)
```

and the existing-version read:

```python
    existing = read_spec_meta(path, stage_names)
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_gated_plan.py -v`
Expected: PASS, new test included, all pre-existing gated-plan tests unchanged and green.

- [ ] **Step 6: Verify no hardcoded stage maps survive**

Run: `grep -n "_MARKER\|_UPSTREAM" src/spec_runner/cli_plan.py`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/cli_plan.py tests/test_gated_plan.py
git commit -m "fix(plan): drive gated generation markers and upstreams from the profile

_MARKER and _UPSTREAM were module-level dicts hardcoded to the three lite
stages, so plan --gated with a custom profile raised KeyError before it
reached the meta reads. StageDef already carries marker_prefix and
upstream; resolve both through prompt._stage_def, and pass the profile's
stage names to the two read_spec_meta calls.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full verification, CHANGELOG, PR

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 1-4 complete.
- Produces: a green branch ready for review.

- [ ] **Step 1: Confirm no profile-blind call sites remain**

Run:

```bash
grep -rn "read_spec_meta(" src/spec_runner/*.py | grep -v "def read_spec_meta"
```

Expected: every call passes a second argument. The four already-correct sites (`spec_commands.py:48`, `cli_plan.py:252`, `spec.py:450`, `spec.py:471`) plus the seven fixed here.

- [ ] **Step 2: Run the full gate**

```bash
uv run pytest tests/ -q -m "not slow"
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Expected: all pass; test count is 1129 + the tests added in Tasks 1-4.

- [ ] **Step 3: Add the CHANGELOG entries**

Under `## [Unreleased]` in `CHANGELOG.md`, add:

```markdown
### Fixed

- **Governance gate could be bypassed under a custom stage profile.**
  `spec_run_gate_ok` read `tasks.md` with `read_spec_meta`'s default `lite`
  stage tuple, so a managed spec whose stage is not part of `lite` resolved
  to `None` (= unmanaged) and passed `run --strict` / `watch --strict` even
  while in `draft`. All config-aware call sites now resolve stages from the
  configured profile: the run gate, `spec approve` / `reject` / `check`, and
  the two meta reads in gated generation.
- **`plan --gated` crashed on a custom stage profile.** `_MARKER` and
  `_UPSTREAM` in `cli_plan.py` were hardcoded to the three `lite` stages, so
  generating a stage from any other profile raised `KeyError`. Both are now
  resolved from the profile's `StageDef` (`marker_prefix`, `upstream`).
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record the profile-awareness fixes

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push -u origin fix/profile-aware-spec-surface
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "fix(spec): make the spec surface profile-aware (gate bypass + gated-plan crash)"
```

Body must state: the bypass is live in released versions (profiles shipped in v2.9.0); the two defects and their reproductions; that seven call sites changed; that no existing assertion was modified and the only test-file change beyond new tests is the `resolve_spec_profile` stub added to the `test_run_gate.py` double. Note that this PR precedes the SpecMeta contract v2 work and that the design doc is `docs/superpowers/specs/2026-07-26-specmeta-contract-v2-design.md` §3.4.

- [ ] **Step 6: Address review**

Read GitHub Copilot's review. Fix valid findings with new commits on the same branch; answer invalid ones with reasoning rather than applying them blindly. Do not merge — the user merges.

---

## Self-Review

**Spec coverage.** This plan implements design §3.4 in full: the seven call sites (Tasks 2-4), the regression test for the gate bypass (Task 2), the `_MARKER`/`_UPSTREAM` addition to §3.4 (Task 4), and its own CHANGELOG entry (Task 5), as required by the "ships first, as its own PR" decision. Everything else in the design belongs to PR 2 and is intentionally absent here — no `SpecMeta` shape change, no `extra`, no validation matrix, no `SPEC_META_CONTRACT`.

**Placeholders.** None. Three steps direct the implementer to read an existing test helper and reuse it rather than hardcoding a name this plan cannot verify (Task 1 Step 4, Task 3 Step 2, Task 4 Step 1); each states exactly what to look for and what to substitute, which is a real instruction rather than a deferred decision.

**Type consistency.** `read_spec_meta(path, stages)` takes `Sequence[str]`; `.names()` returns `tuple[str, ...]`, which satisfies it. `StageDef.upstream` is `tuple[str, ...]`, iterated exactly where the old `_UPSTREAM[stage]` list was. `_stage_def(stage, profile)` raises `KeyError` for an unknown stage — same failure mode as the dict it replaces, so no caller needs new error handling. `spec_run_gate_ok` and the three `cmd_spec_*` signatures are unchanged.
