# Gated Spec Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in "gated" spec-generation mode to spec-runner with file-based frontmatter state (draft/approved/stale), a hard per-stage validation gate, human approval checkpoints, and rich single-source templates.

**Architecture:** State lives in YAML frontmatter on each spec file (`requirements.md`, `design.md`, `tasks.md`), managed by a new `spec.py` module (atomic writes under FileLock). A new `spec` subcommand family and a `plan --gated` branch drive the flow one stage at a time; a `run` gate blocks execution of unapproved `tasks.md` when `spec_governance: strict`. Everything is backward-compatible: default governance is `off`, and files without frontmatter are "unmanaged" and behave exactly as today.

**Tech Stack:** Python 3.10+, dataclasses, PyYAML, `fcntl` file locking (existing `ExecutorLock`), `importlib.resources`, `hashlib`, pytest (mock CLI, `@pytest.mark.slow` for e2e). Ruff line length 100, mypy strict.

## Global Constraints

- Python 3.10+; type annotations required everywhere; mypy strict mode.
- Ruff line length **100** (E501 ignored); rules E, F, W, I, UP, B, C4, SIM.
- Follow existing patterns: dataclasses like `ExecutorConfig`/`ValidationResult`, `get_logger("...")` for logging, argparse subcommands in `cli.py:_build_parser()`.
- **Backward compatibility is an invariant:** default `spec_governance: off`; files without frontmatter are "unmanaged" and never blocked. All existing tests must stay green.
- `generated_by`/`approved_by` use the ecosystem agent-id convention `<harness>@<model>` (e.g. `claude@claude-opus-4-8`).
- `source_prompt_version` is a template **content hash** (`sha256:<hex>`), never a hand-maintained integer.
- All frontmatter mutations are **atomic** (temp file + `os.replace`) and serialized under the existing `ExecutorLock`.
- New tests marked `@pytest.mark.slow` only for subprocess/e2e; unit tests mock CLI.

---

## File Structure

**New files:**
- `src/spec_runner/spec.py` — `SpecMeta` dataclass, frontmatter parse/split/strip, `read_spec_meta`, atomic locked `write_spec_meta`, `STAGES`, `downstream_stages`, `resolve_next_stage`, `mark_downstream_stale`, `apply_approval`.
- `src/spec_runner/spec_commands.py` — `spec status/approve/reject/adopt/check` command handlers + `run_checkpoint_menu` (TTY overlay).
- Tests: `tests/test_spec_meta.py`, `tests/test_spec_commands.py`, `tests/test_spec_lock.py`, `tests/test_adopt_gate.py`, `tests/test_gated_plan.py`, `tests/test_run_gate.py`, `tests/test_source_prompt_version.py`.

**Modified files:**
- `src/spec_runner/prompt.py` — `load_bundled_template`, `template_hash`, `build_gated_generation_prompt`.
- `src/spec_runner/validate.py` — `validate_requirements`, `validate_design`, `verdict_from_result`, `validate_spec_stage`.
- `src/spec_runner/task.py` — `parse_tasks` (and sibling readers) strip leading frontmatter.
- `src/spec_runner/config.py` — `spec_governance` field + YAML loading + `spec_lock_file` property.
- `src/spec_runner/cli_plan.py` — `--gated`/`--stage` branch in `cmd_plan`.
- `src/spec_runner/cli.py` — run gate in `_run_tasks`; `spec` subparser; `--gated/--stage` and `--strict/--no-strict` flags.

**Responsibility boundaries:** `spec.py` owns *state* (read/write/transitions, no CLI, no LLM). `spec_commands.py` owns *commands* (CLI I/O + TTY). `validate.py` owns *validators*. `prompt.py` owns *prompt assembly*. This keeps each file focused and independently testable.

---

## Task 1: Frontmatter core — `SpecMeta`, parse/split/strip

**Files:**
- Create: `src/spec_runner/spec.py`
- Test: `tests/test_spec_meta.py`

**Interfaces:**
- Produces:
  - `SpecMeta` dataclass with fields: `spec_stage: str`, `status: str = "draft"`, `version: int = 1`, `generated_by: str = ""`, `generated_at: str = ""`, `source_prompt_version: str = ""`, `validation: str = ""`, `approved_by: str | None = None`, `approved_at: str | None = None`.
  - `STAGES: tuple[str, str, str] = ("requirements", "design", "tasks")`
  - `split_frontmatter(text: str) -> tuple[dict | None, str]` — `(meta_dict_or_None, body)`.
  - `strip_frontmatter(text: str) -> str` — body only.
  - `meta_from_dict(d: dict) -> SpecMeta`, `meta_to_dict(m: SpecMeta) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec_meta.py
from spec_runner.spec import (
    SpecMeta,
    split_frontmatter,
    strip_frontmatter,
    meta_from_dict,
    meta_to_dict,
)

FM = """---
spec_stage: requirements
status: draft
version: 1
validation: pass
---
# Requirements
body line
"""


def test_split_frontmatter_extracts_meta_and_body():
    meta, body = split_frontmatter(FM)
    assert meta is not None
    assert meta["spec_stage"] == "requirements"
    assert meta["version"] == 1
    assert body.startswith("# Requirements")


def test_split_frontmatter_none_when_absent():
    meta, body = split_frontmatter("# Just a doc\nno frontmatter")
    assert meta is None
    assert body == "# Just a doc\nno frontmatter"


def test_strip_frontmatter_returns_body_only():
    assert strip_frontmatter(FM).startswith("# Requirements")
    assert "spec_stage" not in strip_frontmatter(FM)


def test_strip_frontmatter_noop_without_frontmatter():
    text = "# No FM\nline"
    assert strip_frontmatter(text) == text


def test_meta_roundtrip():
    m = SpecMeta(spec_stage="design", status="approved", version=3)
    d = meta_to_dict(m)
    m2 = meta_from_dict(d)
    assert m2 == m
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spec_meta.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spec_runner.spec'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/spec_runner/spec.py
"""Spec lifecycle state: frontmatter parsing, status, atomic locked writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

STAGES: tuple[str, str, str] = ("requirements", "design", "tasks")

_FM_DELIM = "---"


@dataclass
class SpecMeta:
    """Frontmatter state for one spec document."""

    spec_stage: str
    status: str = "draft"  # draft | approved | stale
    version: int = 1
    generated_by: str = ""
    generated_at: str = ""
    source_prompt_version: str = ""
    validation: str = ""  # pass | fail | warn | ""
    approved_by: str | None = None
    approved_at: str | None = None


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a leading ``---\\n...\\n---`` YAML block from the body.

    Returns ``(meta_dict, body)`` or ``(None, text)`` when no frontmatter.
    """
    if not text.startswith(_FM_DELIM + "\n"):
        return None, text
    end = text.find("\n" + _FM_DELIM, len(_FM_DELIM) + 1)
    if end == -1:
        return None, text
    raw = text[len(_FM_DELIM) + 1 : end]
    # Body starts after the closing delimiter's line.
    after = text.find("\n", end + 1)
    body = text[after + 1 :] if after != -1 else ""
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, text
    if not isinstance(loaded, dict):
        return None, text
    return loaded, body


def strip_frontmatter(text: str) -> str:
    """Return the document body with any leading frontmatter removed."""
    _, body = split_frontmatter(text)
    return body


def meta_from_dict(d: dict) -> SpecMeta:
    """Build a SpecMeta from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(SpecMeta)}
    return SpecMeta(**{k: v for k, v in d.items() if k in known})


def meta_to_dict(m: SpecMeta) -> dict:
    """Serialize a SpecMeta to a plain dict (frontmatter order)."""
    return asdict(m)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spec_meta.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta.py
git commit -m "feat(spec): SpecMeta + frontmatter split/strip"
```

