"""RED for BEH-06: a task that does not declare verify-first behaves exactly
as it did before the third mode existed.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-06
Traces: FR-04

Given a tasks-file mixing a task with no `**Mode:**`/`**Verifies:**` line at
all, a task explicitly declaring `**Mode:** standard`, a task explicitly
declaring `**Mode:** tdd`, and — in the same file — a sibling task that *does*
declare `verify_first` with a valid `**Verifies:**` group, the three
non-verify-first tasks must resolve and validate identically to how they did
before TASK-001..003 introduced the third mode, under either project default
(`standard` or `tdd`), and regardless of the verify_first sibling's presence.

An A/B run of this exact scenario against the pre-#367 commit (`ed2d632^`,
via `git worktree`) and against this branch's HEAD, comparing
`ExecutorConfig.resolve_execution_mode` and `validate.validate_all` for every
non-verify-first task, shows zero drift — which is exactly what BEH-06
promises and TASK-001..003 already deliver. TASK-005 is about to touch
`execution.py`/`hooks.py` for the first time (the live verify-run, FR-05/06)
in a way TASK-001..003 never did (they only touched `config.py`, `task.py`,
`validate.py`), so this slice's job is to turn that one-time A/B diff into a
permanent, golden-pinned regression test *before* that happens — the same
role `tests/test_c1_zero_behaviour.py` played for the STAGES→profile
refactor, including its `--update-golden` mechanic (`tests/conftest.py`).

Today no such golden exists: this is the first test to freeze this exact
snapshot, so it fails on a missing fixture rather than a wrong value — that
absence is itself what this slice must fill in.
"""

import json
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.task import parse_tasks
from spec_runner.validate import validate_all

GOLDEN = (
    Path(__file__).parent / "fixtures" / "beh-06-zero-behaviour" / "non_verify_first_snapshot.json"
)

TASKS_MD = """### TASK-001: legacy, no Mode or Verifies at all
\U0001f7e0 P1 | ⬜ TODO   Est: 1d

Plain description, untouched by this feature.

**Checklist:**
- [ ] do a thing
- [x] do another thing

**Traces to:** [REQ-001]

### TASK-002: explicit standard
\U0001f7e0 P1 | ⬜ TODO   Est: 1d
**Mode:** standard
**Depends on:** [TASK-001]

**Checklist:**
- [ ] do a thing

### TASK-003: explicit tdd
\U0001f7e0 P1 | ⬜ TODO   Est: 1d
**Mode:** tdd
**Depends on:** [TASK-002]

**Checklist:**
- [ ] do a thing

### TASK-004: sibling verify-first task, out of BEH-06's scope
\U0001f7e0 P1 | ⬜ TODO   Est: 1d
**Mode:** verify_first
**Verifies:** tests/test_x.py::test_y

**Checklist:**
- [ ] do a thing
"""

NON_VERIFY_FIRST_IDS = ("TASK-001", "TASK-002", "TASK-003")


def _snapshot(tmp_path: Path) -> dict:
    tasks_path = tmp_path / "tasks.md"
    tasks_path.write_text(TASKS_MD)
    tasks = parse_tasks(tasks_path)
    by_id = {t.id: t for t in tasks}

    out: dict = {}
    for project_default in ("standard", "tdd"):
        cfg = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
            logs_dir=tmp_path / "logs",
            execution_mode=project_default,
        )
        resolved = {tid: cfg.resolve_execution_mode(by_id[tid]) for tid in NON_VERIFY_FIRST_IDS}
        result = validate_all(tasks_file=tasks_path, config_file=None)
        out[project_default] = {
            "resolved_modes": resolved,
            "validate_ok": result.ok,
            "validate_errors": result.errors,
            "validate_warnings": result.warnings,
        }
    return out


class TestNonVerifyFirstTasksMatchThePreFeatureBaseline:
    """kind: contract — BEH-06: the golden snapshot pins zero drift for the
    three non-verify-first tasks, under either project default, with a
    verify_first sibling present in the same file."""

    def test_snapshot_matches_the_golden_baseline(self, tmp_path: Path, request):
        snapshot = _snapshot(tmp_path)

        if request.config.getoption("--update-golden", default=False):
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

        golden = json.loads(GOLDEN.read_text())
        assert snapshot == golden
