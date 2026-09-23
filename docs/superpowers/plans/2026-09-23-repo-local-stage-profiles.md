# Repo-local Stage Profiles and External Stages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a repository declare its own stage profile, including an *external* stage that spec-runner reads (for traces, pins and an admission check) but never generates, validates or writes — so `spec approve tasks` in a devtools workstream produces `traces_to`/`upstream_hashes` for the real upstream without post-processing.

**Architecture:** `spec.py` grows the model (`StageDef.path`/`external`, repo-local lookup, per-config path resolution, strict frontmatter + admission). `config.resolve_spec_profile()` becomes the single place that loads a profile for a config and refuses bad paths/collisions. Every reader of stage metas goes through one helper that maps an external stage to a synthetic `approved` meta when admitted and `None` otherwise, so `resolve_next_stage`/`stage_readiness`/gates keep their logic; the commands refuse external targets and check admission of external upstreams.

**Tech Stack:** Python 3.11+, PyYAML, argparse, pytest (`uv run pytest`), ruff (line length 100), mypy.

**Spec:** `docs/superpowers/specs/2026-09-23-repo-local-stage-profiles-design.md` (commit `d97f20b`) — read it before starting; section numbers below refer to it.

## Global Constraints

- Repo-local profiles live at `<project_root>/spec/profiles/<name>.yaml`; a name present both there and bundled is refused, never shadowed.
- `path` is allowed **only** on an `external: true` stage; an external stage **must** declare `path`; an external stage **must not** declare `template`, `marker_prefix` or `validator`.
- Placeholders in `path`: exactly `{prefix}` (= `spec_prefix`) and `{ws}` (= `spec_prefix` without one trailing `-`). Any other `{…}` is refused. Absolute paths and paths resolving outside `project_root` are refused.
- Paths are resolved against `project_root` (never `spec_dir`, never `--change`'s directory).
- After substitution and `Path.resolve()`, no two stages share a file (external vs external, external vs every managed stage's default `spec_dir/<prefix><stage>.md`).
- External upstream admission: file exists; if frontmatter has `status`, it equals `approved`; no `status` (or no frontmatter) → existence suffices; malformed frontmatter → error naming file and YAML error.
- spec-runner **never writes** into an external stage's file.
- `approve` checks admission only for **external** upstreams; non-external upstream checking is unchanged (none).
- `lite` and every default behaviour byte-identical (`tests/test_c1_zero_behaviour.py`, `tests/test_stage_profile.py` stay green unmodified).
- Refusals exit 1 with a `⛔` line, never a traceback.
- No state-DB, `--json-result` or `schemas/` change. CHANGELOG entry under Unreleased as **minor**.
- Repo rules: TDD (failing test first), `uv run ruff format . && uv run ruff check .`, `uv run mypy src`, full `uv run pytest tests/ -q -m "not slow"` before the PR.

## Review Focus

1. **Empty `spec_prefix` with `{ws}`/`{prefix}` in a path** — `workstreams//spec/…` must be refused ("requires --spec-prefix"), not silently resolved. Test in Task 2.
2. **A symlink inside the repo pointing at a managed stage file** (`workstreams/ws/spec/30-decomposition.md -> ../../../spec/ws-tasks.md`) — a collision after `resolve()`, refused. Test in Task 2.
3. **External path that is a directory** — admission must refuse ("not a file"), not crash on `read_text`. Test in Task 3.
4. **Frontmatter with a non-string `status`** (`status: true`, `status: 1`) — not `approved`, refused quoting the value. Test in Task 3.
5. **`--change <id>` with a repo-local profile** — the profile is still found under `<project_root>/spec/profiles/` and external paths still resolve against `project_root`. Test in Task 2.

---

## File Structure

- `src/spec_runner/spec.py` — model + loading + path resolution + strict frontmatter + admission + synthetic metas + `resolve_next_stage` `waiting` + `mark_downstream_stale` skip. (It already owns the profile model, `stage_path`, metas and approval; the additions are the same responsibility.)
- `src/spec_runner/config.py` — `resolve_spec_profile()` passes `project_root`, resolves paths, maps new errors to `ConfigError`.
- `src/spec_runner/spec_commands.py` — refuse external targets; admission in `approve`; status shows external; `_metas` via the helper.
- `src/spec_runner/cli_plan.py` — gate via admission for external upstreams; refuse external target; `_current_metas` via the helper; `waiting` message.
- `src/spec_runner/cli.py` — drop hardcoded stage `choices`; validate names against the profile.
- Tests: `tests/test_repo_local_profiles.py` (Tasks 1–2), `tests/test_external_stages.py` (Tasks 3–5).
- Docs: `CHANGELOG.md`, `CLAUDE.md` (spec.py row + CLI examples), `README.md` (spec governance section: one paragraph + the §4 example).

---

### Task 1: Profile model, repo-local lookup, field rules, graph-error class

**Files:**
- Modify: `src/spec_runner/spec.py` (`StageDef`, `StageProfile`, `load_profile`, `validate_profile_graph`, `available_profiles`)
- Test: `tests/test_repo_local_profiles.py` (create)

**Interfaces:**
- Produces:
  - `StageDef.path: str | None = None`, `StageDef.external: bool = False` (new trailing fields; `template`/`marker_prefix`/`validator_key` become `str = ""` defaults for external stages).
  - `StageProfile.get(name: str) -> StageDef | None`.
  - `class ProfileError(ValueError)` — any refusal about a profile's content (fields, placeholders, collisions); `ProfileGraphError(ProfileError)`.
  - `load_profile(name: str, project_root: Path | None = None) -> StageProfile`.
  - `available_profiles(project_root: Path | None = None) -> list[str]` (sorted, union of both sources).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repo_local_profiles.py
"""#338: repo-local stage profiles and external stages — the model."""

from pathlib import Path

import pytest

from spec_runner.spec import (
    LITE,
    ProfileError,
    ProfileGraphError,
    available_profiles,
    load_profile,
)

WORKSTREAM = """\
name: workstream
stages:
  - name: decomposition
    external: true
    path: "workstreams/{ws}/spec/30-decomposition.md"
    upstream: []
  - name: tasks
    template: tasks.template.md
    marker_prefix: SPEC_TASKS
    validator: tasks
    upstream: [decomposition]
"""


def _write_profile(root: Path, name: str, text: str) -> None:
    d = root / "spec" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(text)


class TestRepoLocalLookup:
    def test_a_repo_local_profile_loads(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        profile = load_profile("workstream", tmp_path)
        assert profile.names() == ("decomposition", "tasks")
        dec = profile.get("decomposition")
        assert dec is not None and dec.external and dec.path.startswith("workstreams/")
        assert profile.get("tasks").upstream == ("decomposition",)

    def test_a_bundled_profile_still_loads_without_a_root(self):
        assert load_profile("lite") == LITE

    def test_a_local_name_shadowing_a_bundled_one_is_refused(self, tmp_path):
        _write_profile(tmp_path, "lite", WORKSTREAM)
        with pytest.raises(ProfileError, match="rename the local one"):
            load_profile("lite", tmp_path)

    def test_an_unknown_name_is_still_a_plain_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="unknown stage profile"):
            load_profile("nope", tmp_path)

    def test_available_lists_both_sources(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        assert available_profiles(tmp_path) == ["lite", "workstream"]
        assert available_profiles() == ["lite"]


class TestStageFieldRules:
    @pytest.mark.parametrize(
        ("stage_yaml", "message"),
        [
            ("  - name: x\n    external: true\n    upstream: []\n", "must declare `path`"),
            (
                "  - name: x\n    external: true\n    path: a.md\n    template: t.md\n",
                "must not declare",
            ),
            (
                "  - name: x\n    template: t.md\n    marker_prefix: X\n    validator: tasks\n"
                "    path: a.md\n",
                "only on an external stage",
            ),
            (
                "  - name: x\n    external: true\n    path: \"a/{nope}.md\"\n",
                "unknown placeholder",
            ),
            ("  - name: x\n    external: true\n    path: /abs.md\n", "relative to the project"),
        ],
    )
    def test_refused(self, tmp_path, stage_yaml, message):
        _write_profile(tmp_path, "bad", "name: bad\nstages:\n" + stage_yaml)
        with pytest.raises(ProfileError, match=message):
            load_profile("bad", tmp_path)

    def test_a_non_mapping_profile_is_refused(self, tmp_path):
        _write_profile(tmp_path, "bad", "- just\n- a list\n")
        with pytest.raises(ProfileError, match="mapping"):
            load_profile("bad", tmp_path)


class TestGraphErrorsAreGraphErrors:
    def test_a_cycle_raises_the_graph_error_class(self, tmp_path):
        _write_profile(
            tmp_path,
            "cyc",
            "name: cyc\nstages:\n"
            "  - {name: a, template: t, marker_prefix: A, validator: tasks, upstream: [b]}\n"
            "  - {name: b, template: t, marker_prefix: B, validator: tasks, upstream: [a]}\n",
        )
        with pytest.raises(ProfileGraphError, match="cycle"):
            load_profile("cyc", tmp_path)

    def test_an_unknown_upstream_raises_the_graph_error_class(self, tmp_path):
        _write_profile(
            tmp_path,
            "dang",
            "name: dang\nstages:\n"
            "  - {name: a, template: t, marker_prefix: A, validator: tasks, upstream: [zz]}\n",
        )
        with pytest.raises(ProfileGraphError, match="unknown stage"):
            load_profile("dang", tmp_path)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_repo_local_profiles.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProfileError'`.

- [ ] **Step 3: Implement**

In `src/spec_runner/spec.py`:

```python
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_KNOWN_PLACEHOLDERS = frozenset({"prefix", "ws"})


@dataclass(frozen=True)
class StageDef:
    """(existing docstring) ... ``external`` marks a stage produced outside
    spec-runner (#338): it has a ``path`` and no template/marker/validator."""

    name: str
    template: str = ""
    marker_prefix: str = ""
    validator_key: str = ""
    upstream: tuple[str, ...] = ()
    prompt_text: str = ""
    path: str | None = None
    external: bool = False
    # keep the `requires` property unchanged
```

Add to `StageProfile`:

```python
    def get(self, name: str) -> StageDef | None:
        """The stage named ``name``, or None."""
        return next((s for s in self.stages if s.name == name), None)
```

Replace the exception classes (move above `load_profile`):

```python
class ProfileError(ValueError):
    """A profile was found but its content is refused (#338): stage fields,
    placeholders, name collision, file collisions."""


class ProfileGraphError(ProfileError):
    """(existing docstring)"""
```

In `validate_profile_graph` change both `raise ValueError(` to `raise ProfileGraphError(`.

Replace `load_profile` and `available_profiles`:

```python
def _local_profiles_dir(project_root: Path | None) -> Path | None:
    return None if project_root is None else Path(project_root) / "spec" / "profiles"


def load_profile(name: str, project_root: Path | None = None) -> StageProfile:
    """Load profile ``name`` from ``<project_root>/spec/profiles`` or the bundle.

    A name found in both places is refused (#338): no precedence to remember.

    Raises:
        ValueError: the name exists nowhere ("unknown stage profile").
        ProfileError: the profile exists but is refused (fields, graph, shadowing).
    """
    bundled = files("spec_runner") / "profiles" / f"{name}.yaml"
    local_dir = _local_profiles_dir(project_root)
    local = local_dir / f"{name}.yaml" if local_dir is not None else None
    bundled_exists = bundled.is_file()
    local_exists = local is not None and local.is_file()
    if bundled_exists and local_exists:
        raise ProfileError(
            f"profile {name!r} exists in spec/profiles/ and is bundled; rename the local one"
        )
    if local_exists:
        assert local is not None
        raw = local.read_text(encoding="utf-8")
    elif bundled_exists:
        raw = bundled.read_text(encoding="utf-8")
    else:
        raise ValueError(f"unknown stage profile: {name!r}")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ProfileError(f"profile {name!r} is not a mapping")
    stages = tuple(_stage_def_from(s, name) for s in data.get("stages", []))
    profile = StageProfile(name=data.get("profile", data.get("name", name)), stages=stages)
    validate_profile_graph(profile)
    return profile


def _stage_def_from(s: dict, profile: str) -> StageDef:
    """One stage entry → `StageDef`, enforcing the #338 field rules."""
    name = s["name"]
    external = bool(s.get("external", False))
    path = s.get("path")
    upstream = tuple(s.get("requires") or s.get("upstream") or ())
    where = f"stage {name!r} in profile {profile!r}"
    if external:
        if not path:
            raise ProfileError(f"{where}: an external stage must declare `path`")
        declared = [k for k in ("template", "marker_prefix", "validator") if k in s]
        if declared:
            raise ProfileError(
                f"{where}: an external stage must not declare {', '.join(declared)} — "
                "it is never generated or validated here"
            )
        _check_path_template(path, where)
        return StageDef(name=name, upstream=upstream, path=path, external=True)
    if path is not None:
        raise ProfileError(
            f"{where}: `path` is allowed only on an external stage in this release"
        )
    return StageDef(
        name=name,
        template=s["template"],
        marker_prefix=s["marker_prefix"],
        validator_key=s["validator"],
        upstream=upstream,
        prompt_text=s.get("prompt_text", ""),
    )


def _check_path_template(path: str, where: str) -> None:
    unknown = [p for p in _PLACEHOLDER.findall(path) if p not in _KNOWN_PLACEHOLDERS]
    if unknown:
        raise ProfileError(f"{where}: unknown placeholder {{{unknown[0]}}} in path {path!r}")
    if Path(path).is_absolute():
        raise ProfileError(f"{where}: path {path!r} must be relative to the project root")


def available_profiles(project_root: Path | None = None) -> list[str]:
    """Sorted names of bundled profiles plus ``<project_root>/spec/profiles``."""
    prof_dir = files("spec_runner") / "profiles"
    names = {e.name[: -len(".yaml")] for e in prof_dir.iterdir() if e.name.endswith(".yaml")}
    local_dir = _local_profiles_dir(project_root)
    if local_dir is not None and local_dir.is_dir():
        names |= {p.stem for p in local_dir.glob("*.yaml")}
    return sorted(names)
```

Note: `data.get("profile", data.get("name", name))` keeps `lite` (which uses `profile:` or neither) identical; check `lite.yaml`'s top-level key before editing and keep `LITE == load_profile("lite")`.

- [ ] **Step 4: Run the new tests and the pinned profile tests**

Run: `uv run pytest tests/test_repo_local_profiles.py tests/test_stage_profile.py tests/test_c1_zero_behaviour.py tests/test_dag_profiles.py tests/test_prompt_profile.py -q`
Expected: all PASS. If `test_dag_profiles.py` asserted `ValueError` for graph errors it still passes (`ProfileGraphError` is a `ValueError`).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py tests/test_repo_local_profiles.py
git commit -m "spec: repo-local profiles, external stage fields, graph errors as ProfileGraphError (#338)"
```

---

### Task 2: Per-config resolution — placeholders, containment, collisions, `stage_path`

**Files:**
- Modify: `src/spec_runner/spec.py` (`stage_path`, new `resolve_stage_paths`)
- Modify: `src/spec_runner/config.py` (`resolve_spec_profile`)
- Test: `tests/test_repo_local_profiles.py` (append)

**Interfaces:**
- Consumes: Task 1's `load_profile(name, project_root)`, `ProfileError`, `StageDef.path/external`.
- Produces:
  - `resolve_stage_paths(profile: StageProfile, config: ExecutorConfig) -> dict[str, Path]` — every stage → absolute resolved path; raises `ProfileError` on empty-prefix placeholder, escape, collision.
  - `stage_path(config, stage, profile: StageProfile | None = None) -> Path` — external stages from their declared path; managed unchanged.
  - `ExecutorConfig.resolve_spec_profile()` raises `ConfigError` for `ProfileError` (with its message) and runs `resolve_stage_paths` so a bad path fails at startup.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_repo_local_profiles.py`)

```python
from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.spec import resolve_stage_paths, stage_path


def _cfg(root: Path, prefix: str = "ws-", **kw) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, spec_prefix=prefix, spec_profile="workstream", **kw)


class TestPathResolution:
    def test_ws_and_prefix_are_substituted_against_the_project_root(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        cfg = _cfg(tmp_path)
        profile = cfg.resolve_spec_profile()
        assert stage_path(cfg, "decomposition", profile) == (
            tmp_path / "workstreams/ws/spec/30-decomposition.md"
        ).resolve()
        assert stage_path(cfg, "tasks", profile) == cfg.spec_dir / "ws-tasks.md"

    def test_a_managed_path_is_unchanged_under_lite(self, tmp_path):
        cfg = ExecutorConfig(project_root=tmp_path)
        assert stage_path(cfg, "tasks") == cfg.spec_dir / "tasks.md"

    def test_an_empty_prefix_with_a_placeholder_is_refused(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        with pytest.raises(ConfigError, match="requires --spec-prefix"):
            _cfg(tmp_path, prefix="").resolve_spec_profile()

    def test_a_path_escaping_the_project_is_refused(self, tmp_path):
        _write_profile(
            tmp_path,
            "workstream",
            WORKSTREAM.replace("workstreams/{ws}/spec/30-decomposition.md", "../outside.md"),
        )
        with pytest.raises(ConfigError, match="outside the project"):
            _cfg(tmp_path).resolve_spec_profile()

    def test_an_external_path_on_a_managed_file_is_refused(self, tmp_path):
        _write_profile(
            tmp_path,
            "workstream",
            WORKSTREAM.replace("workstreams/{ws}/spec/30-decomposition.md", "spec/{prefix}tasks.md"),
        )
        with pytest.raises(ConfigError, match="decomposition.*tasks|tasks.*decomposition"):
            _cfg(tmp_path).resolve_spec_profile()

    def test_a_symlink_onto_a_managed_file_is_a_collision(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / "ws-tasks.md").write_text("x\n")
        link = tmp_path / "workstreams" / "ws" / "spec" / "30-decomposition.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(tmp_path / "spec" / "ws-tasks.md")
        with pytest.raises(ConfigError, match="same file"):
            _cfg(tmp_path).resolve_spec_profile()

    def test_a_change_folder_still_finds_the_profile_and_the_root_path(self, tmp_path):
        _write_profile(tmp_path, "workstream", WORKSTREAM)
        cfg = _cfg(tmp_path, change_id="add-x")
        profile = cfg.resolve_spec_profile()
        assert stage_path(cfg, "decomposition", profile) == (
            tmp_path / "workstreams/ws/spec/30-decomposition.md"
        ).resolve()

    def test_graph_errors_are_reported_as_graph_errors(self, tmp_path):
        _write_profile(
            tmp_path,
            "workstream",
            "name: workstream\nstages:\n"
            "  - {name: a, template: t, marker_prefix: A, validator: tasks, upstream: [a]}\n",
        )
        with pytest.raises(ConfigError, match="cycle") as exc:
            _cfg(tmp_path).resolve_spec_profile()
        assert "unknown spec_profile" not in str(exc.value)
```

Before writing `test_a_change_folder…`, confirm the config field name for `--change` is `change_id` (`grep -n "change_id" src/spec_runner/config.py`); adjust the keyword if different.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_repo_local_profiles.py -q -k PathResolution`
Expected: FAIL — `ImportError: cannot import name 'resolve_stage_paths'`.

- [ ] **Step 3: Implement**

In `src/spec_runner/spec.py`:

```python
def _default_stage_path(config: ExecutorConfig, stage: str) -> Path:
    return config.spec_dir / f"{config.spec_prefix}{stage}.md"


def resolve_stage_paths(profile: StageProfile, config: ExecutorConfig) -> dict[str, Path]:
    """Every stage's file, with #338's rules applied (spec §4, §4.2).

    External paths: placeholders substituted, resolved against
    ``project_root`` (never ``spec_dir``), required to stay inside it.
    Then no two stages may resolve to the same file.
    """
    root = Path(config.project_root).resolve()
    prefix = config.spec_prefix or ""
    ws = prefix[:-1] if prefix.endswith("-") else prefix
    out: dict[str, Path] = {}
    for sd in profile.stages:
        if not sd.external:
            out[sd.name] = _default_stage_path(config, sd.name)
            continue
        assert sd.path is not None
        if _PLACEHOLDER.search(sd.path) and not prefix:
            raise ProfileError(
                f"stage {sd.name!r}: path {sd.path!r} uses a placeholder, which "
                "requires --spec-prefix"
            )
        resolved = (root / sd.path.format(prefix=prefix, ws=ws)).resolve()
        if not resolved.is_relative_to(root):
            raise ProfileError(
                f"stage {sd.name!r}: path {sd.path!r} resolves outside the project ({resolved})"
            )
        out[sd.name] = resolved
    seen: dict[Path, str] = {}
    for name, p in out.items():
        key = p.resolve()
        if key in seen:
            raise ProfileError(
                f"stages {seen[key]!r} and {name!r} resolve to the same file ({key}); "
                "an external stage must not share a file with any other stage"
            )
        seen[key] = name
    return out


def stage_path(config: ExecutorConfig, stage: str, profile: StageProfile | None = None) -> Path:
    """(existing docstring) + external stages (#338) come from their declared
    `path`; every managed stage keeps `spec/<prefix><name>.md`."""
    graph = profile if profile is not None else _profile_for(config)
    sd = graph.get(stage) if graph is not None else None
    if sd is not None and sd.external:
        return resolve_stage_paths(graph, config)[stage]
    return _default_stage_path(config, stage)


def _profile_for(config: ExecutorConfig) -> StageProfile | None:
    """The config's profile, or None when resolving it is not possible (a
    bare config object in a test)."""
    resolver = getattr(config, "resolve_spec_profile", None)
    return resolver() if resolver is not None else None
```

`str.format` with a stray `{` is already excluded by `_check_path_template`; keep it that way.

In `src/spec_runner/config.py` replace `resolve_spec_profile`:

```python
    def resolve_spec_profile(self) -> "StageProfile":
        """(existing docstring) + repo-local profiles and path rules (#338)."""
        from .spec import ProfileError, available_profiles, load_profile, resolve_stage_paths

        try:
            profile = load_profile(self.spec_profile, self.project_root)
            resolve_stage_paths(profile, self)
            return profile
        except ProfileError as exc:
            raise ConfigError(str(exc)) from exc
        except ValueError:
            available = ", ".join(available_profiles(self.project_root))
            raise ConfigError(
                f"unknown spec_profile: {self.spec_profile!r}; available: {available}"
            ) from None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_repo_local_profiles.py tests/test_spec_profile_config.py tests/test_c1_zero_behaviour.py tests/test_spec_commands.py tests/test_gated_plan.py tests/test_authoring_links.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py src/spec_runner/config.py tests/test_repo_local_profiles.py
git commit -m "spec: resolve external stage paths per config — placeholders, containment, collisions (#338)"
```

---

### Task 3: Strict frontmatter and external admission; `approve` gates on it

**Files:**
- Modify: `src/spec_runner/spec.py` (new `ExternalStageError`, `read_frontmatter_strict`, `external_admission`)
- Modify: `src/spec_runner/spec_commands.py` (`cmd_spec_approve`, refuse external targets in approve/reject/adopt/check)
- Test: `tests/test_external_stages.py` (create)

**Interfaces:**
- Consumes: Task 2's `stage_path(config, stage, profile)`, `StageProfile.get`.
- Produces:
  - `class ExternalStageError(Exception)`.
  - `read_frontmatter_strict(path: Path) -> dict | None` — None when there is no leading `---` block; raises `ExternalStageError` on invalid YAML or a non-mapping block.
  - `external_admission(config, profile, stage) -> str | None` — None = admitted; otherwise the refusal sentence. Never writes.
  - `external_upstream_refusal(config, profile, stage) -> str | None` — the first refusal among `stage`'s external direct upstreams.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_external_stages.py
"""#338: external stages — admission, refusals, the issue's observable."""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.spec import git_blob_hash, split_frontmatter

WORKSTREAM = """\
name: workstream
stages:
  - name: decomposition
    external: true
    path: "workstreams/{ws}/spec/30-decomposition.md"
    upstream: []
  - name: tasks
    template: tasks.template.md
    marker_prefix: SPEC_TASKS
    validator: tasks
    upstream: [decomposition]
"""

TASKS = """\
---
spec_stage: tasks
status: draft
version: 1
---
# Tasks

### TASK-001: demo
P1 | TODO   Est: 1h

**Traces to:** [DT-01]
"""


def _project(tmp_path: Path, decomposition: str | None) -> tuple[ExecutorConfig, Path]:
    (tmp_path / "spec" / "profiles").mkdir(parents=True)
    (tmp_path / "spec" / "profiles" / "workstream.yaml").write_text(WORKSTREAM)
    (tmp_path / "spec" / "ws-tasks.md").write_text(TASKS)
    ext = tmp_path / "workstreams" / "ws" / "spec" / "30-decomposition.md"
    if decomposition is not None:
        ext.parent.mkdir(parents=True)
        ext.write_text(decomposition)
    cfg = ExecutorConfig(project_root=tmp_path, spec_prefix="ws-", spec_profile="workstream")
    return cfg, ext


def _approve(cfg: ExecutorConfig, stage: str) -> int:
    from argparse import Namespace

    from spec_runner.spec_commands import cmd_spec_approve

    return cmd_spec_approve(Namespace(stage=stage), cfg)


APPROVED = "---\nspec_stage: decomposition\nstatus: approved\n---\n## DT-01\nbody\n"


class TestTheIssuesObservable:
    def test_approve_tasks_traces_and_pins_the_external_upstream(self, tmp_path):
        cfg, ext = _project(tmp_path, APPROVED)
        before = ext.read_bytes()
        assert _approve(cfg, "tasks") == 0
        meta, _ = split_frontmatter((tmp_path / "spec" / "ws-tasks.md").read_text())
        assert meta["traces_to"][0] == "decomposition"
        assert "design" not in meta["traces_to"]
        assert meta["upstream_hashes"] == {"decomposition": git_blob_hash(before)}
        assert ext.read_bytes() == before


class TestAdmission:
    @pytest.mark.parametrize(
        ("decomposition", "expect_rc", "needle"),
        [
            ("---\nspec_stage: decomposition\nstatus: draft\n---\nx\n", 1, "draft"),
            ("---\nstatus: true\n---\nx\n", 1, "True"),
            ("---\nspec_stage: decomposition\n---\nx\n", 0, "approved"),
            ("no frontmatter at all\n", 0, "approved"),
            ("---\nstatus: [unclosed\n---\nx\n", 1, "malformed"),
            ("---\n- a list\n---\nx\n", 1, "malformed"),
        ],
    )
    def test_the_admission_table(self, tmp_path, capsys, decomposition, expect_rc, needle):
        cfg, ext = _project(tmp_path, decomposition)
        before = ext.read_bytes()
        rc = _approve(cfg, "tasks")
        out = capsys.readouterr().out
        assert rc == expect_rc
        assert needle in out
        assert ext.read_bytes() == before

    def test_a_missing_external_file_is_refused_naming_the_path(self, tmp_path, capsys):
        cfg, ext = _project(tmp_path, None)
        assert _approve(cfg, "tasks") == 1
        assert "30-decomposition.md" in capsys.readouterr().out

    def test_an_external_path_that_is_a_directory_is_refused(self, tmp_path, capsys):
        cfg, ext = _project(tmp_path, None)
        ext.mkdir(parents=True)
        assert _approve(cfg, "tasks") == 1
        assert "not a file" in capsys.readouterr().out


class TestExternalTargetsAreRefused:
    @pytest.mark.parametrize("command", ["approve", "reject", "adopt", "check"])
    def test_every_spec_command_refuses_an_external_target(self, tmp_path, capsys, command):
        from argparse import Namespace

        import spec_runner.spec_commands as sc

        cfg, ext = _project(tmp_path, APPROVED)
        before = ext.read_bytes()
        handler = getattr(sc, f"cmd_spec_{command}")
        rc = handler(Namespace(stage="decomposition", force=False), cfg)
        assert rc == 1
        assert "is external" in capsys.readouterr().out
        assert ext.read_bytes() == before
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_external_stages.py -q`
Expected: FAIL — approve with `lite`-era code adds `design` / admission missing / external targets not refused.

- [ ] **Step 3: Implement** in `src/spec_runner/spec.py`:

```python
class ExternalStageError(Exception):
    """An external stage's file cannot be read as the admission rule needs."""


def read_frontmatter_strict(path: Path) -> dict | None:
    """The leading frontmatter mapping, None when there is none; raises on a
    block that is not valid YAML or not a mapping (#338 §6 — never read as
    "no status")."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith(_FM_DELIM + "\n"):
        return None
    end = text.find("\n" + _FM_DELIM, len(_FM_DELIM))
    if end == -1:
        raise ExternalStageError(f"{path}: malformed frontmatter — no closing `---`")
    block = text[len(_FM_DELIM) + 1 : end]
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise ExternalStageError(f"{path}: malformed frontmatter — {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ExternalStageError(f"{path}: malformed frontmatter — not a mapping")
    return loaded


def external_admission(config: ExecutorConfig, profile: StageProfile, stage: str) -> str | None:
    """None when external ``stage`` admits its downstream; else why not (§6).
    Reads only."""
    path = stage_path(config, stage, profile)
    if not path.exists():
        return f"external upstream {stage!r} not found at {path}"
    if not path.is_file():
        return f"external upstream {stage!r} at {path} is not a file"
    try:
        meta = read_frontmatter_strict(path)
    except ExternalStageError as exc:
        return f"external upstream {stage!r}: {exc}"
    if meta is not None and "status" in meta and meta["status"] != "approved":
        return f"external upstream {stage!r} has status {meta['status']!r}, not 'approved'"
    return None


def external_upstream_refusal(
    config: ExecutorConfig, profile: StageProfile, stage: str
) -> str | None:
    """The first refusal among ``stage``'s **external** direct upstreams."""
    sd = profile.get(stage)
    for up in sd.upstream if sd is not None else ():
        up_def = profile.get(up)
        if up_def is not None and up_def.external:
            refusal = external_admission(config, profile, up)
            if refusal is not None:
                return refusal
    return None
```

In `src/spec_runner/spec_commands.py` add a guard used by approve/reject/adopt/check, first thing in each:

```python
def _refuse_external(config: ExecutorConfig, stage: str) -> int | None:
    """1 (after printing why) when ``stage`` is external, else None (#338)."""
    sd = _profile(config).get(stage)
    if sd is not None and sd.external:
        print(f"⛔ {stage} is external — produced outside spec-runner; nothing to do here")
        return 1
    return None
```

and in each of `cmd_spec_approve`, `cmd_spec_reject`, `cmd_spec_adopt`, `cmd_spec_check`, as the first statements after `stage = args.stage`:

```python
    refused = _refuse_external(config, stage)
    if refused is not None:
        return refused
```

In `cmd_spec_approve`, after the `meta is None` check and before `validate_spec_stage`:

```python
    upstream_refusal = external_upstream_refusal(config, _profile(config), stage)
    if upstream_refusal is not None:
        print(f"⛔ {stage}: not approved — {upstream_refusal}")
        return 1
```

Import `external_upstream_refusal` from `.spec`. The admission-table "approved" needle is satisfied by the existing `"{stage}: approved (v…)"` line. Check that `tasks` validation passes for the fixture body (`uv run spec-runner validate` rules); if the tasks validator requires fields the fixture lacks, extend `TASKS` rather than weakening the test.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_external_stages.py tests/test_spec_commands.py tests/test_adopt_gate.py tests/test_authoring_links.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py src/spec_runner/spec_commands.py tests/test_external_stages.py
git commit -m "spec: external upstream admission gates approve; external targets refused (#338)"
```

---

### Task 4: Metas, status, next stage, stale cascade, `plan --gated`

**Files:**
- Modify: `src/spec_runner/spec.py` (`profile_metas`, `resolve_next_stage` `waiting`, `mark_downstream_stale` skip)
- Modify: `src/spec_runner/spec_commands.py` (`_metas`, `cmd_spec_status`)
- Modify: `src/spec_runner/cli_plan.py` (`_current_metas`, `run_gated_stage` gate + external target, `_print_gate_status`)
- Test: `tests/test_external_stages.py` (append)

**Interfaces:**
- Consumes: Task 3's `external_admission`, `external_upstream_refusal`, `ExternalStageError`.
- Produces:
  - `profile_metas(config, profile) -> dict[str, SpecMeta | None]` — managed: `read_spec_meta`; external: `SpecMeta(spec_stage=name, status="approved", version=1)` when admitted, else None.
  - `resolve_next_stage(...)` may return `("waiting", stage)` for a not-admitted external stage (never `"generate"`).
  - `external_status_line(config, profile, stage) -> str` — `present` / `missing` / `status: <v>` / `malformed frontmatter`.

- [ ] **Step 1: Write the failing tests** (append)

```python
class TestTheRestOfTheLifecycle:
    def test_status_shows_the_external_stage(self, tmp_path, capsys):
        from argparse import Namespace

        from spec_runner.spec_commands import cmd_spec_status

        cfg, _ = _project(tmp_path, "---\nstatus: draft\n---\nx\n")
        cmd_spec_status(Namespace(), cfg)
        out = capsys.readouterr().out
        assert "decomposition" in out and "external" in out and "status: draft" in out
        assert "next: waiting → decomposition" in out

    def test_next_stage_is_never_generate_for_an_external_stage(self, tmp_path):
        from spec_runner.spec import profile_metas, resolve_next_stage

        cfg, _ = _project(tmp_path, None)
        profile = cfg.resolve_spec_profile()
        assert resolve_next_stage(profile_metas(cfg, profile), profile) == (
            "waiting",
            "decomposition",
        )

    def test_approving_tasks_never_writes_stale_into_an_external_file(self, tmp_path):
        from spec_runner.spec import mark_downstream_stale
        from spec_runner.config import ExecutorLock

        cfg, ext = _project(tmp_path, APPROVED)
        before = ext.read_bytes()
        profile = cfg.resolve_spec_profile()
        mark_downstream_stale(cfg, "decomposition", ExecutorLock(cfg.spec_lock_file), profile)
        assert ext.read_bytes() == before

    def test_plan_gated_refuses_an_external_target(self, tmp_path, capsys):
        from spec_runner.cli_plan import run_gated_stage

        cfg, ext = _project(tmp_path, APPROVED)
        assert run_gated_stage("decomposition", "d", cfg, invoke=_never) == 1
        assert "is external" in capsys.readouterr().out

    def test_plan_gated_gates_on_admission(self, tmp_path, capsys):
        from spec_runner.cli_plan import run_gated_stage

        cfg, ext = _project(tmp_path, "---\nstatus: draft\n---\nx\n")
        assert run_gated_stage("tasks", "d", cfg, invoke=_never) == 2
        assert "draft" in capsys.readouterr().out


def _never(*_a, **_k):
    raise AssertionError("no generation may run")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_external_stages.py -q -k Lifecycle`
Expected: FAIL.

- [ ] **Step 3: Implement**

`src/spec_runner/spec.py`:

```python
def profile_metas(config: ExecutorConfig, profile: StageProfile) -> dict[str, SpecMeta | None]:
    """Per-stage metas for ``profile`` (#338): an external stage reads as a
    synthetic ``approved`` meta when admitted and None otherwise, so readiness
    and gates keep their logic. Nothing is written."""
    names = profile.names()
    out: dict[str, SpecMeta | None] = {}
    for sd in profile.stages:
        if sd.external:
            admitted = external_admission(config, profile, sd.name) is None
            out[sd.name] = (
                SpecMeta(spec_stage=sd.name, status="approved", version=1) if admitted else None
            )
        else:
            out[sd.name] = read_spec_meta(stage_path(config, sd.name, profile), names)
    return out


def external_status_line(config: ExecutorConfig, profile: StageProfile, stage: str) -> str:
    """What `spec status` shows for an external stage."""
    path = stage_path(config, stage, profile)
    if not path.is_file():
        return "missing"
    try:
        meta = read_frontmatter_strict(path)
    except ExternalStageError:
        return "malformed frontmatter"
    if meta and "status" in meta:
        return f"status: {meta['status']}"
    return "present"
```

In `resolve_next_stage`, inside the `if m is None:` branch, before the `generate` return:

```python
            if isinstance(graph, StageProfile):
                sd = graph.get(stage)
                if sd is not None and sd.external:
                    return ("waiting", stage)
```

In `mark_downstream_stale`, skip external stages:

```python
    for ds in downstream_stages(stage, graph):
        if isinstance(graph, StageProfile) and (sd := graph.get(ds)) is not None and sd.external:
            continue
        ...existing body...
```

`src/spec_runner/spec_commands.py`: `_metas` → `return profile_metas(config, _profile(config))`; in `cmd_spec_status` loop:

```python
        sd = profile.get(stage)
        if sd is not None and sd.external:
            print(f"{stage:12} external  {external_status_line(config, profile, stage)}")
            continue
```

`src/spec_runner/cli_plan.py`:
- `_current_metas(config)` → `profile = config.resolve_spec_profile(); return profile_metas(config, profile)`.
- At the top of `run_gated_stage`, after `stage_def = _stage_def(stage, profile)` — move the external check **before** `_stage_def` (which would `KeyError`-ish on missing template use):

```python
    sd = profile.get(stage)
    if sd is not None and sd.external:
        print(f"⛔ {stage} is external — produced outside spec-runner; not generated here")
        return 1
```

- In the upstream gate loop:

```python
    for upstream in stage_def.upstream:
        up = profile.get(upstream)
        if up is not None and up.external:
            refusal = external_admission(config, profile, upstream)
            if refusal is not None:
                print(f"⛔ cannot generate {stage}: {refusal}")
                return 2
            continue
        ...existing managed check...
```

- In the ancestor-context loop, for an external ancestor read its body with `read_spec_body(stage_path(config, ancestor, profile))` and set `statuses[ancestor] = "external"`.
- `_print_gate_status`: add

```python
    if action == "waiting":
        print(f"waiting for external {stage} — produced outside spec-runner")
        return True
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_external_stages.py tests/test_gated_plan.py tests/test_spec_commands.py tests/test_dag_profiles.py tests/test_c1_zero_behaviour.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py src/spec_runner/spec_commands.py src/spec_runner/cli_plan.py tests/test_external_stages.py
git commit -m "spec: external stages in status, next-stage, stale cascade and plan --gated (#338)"
```

---

### Task 5: CLI stage names from the profile; docs; full suite

**Files:**
- Modify: `src/spec_runner/cli.py` (drop `choices` on `plan --stage` and `spec approve|reject|check|adopt` stage args; validate after config)
- Modify: `CHANGELOG.md`, `CLAUDE.md`, `README.md`
- Test: `tests/test_external_stages.py` (append)

**Interfaces:**
- Consumes: `config.resolve_spec_profile()`.
- Produces: `_check_stage_name(config, stage) -> None` in `cli.py` — `SystemExit("⛔ unknown stage …; profile <name> has: …")`.

- [ ] **Step 1: Write the failing tests** (append)

```python
class TestCliStageNames:
    def test_a_profile_stage_name_is_accepted_by_the_cli(self, tmp_path, monkeypatch, capsys):
        from spec_runner.cli import main

        cfg, _ = _project(tmp_path, APPROVED)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["spec-runner", "spec", "approve", "decomposition", "--spec-prefix", "ws-",
             "--profile", "workstream"],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "is external" in capsys.readouterr().out

    def test_an_unknown_stage_names_the_profile_stages(self, tmp_path, monkeypatch):
        from spec_runner.cli import main

        _project(tmp_path, APPROVED)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["spec-runner", "spec", "approve", "design", "--spec-prefix", "ws-",
             "--profile", "workstream"],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert "decomposition" in str(exc.value) and "tasks" in str(exc.value)

    def test_lite_still_rejects_an_unknown_stage(self, tmp_path, monkeypatch):
        from spec_runner.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["spec-runner", "spec", "approve", "decomposition"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert "requirements" in str(exc.value)
```

Before writing these, check how `--profile` / `--spec-prefix` are spelled on the `spec` subparser (`grep -n '"--profile"' src/spec_runner/cli.py`) and whether `spec approve` exits via `raise SystemExit(handler(...))` — match the real argv order.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_external_stages.py -q -k CliStageNames`
Expected: FAIL — argparse rejects `decomposition` (`invalid choice`).

- [ ] **Step 3: Implement** in `src/spec_runner/cli.py`: remove `choices=["requirements", "design", "tasks"]` from `plan --stage` (line ~2075) and the four `spec` subparsers (~2424–2439), keeping their help text. Add:

```python
def _check_stage_name(config: ExecutorConfig, stage: str | None) -> None:
    """Stage names come from the resolved profile (#338), not a fixed list."""
    if stage is None:
        return
    names = config.resolve_spec_profile().names()
    if stage not in names:
        raise SystemExit(
            f"⛔ unknown stage {stage!r}; profile {config.spec_profile!r} has: {', '.join(names)}"
        )
```

Call it in `main()` right before dispatching `spec` subcommands (`args.spec_command in {"approve","reject","check","adopt"}` → `_check_stage_name(config, args.stage)`) and before `plan` when `getattr(args, "stage", None)` is set.

Docs:
- `CHANGELOG.md` under `## [Unreleased]` → `### Added`: a bullet "**Repo-local stage profiles and external stages (#338).**" stating: `spec/profiles/<name>.yaml`, shadowing refused, `external: true` + `path` (`{prefix}`, `{ws}`), admission rule (status approved or absent; malformed frontmatter refused), external stages never written, `approve` gates only external upstreams, stage names from the profile, graph errors reported as such. Minor.
- `README.md`, spec governance section: one paragraph + the spec's §4 YAML example.
- `CLAUDE.md`: extend the `spec.py` row (profiles row) with the new functions; add `spec approve tasks --profile workstream --spec-prefix <ws>-` to the CLI block; list the two new test files.

- [ ] **Step 4: Full verification**

Run:
```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
uv run pytest tests/ -q -m "not slow" -p no:cacheprovider
```
Expected: all green; `test_c1_zero_behaviour.py` and `test_stage_profile.py` unmodified and passing.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/cli.py tests/test_external_stages.py CHANGELOG.md CLAUDE.md README.md
git commit -m "cli: stage names from the profile; docs for repo-local profiles (#338)"
```

---

## After the tasks

- One local review round (`sh scripts/review/local-claude.sh`); stop when a round has no major/blocker with high confidence.
- Draft PR "Closes #338", acceptance `sh ../devtools/review-pr.sh spec-runner <pr>`, merge per repo policy.
- Reply on #338 (inbox) with how to declare the profile; open an issue in devtools proposing they declare `spec/profiles/workstream.yaml` and drop `--conform-approve` — no edit to their repo.