---

## Task 2: Atomic locked read/write of spec files

**Files:**
- Modify: `src/spec_runner/spec.py`
- Test: `tests/test_spec_meta.py` (extend), `tests/test_spec_lock.py`

**Interfaces:**
- Consumes: `SpecMeta`, `split_frontmatter`, `meta_from_dict`/`meta_to_dict` (Task 1); `ExecutorLock` from `config.py:34`.
- Produces:
  - `read_spec_meta(path: Path) -> SpecMeta | None` — `None` if file missing or unmanaged (no frontmatter).
  - `read_spec_body(path: Path) -> str` — body with frontmatter stripped (empty string if missing).
  - `write_spec(path: Path, meta: SpecMeta, body: str, lock: "ExecutorLock | None" = None) -> None` — atomic (temp + `os.replace`); acquires `lock` around the write if provided.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_spec_meta.py
from pathlib import Path
from spec_runner.spec import read_spec_meta, read_spec_body, write_spec, SpecMeta


def test_write_then_read_roundtrip(tmp_path: Path):
    p = tmp_path / "requirements.md"
    write_spec(p, SpecMeta(spec_stage="requirements", version=2), "# Body\ntext\n")
    meta = read_spec_meta(p)
    assert meta is not None and meta.version == 2 and meta.spec_stage == "requirements"
    assert read_spec_body(p).startswith("# Body")


def test_read_meta_none_for_unmanaged(tmp_path: Path):
    p = tmp_path / "tasks.md"
    p.write_text("# Tasks\nno frontmatter\n")
    assert read_spec_meta(p) is None


def test_read_meta_none_for_missing(tmp_path: Path):
    assert read_spec_meta(tmp_path / "nope.md") is None


def test_write_is_atomic_no_partial_on_replace(tmp_path: Path, monkeypatch):
    # Simulate os.replace failing: the original file must remain intact.
    p = tmp_path / "design.md"
    write_spec(p, SpecMeta(spec_stage="design", version=1), "original\n")
    import spec_runner.spec as specmod

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(specmod.os, "replace", boom)
    try:
        write_spec(p, SpecMeta(spec_stage="design", version=9), "new body\n")
    except OSError:
        pass
    # Original content preserved; no temp file left behind.
    assert read_spec_meta(p).version == 1
    assert not any(x.name.startswith(".design.md.") for x in tmp_path.iterdir())
```

```python
# tests/test_spec_lock.py
from pathlib import Path
from spec_runner.config import ExecutorLock
from spec_runner.spec import write_spec, read_spec_meta, SpecMeta


def test_write_under_lock_serializes(tmp_path: Path):
    p = tmp_path / "requirements.md"
    lock = ExecutorLock(tmp_path / ".spec.lock")
    write_spec(p, SpecMeta(spec_stage="requirements", version=1), "a\n", lock=lock)
    # Lock must be released after write (re-acquire succeeds).
    assert lock.acquire() is True
    lock.release()
    assert read_spec_meta(p).version == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spec_meta.py::test_write_then_read_roundtrip tests/test_spec_lock.py -v`
Expected: FAIL with `ImportError: cannot import name 'write_spec'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/spec.py
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ExecutorLock


def _render(meta: SpecMeta, body: str) -> str:
    fm = yaml.safe_dump(meta_to_dict(meta), sort_keys=False).rstrip("\n")
    return f"{_FM_DELIM}\n{fm}\n{_FM_DELIM}\n{body}"


def read_spec_meta(path: Path) -> SpecMeta | None:
    """Return SpecMeta, or None if the file is missing or unmanaged."""
    if not path.exists():
        return None
    meta_dict, _ = split_frontmatter(path.read_text())
    if meta_dict is None:
        return None
    return meta_from_dict(meta_dict)


def read_spec_body(path: Path) -> str:
    """Return the document body (frontmatter stripped); '' if missing."""
    if not path.exists():
        return ""
    return strip_frontmatter(path.read_text())


def write_spec(
    path: Path,
    meta: SpecMeta,
    body: str,
    lock: "ExecutorLock | None" = None,
) -> None:
    """Atomically write frontmatter + body, optionally under a file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    if lock is not None:
        acquired = lock.acquire()
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_render(meta, body))
            os.replace(tmp, str(path))
        except BaseException:
            with __import__("contextlib").suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
    finally:
        if lock is not None and acquired:
            lock.release()
```

Note: keep imports at module top per ruff (move `import os`, `import tempfile`, `import contextlib` to the top of the file; the inline `__import__` above is only illustrative — replace with a top-level `import contextlib` and `contextlib.suppress`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spec_meta.py tests/test_spec_lock.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta.py tests/test_spec_lock.py
git commit -m "feat(spec): atomic locked read/write of spec frontmatter"
```

---

## Task 3: `parse_tasks` ignores frontmatter (backward-compat)

**Files:**
- Modify: `src/spec_runner/task.py` (readers at lines 64, 202, 252, 286)
- Test: `tests/test_task.py` (extend)

**Interfaces:**
- Consumes: `strip_frontmatter` (Task 1).
- Produces: no new signatures; `parse_tasks` behavior unchanged for frontmatter-less files, and correct for files with frontmatter.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_task.py
from pathlib import Path
from spec_runner.task import parse_tasks

TASKS_WITH_FM = """---
spec_stage: tasks
status: approved
version: 2
---
## Milestone M1

### TASK-001: First
🔴 P0 | ⬜ TODO | Est: 1d
"""


def test_parse_tasks_ignores_frontmatter(tmp_path: Path):
    p = tmp_path / "tasks.md"
    p.write_text(TASKS_WITH_FM)
    tasks = parse_tasks(p)
    assert [t.id for t in tasks] == ["TASK-001"]
    assert tasks[0].name == "First"


def test_parse_tasks_without_frontmatter_unchanged(tmp_path: Path):
    p = tmp_path / "tasks.md"
    p.write_text("### TASK-009: Solo\n🔴 P0 | ⬜ TODO | Est: 1d\n")
    tasks = parse_tasks(p)
    assert [t.id for t in tasks] == ["TASK-009"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_task.py::test_parse_tasks_ignores_frontmatter -v`
Expected: FAIL — the task is parsed but the frontmatter's `### `-less lines / `---` confuse milestone/description capture, or (depending on parser) `TASK-001` still found but `spec_stage:` leaks into description. Assertion on `tasks[0].name`/count fails.

- [ ] **Step 3: Write minimal implementation**

In `src/spec_runner/task.py`, add the import and strip content in each reader. At the top:

```python
from .spec import strip_frontmatter
```

Then change each `content = filepath.read_text()` (lines 64, 202, 252, 286) to:

```python
    content = strip_frontmatter(filepath.read_text())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_task.py -v`
Expected: PASS (existing task tests + 2 new)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/task.py tests/test_task.py
git commit -m "feat(task): parse_tasks strips leading frontmatter (backward-compat)"
```

---

## Task 4: Bundled template loading + content hash

**Files:**
- Modify: `src/spec_runner/prompt.py`
- Test: `tests/test_source_prompt_version.py`

**Interfaces:**
- Produces:
  - `load_bundled_template(stage: str) -> str` — reads `skills/spec-generator-skill/templates/<stage>.template.md` via `importlib.resources`.
  - `template_hash(stage: str) -> str` — `"sha256:<hex>"` of the template content.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_source_prompt_version.py
from spec_runner.prompt import load_bundled_template, template_hash


def test_load_bundled_template_has_sections():
    text = load_bundled_template("requirements")
    assert text.strip()
    # Rich template, not a 3-line stub.
    assert len(text) > 200


def test_template_hash_is_sha256_prefixed_and_stable():
    h1 = template_hash("design")
    h2 = template_hash("design")
    assert h1.startswith("sha256:")
    assert h1 == h2


def test_template_hash_differs_by_stage():
    assert template_hash("requirements") != template_hash("tasks")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_source_prompt_version.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_bundled_template'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/prompt.py
import hashlib
from importlib import resources

_TEMPLATE_PKG = "spec_runner.skills.spec-generator-skill.templates"
_TEMPLATE_FILES = {
    "requirements": "requirements.template.md",
    "design": "design.template.md",
    "tasks": "tasks.template.md",
}


def load_bundled_template(stage: str) -> str:
    """Load the bundled rich template for a stage (importlib.resources)."""
    fname = _TEMPLATE_FILES[stage]
    return (
        resources.files("spec_runner")
        .joinpath("skills", "spec-generator-skill", "templates", fname)
        .read_text(encoding="utf-8")
    )


def template_hash(stage: str) -> str:
    """Return 'sha256:<hex>' content hash of the stage template."""
    digest = hashlib.sha256(load_bundled_template(stage).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
```

Note: confirm the templates ship inside the wheel. If `pyproject.toml` does not already include `skills/**` as package data, add it (check `[tool.setuptools.package-data]` / `[tool.hatch.build]`). `init_cmd.py` already installs these templates, so they are packaged — verify the resource path resolves in an installed build during Step 4.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_source_prompt_version.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/prompt.py tests/test_source_prompt_version.py
git commit -m "feat(prompt): load bundled rich templates + content-hash versioning"
```

---

## Task 5: `build_gated_generation_prompt`

**Files:**
- Modify: `src/spec_runner/prompt.py`
- Test: `tests/test_prompt.py` (extend)

**Interfaces:**
- Consumes: `load_bundled_template` (Task 4); existing `SPEC_STAGES` markers (`prompt.py:17`).
- Produces:
  - `build_gated_generation_prompt(stage: str, description: str, context: dict[str, str]) -> str` — role instructions + full template body + description + prior approved stages + `SPEC_<STAGE>_READY/END` markers.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_prompt.py
from spec_runner.prompt import build_gated_generation_prompt


def test_gated_prompt_embeds_template_and_markers():
    p = build_gated_generation_prompt("requirements", "Build a widget", {})
    assert "Build a widget" in p
    assert "SPEC_REQUIREMENTS_READY" in p
    assert "SPEC_REQUIREMENTS_END" in p
    # Template body is embedded (rich, not the 3-line stub).
    assert len(p) > 500


def test_gated_prompt_design_includes_prior_requirements():
    ctx = {"requirements": "REQ-001 the widget spins"}
    p = build_gated_generation_prompt("design", "Build a widget", ctx)
    assert "REQ-001 the widget spins" in p
    assert "SPEC_DESIGN_READY" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prompt.py::test_gated_prompt_embeds_template_and_markers -v`
Expected: FAIL with `ImportError: cannot import name 'build_gated_generation_prompt'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/prompt.py
_PRIOR_FOR = {
    "requirements": [],
    "design": ["requirements"],
    "tasks": ["requirements", "design"],
}


def build_gated_generation_prompt(
    stage: str, description: str, context: dict[str, str]
) -> str:
    """Build a rich, template-driven generation prompt for one gated stage."""
    marker = SPEC_STAGES[stage]["marker"]
    template = load_bundled_template(stage)

    prior_parts = []
    for prior in _PRIOR_FOR[stage]:
        if context.get(prior):
            prior_parts.append(f"## Approved {prior}\n\n{context[prior]}")
    prior_block = "\n\n".join(prior_parts)

    return (
        f"You are generating the '{stage}' spec document. Fill the TEMPLATE below "
        f"from the DESCRIPTION and any approved upstream stages. Do not invent or "
        f"drop sections. Out of Scope is mandatory; acceptance criteria use "
        f"GIVEN-WHEN-THEN; add [REQ-XXX]/[DESIGN-XXX] traceability where the "
        f"template calls for it.\n\n"
        f"## DESCRIPTION\n\n{description}\n\n"
        f"{prior_block}\n\n" if prior_block else
        f"You are generating the '{stage}' spec document. Fill the TEMPLATE below "
        f"from the DESCRIPTION. Do not invent or drop sections. Out of Scope is "
        f"mandatory; acceptance criteria use GIVEN-WHEN-THEN.\n\n"
        f"## DESCRIPTION\n\n{description}\n\n"
    ) + (
        f"## TEMPLATE\n\n{template}\n\n"
        f"When done, output ONLY the finished document between markers:\n"
        f"{marker}_READY\n<document>\n{marker}_END\n"
    )
```

Note: the branch above is awkward as one expression — implement it as a normal `if prior_block:` block that builds `header` then returns `header + template_block`. Keep the two marker strings (`{marker}_READY`, `{marker}_END`) consistent with `parse_spec_marker` (`prompt.py:234`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prompt.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/prompt.py tests/test_prompt.py
git commit -m "feat(prompt): build_gated_generation_prompt (template-driven, staged)"
```

---

## Task 6: Per-stage validators + verdict

**Files:**
- Modify: `src/spec_runner/validate.py`
- Test: `tests/test_validate.py` (extend)

**Interfaces:**
- Consumes: `ValidationResult` (`validate.py:27`), `strip_frontmatter` (Task 1).
- Produces:
  - `validate_requirements(path: Path) -> ValidationResult` — unique `[REQ-XXX]`; every REQ has acceptance criteria; an "Out of Scope" heading present.
  - `validate_design(path: Path) -> ValidationResult` — unique `[DESIGN-XXX]`; every `traces to [REQ-XXX]` references a REQ that exists in the sibling requirements file (dangling-ref check).
  - `verdict_from_result(result: ValidationResult) -> str` — `"fail"` if errors, `"warn"` if only warnings, else `"pass"`.
  - `validate_spec_stage(stage: str, config: "ExecutorConfig") -> ValidationResult` — dispatch to the right validator using `config.requirements_file/design_file/tasks_file`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_validate.py
from pathlib import Path
from spec_runner.validate import (
    validate_requirements,
    validate_design,
    verdict_from_result,
    ValidationResult,
)

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


def test_validate_requirements_ok(tmp_path: Path):
    p = tmp_path / "requirements.md"
    p.write_text(GOOD_REQ)
    assert validate_requirements(p).ok


def test_validate_requirements_missing_out_of_scope(tmp_path: Path):
    p = tmp_path / "requirements.md"
    p.write_text(BAD_REQ_NO_SCOPE)
    r = validate_requirements(p)
    assert not r.ok
    assert any("Out of Scope" in e for e in r.errors)


def test_validate_design_dangling_req(tmp_path: Path):
    (tmp_path / "requirements.md").write_text(GOOD_REQ)
    design = tmp_path / "design.md"
    design.write_text("### DESIGN-001: C\ntraces to [REQ-999]\n")
    r = validate_design(design)
    assert not r.ok
    assert any("REQ-999" in e for e in r.errors)


def test_verdict_levels():
    ok = ValidationResult()
    assert verdict_from_result(ok) == "pass"
    warn = ValidationResult(warnings=["w"])
    assert verdict_from_result(warn) == "warn"
    fail = ValidationResult(errors=["e"])
    assert verdict_from_result(fail) == "fail"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validate.py::test_validate_requirements_ok -v`
Expected: FAIL with `ImportError: cannot import name 'validate_requirements'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/validate.py
import re
from pathlib import Path
from .spec import strip_frontmatter

_REQ_ID = re.compile(r"\bREQ-\d+\b")
_DESIGN_ID = re.compile(r"\bDESIGN-\d+\b")
_REQ_HEADING = re.compile(r"^#+\s*REQ-(\d+)\b", re.MULTILINE)
_DESIGN_HEADING = re.compile(r"^#+\s*DESIGN-(\d+)\b", re.MULTILINE)


def verdict_from_result(result: ValidationResult) -> str:
    """Map a ValidationResult to 'fail' | 'warn' | 'pass'."""
    if result.errors:
        return "fail"
    if result.warnings:
        return "warn"
    return "pass"


def validate_requirements(path: Path) -> ValidationResult:
    """Validate a requirements doc: unique REQ ids, acceptance criteria, scope."""
    result = ValidationResult()
    body = strip_frontmatter(path.read_text())

    ids = _REQ_HEADING.findall(body)
    seen: set[str] = set()
    for rid in ids:
        if rid in seen:
            result.errors.append(f"REQ-{rid}: duplicate requirement ID")
        seen.add(rid)
    if not ids:
        result.errors.append("no REQ-XXX requirements found")
    if "out of scope" not in body.lower():
        result.errors.append("missing 'Out of Scope' section")
    if "acceptance criteria" not in body.lower():
        result.warnings.append("no 'Acceptance Criteria' found")
    return result


def validate_design(path: Path) -> ValidationResult:
    """Validate a design doc: unique DESIGN ids, no dangling REQ references."""
    result = ValidationResult()
    body = strip_frontmatter(path.read_text())

    ids = _DESIGN_HEADING.findall(body)
    seen: set[str] = set()
    for did in ids:
        if did in seen:
            result.errors.append(f"DESIGN-{did}: duplicate design ID")
        seen.add(did)
    if not ids:
        result.errors.append("no DESIGN-XXX components found")

    req_path = path.parent / path.name.replace("design.md", "requirements.md")
    known_reqs: set[str] = set()
    if req_path.exists():
        known_reqs = set(_REQ_ID.findall(strip_frontmatter(req_path.read_text())))
    for ref in _REQ_ID.findall(body):
        if known_reqs and ref not in known_reqs:
            result.errors.append(f"design references unknown {ref}")
    return result


def validate_spec_stage(stage: str, config) -> ValidationResult:
    """Dispatch stage validation using config's stage file paths."""
    if stage == "requirements":
        return validate_requirements(config.requirements_file)
    if stage == "design":
        return validate_design(config.design_file)
    if stage == "tasks":
        return validate_tasks(config.tasks_file)
    raise ValueError(f"unknown stage: {stage}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validate.py -v`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/validate.py tests/test_validate.py
git commit -m "feat(validate): per-stage validators + verdict mapping"
```

---

## Task 7: Status machine — next-stage, approval, stale cascade

**Files:**
- Modify: `src/spec_runner/spec.py`
- Test: `tests/test_spec_meta.py` (extend)

**Interfaces:**
- Consumes: `SpecMeta`, `read_spec_meta`, `read_spec_body`, `write_spec` (Tasks 1–2); `STAGES`.
- Produces:
  - `downstream_stages(stage: str) -> list[str]` — stages after `stage` in `STAGES`.
  - `resolve_next_stage(metas: dict[str, SpecMeta | None]) -> tuple[str, str]` — returns `(action, stage)` where `action ∈ {"generate", "await_approval", "stale", "done"}`.
  - `apply_approval(config, stage: str, approver: str, now: str, fresh_validation: str) -> None` — sets `approved`, bumps `version`, sets `approved_by/at`, refreshes `validation`, and marks downstream stages `stale` (the cascade). Writes via `write_spec` under `config`'s spec lock.
  - `stage_path(config, stage) -> Path` — helper mapping stage → file path.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_spec_meta.py
from spec_runner.spec import downstream_stages, resolve_next_stage


def test_downstream_stages():
    assert downstream_stages("requirements") == ["design", "tasks"]
    assert downstream_stages("tasks") == []


def _m(stage, status):
    return SpecMeta(spec_stage=stage, status=status)


def test_resolve_next_stage_table():
    # nothing yet -> generate requirements
    assert resolve_next_stage({"requirements": None, "design": None, "tasks": None}) == (
        "generate",
        "requirements",
    )
    # requirements draft -> await approval
    assert resolve_next_stage(
        {"requirements": _m("requirements", "draft"), "design": None, "tasks": None}
    ) == ("await_approval", "requirements")
    # requirements approved, design missing -> generate design
    assert resolve_next_stage(
        {"requirements": _m("requirements", "approved"), "design": None, "tasks": None}
    ) == ("generate", "design")
    # all approved -> done
    assert resolve_next_stage(
        {
            "requirements": _m("requirements", "approved"),
            "design": _m("design", "approved"),
            "tasks": _m("tasks", "approved"),
        }
    ) == ("done", "tasks")
    # a stale stage takes priority
    assert resolve_next_stage(
        {
            "requirements": _m("requirements", "approved"),
            "design": _m("design", "stale"),
            "tasks": _m("tasks", "approved"),
        }
    ) == ("stale", "design")
```

```python
# approval cascade test (uses a tiny config stub)
from pathlib import Path
from spec_runner.spec import apply_approval, read_spec_meta, write_spec, SpecMeta


class _Cfg:
    def __init__(self, root: Path):
        self.project_root = root
        self._spec = root / "spec"

    @property
    def requirements_file(self):
        return self._spec / "requirements.md"

    @property
    def design_file(self):
        return self._spec / "design.md"

    @property
    def tasks_file(self):
        return self._spec / "tasks.md"

    @property
    def spec_lock_file(self):
        return self._spec / ".spec.lock"


def test_apply_approval_bumps_and_cascades_stale(tmp_path: Path):
    cfg = _Cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved", version=1), "r\n")
    write_spec(cfg.design_file, SpecMeta("design", "approved", version=1), "d\n")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "approved", version=1), "t\n")

    # Re-approve requirements (version 1 -> 2) must cascade stale downstream.
    apply_approval(cfg, "requirements", approver="tester", now="2026-07-01T00:00:00Z", fresh_validation="pass")

    assert read_spec_meta(cfg.requirements_file).version == 2
    assert read_spec_meta(cfg.requirements_file).status == "approved"
    assert read_spec_meta(cfg.design_file).status == "stale"
    assert read_spec_meta(cfg.tasks_file).status == "stale"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spec_meta.py::test_downstream_stages -v`
Expected: FAIL with `ImportError: cannot import name 'downstream_stages'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/spec.py
def downstream_stages(stage: str) -> list[str]:
    """Stages strictly after `stage` in canonical order."""
    i = STAGES.index(stage)
    return list(STAGES[i + 1 :])


def resolve_next_stage(metas: dict[str, "SpecMeta | None"]) -> tuple[str, str]:
    """Compute (action, stage) from current per-stage metas."""
    # A stale stage anywhere takes priority.
    for stage in STAGES:
        m = metas.get(stage)
        if m is not None and m.status == "stale":
            return ("stale", stage)
    for stage in STAGES:
        m = metas.get(stage)
        if m is None:
            return ("generate", stage)
        if m.status == "draft":
            return ("await_approval", stage)
    return ("done", STAGES[-1])


def stage_path(config, stage: str) -> Path:
    """Map a stage name to its spec file path on `config`."""
    return {
        "requirements": config.requirements_file,
        "design": config.design_file,
        "tasks": config.tasks_file,
    }[stage]


def apply_approval(
    config,
    stage: str,
    approver: str,
    now: str,
    fresh_validation: str,
) -> None:
    """Approve a stage: bump version, record approver, cascade stale downstream."""
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        raise ValueError(f"{stage} is unmanaged (no frontmatter)")
    lock = _spec_lock(config)
    meta.status = "approved"
    meta.version += 1
    meta.approved_by = approver
    meta.approved_at = now
    meta.validation = fresh_validation
    write_spec(path, meta, read_spec_body(path), lock=lock)
    # Cascade: any downstream approved/draft stage becomes stale.
    for ds in downstream_stages(stage):
        ds_path = stage_path(config, ds)
        ds_meta = read_spec_meta(ds_path)
        if ds_meta is not None and ds_meta.status != "stale":
            ds_meta.status = "stale"
            write_spec(ds_path, ds_meta, read_spec_body(ds_path), lock=lock)


def _spec_lock(config):
    from .config import ExecutorLock

    return ExecutorLock(config.spec_lock_file)
```

Note: `config.spec_lock_file` is added in Task 9. For this task's unit test the `_Cfg` stub provides it; the real property lands in Task 9.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spec_meta.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta.py
git commit -m "feat(spec): next-stage resolution + approval with stale cascade"
```

---

## Task 8: `spec` commands — status/approve/reject/adopt/check

**Files:**
- Create: `src/spec_runner/spec_commands.py`
- Test: `tests/test_spec_commands.py`, `tests/test_adopt_gate.py`

**Interfaces:**
- Consumes: `read_spec_meta`, `read_spec_body`, `write_spec`, `apply_approval`, `resolve_next_stage`, `stage_path`, `SpecMeta`, `STAGES` (Tasks 1–2, 7); `validate_spec_stage`, `verdict_from_result` (Task 6); config stage-file properties + `spec_lock_file` (Task 9).
- Produces:
  - `cmd_spec_status(args, config) -> int`
  - `cmd_spec_approve(args, config) -> int` — **re-validates the current body**, blocks on `fail`, else `apply_approval`.
  - `cmd_spec_reject(args, config) -> int` — sets status back to `draft`.
  - `cmd_spec_adopt(args, config) -> int` — validate first; pass→approved, fail→draft (or `--force`→approved+warning).
  - `cmd_spec_check(args, config) -> int` — refresh cached `validation`.
  - `_now() -> str`, `_approver() -> str` (git user.name; `"unknown"` fallback).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec_commands.py
from pathlib import Path
from types import SimpleNamespace
import pytest
from spec_runner.spec import write_spec, read_spec_meta, SpecMeta
from spec_runner import spec_commands


def _cfg(tmp_path: Path):
    spec = tmp_path / "spec"
    return SimpleNamespace(
        project_root=tmp_path,
        requirements_file=spec / "requirements.md",
        design_file=spec / "design.md",
        tasks_file=spec / "tasks.md",
        spec_lock_file=spec / ".spec.lock",
    )


GOOD_REQ = """# Requirements

## Out of Scope
- none

#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""


def test_approve_blocks_on_validation_fail(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), "no scope here\n")
    args = SimpleNamespace(stage="requirements", force=False)
    rc = spec_commands.cmd_spec_approve(args, cfg)
    assert rc != 0
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_approve_revalidates_ignoring_stale_cache_toctou(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # Cached validation says pass, but the body is actually invalid now.
    write_spec(
        cfg.requirements_file,
        SpecMeta("requirements", "draft", validation="pass"),
        "no scope here\n",
    )
    args = SimpleNamespace(stage="requirements", force=False)
    rc = spec_commands.cmd_spec_approve(args, cfg)
    assert rc != 0  # re-validation caught it despite the stale 'pass'
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_approve_succeeds_on_valid(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    args = SimpleNamespace(stage="requirements", force=False)
    rc = spec_commands.cmd_spec_approve(args, cfg)
    assert rc == 0
    m = read_spec_meta(cfg.requirements_file)
    assert m.status == "approved" and m.version == 2


def test_reject_returns_to_draft(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved", version=3), GOOD_REQ)
    args = SimpleNamespace(stage="requirements")
    assert spec_commands.cmd_spec_reject(args, cfg) == 0
    assert read_spec_meta(cfg.requirements_file).status == "draft"
```

```python
# tests/test_adopt_gate.py
from pathlib import Path
from types import SimpleNamespace
from spec_runner.spec import read_spec_meta
from spec_runner import spec_commands
from tests.test_spec_commands import _cfg, GOOD_REQ  # reuse helpers


def test_adopt_invalid_file_becomes_draft(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text("no scope, unmanaged\n")
    args = SimpleNamespace(stage="requirements", force=False)
    spec_commands.cmd_spec_adopt(args, cfg)
    assert read_spec_meta(cfg.requirements_file).status == "draft"


def test_adopt_force_invalid_becomes_approved(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text("no scope, unmanaged\n")
    args = SimpleNamespace(stage="requirements", force=True)
    spec_commands.cmd_spec_adopt(args, cfg)
    assert read_spec_meta(cfg.requirements_file).status == "approved"


def test_adopt_valid_becomes_approved(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.requirements_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.requirements_file.write_text(GOOD_REQ)
    args = SimpleNamespace(stage="requirements", force=False)
    spec_commands.cmd_spec_adopt(args, cfg)
    assert read_spec_meta(cfg.requirements_file).status == "approved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spec_commands.py tests/test_adopt_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spec_runner.spec_commands'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/spec_runner/spec_commands.py
"""`spec` subcommands: status, approve, reject, adopt, check."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from .logging import get_logger
from .spec import (
    STAGES,
    SpecMeta,
    apply_approval,
    read_spec_body,
    read_spec_meta,
    resolve_next_stage,
    stage_path,
    write_spec,
)
from .validate import validate_spec_stage, verdict_from_result

logger = get_logger("spec")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _approver() -> str:
    try:
        out = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _metas(config) -> dict[str, SpecMeta | None]:
    return {s: read_spec_meta(stage_path(config, s)) for s in STAGES}


def cmd_spec_status(args, config) -> int:
    metas = _metas(config)
    for stage in STAGES:
        m = metas[stage]
        if m is None:
            print(f"{stage:12} —        unmanaged")
        else:
            print(f"{stage:12} {m.status:8} v{m.version}  validation={m.validation or '?'}")
    action, stage = resolve_next_stage(metas)
    print(f"\nnext: {action} → {stage}")
    return 0


def cmd_spec_approve(args, config) -> int:
    stage = args.stage
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        print(f"{stage}: unmanaged (no frontmatter); run `spec adopt` first")
        return 2
    # Always re-validate the current body — never trust the cached field (TOCTOU).
    result = validate_spec_stage(stage, config)
    verdict = verdict_from_result(result)
    if verdict == "fail":
        print(f"{stage}: validation FAILED — not approved:")
        for e in result.errors:
            print(f"  - {e}")
        return 1
    apply_approval(config, stage, approver=_approver(), now=_now(), fresh_validation=verdict)
    print(f"{stage}: approved (v{read_spec_meta(path).version})")
    return 0


def cmd_spec_reject(args, config) -> int:
    stage = args.stage
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        print(f"{stage}: unmanaged")
        return 2
    meta.status = "draft"
    write_spec(path, meta, read_spec_body(path))
    print(f"{stage}: re-opened as draft")
    return 0


def cmd_spec_adopt(args, config) -> int:
    stage = args.stage
    path = stage_path(config, stage)
    if not path.exists():
        print(f"{stage}: no file to adopt at {path}")
        return 2
    body = read_spec_body(path)  # strips FM if somehow present
    result = validate_spec_stage(stage, config)
    verdict = verdict_from_result(result)
    if verdict == "fail" and not getattr(args, "force", False):
        status = "draft"
        print(f"{stage}: validation failed → adopted as DRAFT (fix + approve)")
    else:
        status = "approved"
        if verdict == "fail":
            logger.warning("adopt_force_invalid", stage=stage, errors=len(result.errors))
            print(f"⚠️  {stage}: adopting INVALID spec as approved (--force)")
    meta = SpecMeta(
        spec_stage=stage,
        status=status,
        version=1,
        validation=verdict,
        approved_by=_approver() if status == "approved" else None,
        approved_at=_now() if status == "approved" else None,
    )
    write_spec(path, meta, body)
    print(f"{stage}: adopted ({status})")
    return 0


def cmd_spec_check(args, config) -> int:
    stage = args.stage
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        print(f"{stage}: unmanaged")
        return 2
    verdict = verdict_from_result(validate_spec_stage(stage, config))
    meta.validation = verdict
    write_spec(path, meta, read_spec_body(path))
    print(f"{stage}: validation={verdict}")
    return 0 if verdict != "fail" else 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spec_commands.py tests/test_adopt_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec_commands.py tests/test_spec_commands.py tests/test_adopt_gate.py
git commit -m "feat(spec): status/approve(re-validate)/reject/adopt(gated)/check commands"
```

---

## Task 9: Config — `spec_governance` + `spec_lock_file`

**Files:**
- Modify: `src/spec_runner/config.py`
- Test: `tests/test_config.py` (extend)

**Interfaces:**
- Produces:
  - `ExecutorConfig.spec_governance: str = "off"` (`"off" | "strict"`).
  - `ExecutorConfig.spec_lock_file` property → `project_root / "spec" / f".{spec_prefix}spec.lock"`.
  - YAML loading maps `executor_config.get("spec_governance")`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config.py
from pathlib import Path
from spec_runner.config import ExecutorConfig


def test_spec_governance_defaults_off():
    cfg = ExecutorConfig(project_root=Path("."))
    assert cfg.spec_governance == "off"


def test_spec_lock_file_path():
    cfg = ExecutorConfig(project_root=Path("."))
    assert cfg.spec_lock_file.name == ".spec.lock"
    assert cfg.spec_lock_file.parent.name == "spec"


def test_spec_governance_from_yaml(tmp_path: Path):
    from spec_runner.config import load_config_from_yaml

    cfg = tmp_path / "spec-runner.config.yaml"
    cfg.write_text("spec_governance: strict\n")
    loaded = load_config_from_yaml(cfg)
    assert loaded["spec_governance"] == "strict"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_spec_governance_defaults_off -v`
Expected: FAIL with `AttributeError: 'ExecutorConfig' object has no attribute 'spec_governance'`

- [ ] **Step 3: Write minimal implementation**

In `config.py`, add the field near other flags (after `audit_log_operator`, line ~215):

```python
    # Spec governance: "off" (default) | "strict" (gate run on approved tasks.md)
    spec_governance: str = "off"
```

Add the property near the other path properties (after `constitution_file`, line ~254):

```python
    @property
    def spec_lock_file(self) -> Path:
        return self.project_root / "spec" / f".{self.spec_prefix}spec.lock"
```

Add to the `load_config_from_yaml` return dict (near line 421):

```python
            "spec_governance": executor_config.get("spec_governance"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/config.py tests/test_config.py
git commit -m "feat(config): spec_governance flag + spec_lock_file path"
```

---

## Task 10: `run` gate — block unapproved `tasks.md` in strict mode

**Files:**
- Modify: `src/spec_runner/cli.py` (`_run_tasks`) and `_build_parser()` for `--strict/--no-strict`
- Test: `tests/test_run_gate.py`

**Interfaces:**
- Consumes: `read_spec_meta` (Task 2), `config.spec_governance`, `config.tasks_file` (Task 9).
- Produces:
  - `spec_run_gate_ok(config) -> tuple[bool, str]` in `cli.py` — `(allowed, reason)`. Allowed when governance != strict, or tasks.md is unmanaged, or its status == "approved". Blocked (with reason) when managed and status in {draft, stale}.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_gate.py
from pathlib import Path
from types import SimpleNamespace
from spec_runner.spec import write_spec, SpecMeta
from spec_runner.cli import spec_run_gate_ok


def _cfg(tmp_path, governance):
    spec = tmp_path / "spec"
    return SimpleNamespace(
        spec_governance=governance,
        tasks_file=spec / "tasks.md",
    )


def test_gate_off_always_allows(tmp_path: Path):
    cfg = _cfg(tmp_path, "off")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "x\n")
    ok, _ = spec_run_gate_ok(cfg)
    assert ok


def test_gate_strict_allows_unmanaged(tmp_path: Path):
    cfg = _cfg(tmp_path, "strict")
    cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.tasks_file.write_text("### TASK-001: x\n")  # no frontmatter
    ok, _ = spec_run_gate_ok(cfg)
    assert ok


def test_gate_strict_blocks_draft(tmp_path: Path):
    cfg = _cfg(tmp_path, "strict")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "x\n")
    ok, reason = spec_run_gate_ok(cfg)
    assert not ok and "draft" in reason.lower()


def test_gate_strict_allows_approved(tmp_path: Path):
    cfg = _cfg(tmp_path, "strict")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "approved"), "x\n")
    ok, _ = spec_run_gate_ok(cfg)
    assert ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'spec_run_gate_ok'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/cli.py
from .spec import read_spec_meta


def spec_run_gate_ok(config) -> tuple[bool, str]:
    """Return (allowed, reason). Blocks unapproved managed tasks.md in strict mode."""
    if getattr(config, "spec_governance", "off") != "strict":
        return True, ""
    meta = read_spec_meta(config.tasks_file)
    if meta is None:
        return True, ""  # unmanaged: backward-compatible
    if meta.status == "approved":
        return True, ""
    return False, (
        f"tasks.md is {meta.status} (v{meta.version}); "
        f"approve with `spec-runner spec approve tasks` or run with --no-strict"
    )
```

Wire it into `_run_tasks` (near the top, before executing tasks):

```python
    allowed, reason = spec_run_gate_ok(config)
    if not allowed:
        print(f"⛔ spec governance: {reason}")
        return
```

Add `--strict`/`--no-strict` to the `run` subparser in `_build_parser()` and honor them in `build_config` (mirror the existing `hitl_review` pattern): `--strict` → `spec_governance="strict"`, `--no-strict` → `spec_governance="off"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/cli.py tests/test_run_gate.py
git commit -m "feat(cli): run gate blocks unapproved tasks.md under strict governance"
```

---

## Task 11: `plan --gated` — generate one stage, validate, write DRAFT, stop

**Files:**
- Modify: `src/spec_runner/cli_plan.py` (`cmd_plan`), `src/spec_runner/cli.py` (`plan` subparser: `--gated`, `--stage`)
- Test: `tests/test_gated_plan.py`

**Interfaces:**
- Consumes: `build_gated_generation_prompt`, `template_hash`, `parse_spec_marker` (Tasks 4–5); `resolve_next_stage`, `read_spec_meta`, `read_spec_body`, `write_spec`, `SpecMeta`, `stage_path` (Tasks 1–2, 7); `validate_spec_stage`, `verdict_from_result` (Task 6); `build_cli_command` (`runner.py:359`).
- Produces:
  - `run_gated_stage(stage: str, description: str, config, invoke=subprocess.run) -> int` in `cli_plan.py` — generates one stage, writes DRAFT with frontmatter, runs validation, prints verdict, stops. `invoke` is injectable for tests (no real CLI).
  - `cmd_plan` dispatches to `run_gated_stage` when `args.gated` is set; `--stage` overrides the auto-resolved stage.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gated_plan.py
from pathlib import Path
from types import SimpleNamespace
from spec_runner.spec import read_spec_meta, write_spec, SpecMeta
from spec_runner.cli_plan import run_gated_stage


def _cfg(tmp_path: Path):
    spec = tmp_path / "spec"
    return SimpleNamespace(
        project_root=tmp_path,
        requirements_file=spec / "requirements.md",
        design_file=spec / "design.md",
        tasks_file=spec / "tasks.md",
        spec_lock_file=spec / ".spec.lock",
        claude_command="claude",
        claude_model="",
        command_template="",
        skip_permissions=True,
        task_timeout_minutes=1,
    )


GOOD_REQ_BODY = """# Requirements

## Out of Scope
- none

#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""


def _fake_invoke(output: str):
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    return _run


def test_gated_stage_writes_draft_with_frontmatter(tmp_path: Path):
    cfg = _cfg(tmp_path)
    out = f"SPEC_REQUIREMENTS_READY\n{GOOD_REQ_BODY}\nSPEC_REQUIREMENTS_END\n"
    rc = run_gated_stage("requirements", "Build X", cfg, invoke=_fake_invoke(out))
    assert rc == 0
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None and meta.status == "draft" and meta.spec_stage == "requirements"
    assert meta.source_prompt_version.startswith("sha256:")
    assert meta.validation == "pass"


def test_gated_stage_gate_requires_upstream_approved(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # requirements only draft -> generating design must refuse.
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ_BODY)
    rc = run_gated_stage("design", "Build X", cfg, invoke=_fake_invoke("SPEC_DESIGN_READY\nx\nSPEC_DESIGN_END"))
    assert rc != 0
    assert not cfg.design_file.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gated_plan.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_gated_stage'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/spec_runner/cli_plan.py
import subprocess
from .prompt import build_gated_generation_prompt, parse_spec_marker, template_hash
from .runner import build_cli_command
from .spec import (
    SpecMeta,
    read_spec_body,
    read_spec_meta,
    stage_path,
)
from .validate import validate_spec_stage, verdict_from_result

_MARKER = {"requirements": "REQUIREMENTS", "design": "DESIGN", "tasks": "TASKS"}
_UPSTREAM = {"requirements": [], "design": ["requirements"], "tasks": ["requirements", "design"]}


def run_gated_stage(stage: str, description: str, config, invoke=subprocess.run) -> int:
    """Generate one gated stage: enforce upstream gate, write DRAFT, validate, stop."""
    # Gate: every upstream stage must be approved.
    context: dict[str, str] = {}
    for up in _UPSTREAM[stage]:
        m = read_spec_meta(stage_path(config, up))
        if m is None or m.status != "approved":
            print(f"⛔ cannot generate {stage}: {up} must be APPROVED first")
            return 2
        context[up] = read_spec_body(stage_path(config, up))

    prompt = build_gated_generation_prompt(stage, description, context)
    cmd = build_cli_command(
        cmd=config.claude_command,
        prompt=prompt,
        model=config.claude_model,
        template=config.command_template,
        skip_permissions=config.skip_permissions,
    )
    result = invoke(
        cmd, capture_output=True, text=True,
        timeout=config.task_timeout_minutes * 60, cwd=config.project_root,
    )
    if result.returncode != 0:
        print(f"generation failed at {stage}: {result.stderr[:300]}")
        return 1
    body = parse_spec_marker(result.stdout, _MARKER[stage])
    if not body:
        print(f"no {stage} content produced (marker missing)")
        return 1

    path = stage_path(config, stage)
    meta = SpecMeta(
        spec_stage=stage,
        status="draft",
        version=1,
        generated_by=f"{_harness(config)}@{config.claude_model or 'default'}",
        generated_at=_now_iso(),
        source_prompt_version=template_hash(stage),
    )
    from .spec import write_spec
    write_spec(path, meta, body.rstrip("\n") + "\n")

    verdict = verdict_from_result(validate_spec_stage(stage, config))
    meta.validation = verdict
    write_spec(path, meta, read_spec_body(path))
    print(f"{stage}.md written as DRAFT — validation={verdict}")
    if verdict == "fail":
        print("  fix the errors, then `spec approve` (approve will re-validate)")
    else:
        print(f"  approve with: spec-runner spec approve {stage}")
    return 0


def _harness(config) -> str:
    base = (config.claude_command or "claude").split("/")[-1]
    return base or "claude"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

Then in `cmd_plan`, before the existing `--full` branch, add:

```python
    if getattr(args, "gated", False):
        from .spec import STAGES, resolve_next_stage, read_spec_meta, stage_path

        stage = getattr(args, "stage", None)
        if not stage:
            metas = {s: read_spec_meta(stage_path(config, s)) for s in STAGES}
            action, stage = resolve_next_stage(metas)
            if action == "await_approval":
                print(f"{stage} is DRAFT — approve or edit it before continuing")
                return
            if action == "stale":
                print(f"{stage} is STALE — regenerate (--stage {stage} --force) or re-approve")
                return
            if action == "done":
                print("all stages approved → spec-runner run")
                return
        raise SystemExit(run_gated_stage(stage, description, config))
```

Add `--gated` (store_true) and `--stage` (choices: requirements/design/tasks) to the `plan` subparser in `cli.py:_build_parser()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_gated_plan.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/cli_plan.py src/spec_runner/cli.py tests/test_gated_plan.py
git commit -m "feat(plan): --gated generates one stage, validates, writes DRAFT"
```

---

## Task 12: CLI wiring — `spec` subparser + TTY checkpoint menu

**Files:**
- Modify: `src/spec_runner/cli.py` (`_build_parser()`, dispatch), `src/spec_runner/spec_commands.py` (TTY menu)
- Test: `tests/test_cli_flags.py` (extend), `tests/test_spec_commands.py` (menu)

**Interfaces:**
- Consumes: `cmd_spec_status/approve/reject/adopt/check` (Task 8); `run_gated_stage` (Task 11).
- Produces:
  - `spec` subparser with sub-subcommands `status|approve|reject|adopt|check`, each dispatching to the Task 8 handlers; `approve`/`adopt` accept `stage` positional; `adopt` accepts `--force`.
  - `run_checkpoint_menu(stage: str, config, input_fn=input) -> str` — returns one of `{"approved","edit","regenerate","stop","abort"}`; `approve` option is refused while validation fails.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_spec_commands.py
from types import SimpleNamespace
from spec_runner.spec_commands import run_checkpoint_menu
from spec_runner.spec import write_spec, SpecMeta


def test_menu_refuses_approve_when_validation_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), "no scope\n")
    # User picks 'a' (approve) but validation fails → menu returns to prompt; feed 's' next.
    answers = iter(["a", "s"])
    action = run_checkpoint_menu("requirements", cfg, input_fn=lambda _: next(answers))
    assert action == "stop"


def test_menu_approve_when_valid(tmp_path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)
    action = run_checkpoint_menu("requirements", cfg, input_fn=lambda _: "a")
    assert action == "approved"
```

```python
# add to tests/test_cli_flags.py
from spec_runner.cli import _build_parser


def test_spec_subparser_exists():
    parser = _build_parser()
    ns = parser.parse_args(["spec", "approve", "requirements"])
    assert ns.command == "spec"
    assert ns.spec_command == "approve"
    assert ns.stage == "requirements"


def test_plan_gated_flags_parse():
    parser = _build_parser()
    ns = parser.parse_args(["plan", "--gated", "--stage", "design", "desc"])
    assert ns.gated is True and ns.stage == "design"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_flags.py::test_spec_subparser_exists tests/test_spec_commands.py::test_menu_approve_when_valid -v`
Expected: FAIL (`AttributeError`/`ImportError` — subparser and `run_checkpoint_menu` missing)

- [ ] **Step 3: Write minimal implementation**

Add the menu to `spec_commands.py`:

```python
# add to src/spec_runner/spec_commands.py
def run_checkpoint_menu(stage, config, input_fn=input) -> str:
    """Interactive checkpoint over the same file operations as the CI path.

    Returns one of: approved | edit | regenerate | stop | abort.
    """
    while True:
        verdict = verdict_from_result(validate_spec_stage(stage, config))
        approve_hint = "[a] approve" if verdict != "fail" else "[a] approve (blocked: fix errors)"
        print(f"{stage}.md — DRAFT, validation: {verdict.upper()}")
        choice = input_fn(f"{approve_hint}  [e] edit  [r] regenerate  [s] stop  [q] abort: ").strip().lower()
        if choice == "a":
            if verdict == "fail":
                print("  cannot approve while validation fails")
                continue
            rc = cmd_spec_approve(type("A", (), {"stage": stage, "force": False})(), config)
            if rc == 0:
                return "approved"
            continue
        if choice == "e":
            return "edit"
        if choice == "r":
            return "regenerate"
        if choice == "s":
            return "stop"
        if choice == "q":
            return "abort"
```

Add the `spec` subparser in `cli.py:_build_parser()` (mirror existing subparsers), storing `dest="spec_command"`, with `approve/reject/adopt/check` taking a `stage` positional (choices `requirements/design/tasks`) and `adopt` a `--force` flag; `status` takes no args. In `main()` dispatch:

```python
    if args.command == "spec":
        from . import spec_commands
        handler = {
            "status": spec_commands.cmd_spec_status,
            "approve": spec_commands.cmd_spec_approve,
            "reject": spec_commands.cmd_spec_reject,
            "adopt": spec_commands.cmd_spec_adopt,
            "check": spec_commands.cmd_spec_check,
        }[args.spec_command]
        raise SystemExit(handler(args, config))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_flags.py tests/test_spec_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/cli.py src/spec_runner/spec_commands.py tests/test_cli_flags.py tests/test_spec_commands.py
git commit -m "feat(cli): spec subparser + TTY checkpoint menu"
```

---

## Task 13: Docs + full-suite verification

**Files:**
- Modify: `src/spec_runner/CLAUDE.md` (module table + CLI entry points), `CHANGELOG.md`, `README.md` (governance section)
- Test: whole suite

**Interfaces:**
- Consumes: everything above.
- Produces: documentation only; no new code signatures.

- [ ] **Step 1: Update CHANGELOG.md**

Add an entry (Unreleased):

```markdown
### Added
- Gated spec generation (`plan --gated`, `spec status/approve/reject/adopt/check`):
  file-based frontmatter state (draft/approved/stale), hard per-stage validation
  gate, approval checkpoints, rich single-source templates. Opt-in via
  `spec_governance: strict` (default `off`); backward-compatible with unmanaged
  and Maestro-produced specs.
```

- [ ] **Step 2: Update `src/spec_runner/CLAUDE.md`**

Add `spec.py` and `spec_commands.py` rows to the module table; add `plan --gated`, `spec ...`, and `run --strict/--no-strict` to the CLI entry-points list; note `spec_governance` in the config section.

- [ ] **Step 3: Add a README governance section**

Document the flow (`plan --gated` → `spec approve` → `run`), the three statuses, `spec_governance`, and the "guardrail not enforcement boundary" caveat.

- [ ] **Step 4: Run the full suite (non-slow) + lint + types**

Run:
```bash
uv run ruff format . && uv run ruff check . --fix
uv run pytest tests/ -v -m "not slow"
uv run mypy src
```
Expected: all green; no regressions in existing tests.

- [ ] **Step 5: Run slow/e2e subset to confirm no run-path regressions**

Run: `uv run pytest tests/ -v -m slow`
Expected: PASS (existing e2e green — governance defaults off, so unmanaged specs run unchanged).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: document gated spec generation + changelog"
```

---

## Self-Review

**1. Spec coverage** — mapping design → tasks:
- Frontmatter schema (SpecMeta, statuses, versions, all fields) → Tasks 1, 7.
- Atomic write + FileLock + approve-vs-run race → Tasks 2, 8, 10.
- Rich single-source templates + content hash → Tasks 4, 5.
- Two-gate (validation hard + approval human), approve re-validates (TOCTOU) → Tasks 6, 8.
- Next-stage resolution table → Task 7.
- Stale cascade on **any** version bump (incl. edit-then-approve) → Task 7 (`apply_approval`).
- Adopt gate (validate-first) → Task 8.
- reject → draft (no new status; ReviewVerdict collision noted) → Task 8.
- Run gate + `spec_governance` + `--strict/--no-strict` → Tasks 9, 10.
- `plan --gated` one-stage flow + upstream gate → Task 11.
- TTY checkpoint overlay → Task 12.
- Backward compat (parse_tasks strip; default off; unmanaged) → Tasks 3, 9, 10.
- Out-of-scope items (Language Profiles, SQLite machine, cross-phase gate, obs events) → intentionally absent; noted here so no task implements them.

**2. Placeholder scan** — no "TBD"/"handle edge cases" placeholders; every code step carries real code. Two steps flag a code-shape cleanup inline (the awkward one-expression branch in Task 5, and moving inline imports to module top in Task 2) — these are explicit refactor notes, not deferred work.

**3. Type consistency** — `SpecMeta` field names, `verdict_from_result`→`{pass,warn,fail}`, `resolve_next_stage`→`(action, stage)`, `apply_approval(config, stage, approver, now, fresh_validation)`, `run_gated_stage(stage, description, config, invoke=...)`, `spec_run_gate_ok(config)->(bool,str)`, `stage_path(config, stage)` are used consistently across Tasks 1–12. `config.spec_lock_file` is introduced as a stub in Task 7's test and as the real property in Task 9 — the note in Task 7 Step 3 makes this ordering explicit.
