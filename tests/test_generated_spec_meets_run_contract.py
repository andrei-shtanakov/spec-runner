"""#301: `plan --full` must not hand `run` a spec its own validator refuses.

The reported file parsed (headers were canonical) and was returned as a
successful generation, but every task carried a decorated meta line —
``🔴 **P0** | Est: **2h**``, bold around the tokens the parser anchors on and
no status word at all — so `run --all` refused all of it before a single task
started. `validate_generated_tasks` only asked "does at least one task parse",
which is a strictly weaker question than the one `run` asks.

Two halves, and both are needed: the recoverable deviation is normalized
(`normalize_task_meta`), and whatever survives that is a **generation error**,
not a successful generation followed by an execution refusal.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner.cli_plan import (
    apply_plan_confirmation,
    normalize_task_headers,
    normalize_task_meta,
    validate_generated_tasks,
)
from spec_runner.prompt import build_generation_prompt
from spec_runner.task import parse_tasks
from spec_runner.validate import validate_tasks

# Verbatim from the issue (2.33.2, `plan --full ... --spec-prefix maestro-`).
REPORTED = """# Tasks

## Milestone M1

### TASK-001: Скелет пакета, вендоринг контракта и импортируемый линтер

🔴 **P0** | Est: **2h** | **Depends on:** — | **Blocks:** [TASK-002]

**Checklist:**
- [ ] scaffold

**Traces to:** [REQ-1]

### TASK-002: Второй шаг

🟠 **P1** | Est: **3h** | **Depends on:** [TASK-001] | **Blocks:** —

**Checklist:**
- [ ] do it

**Traces to:** [REQ-1]
"""

CANONICAL = """# Tasks

### TASK-001: Add docs rule

🔴 P0 | ⬜ TODO | Est: 0.1d

**Checklist:**
- [ ] do it

**Traces to:** [REQ-1]
**Depends on:** —
"""


def _write(tmp_path: Path, text: str) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    path = spec / "tasks.md"
    path.write_text(text)
    return path


class TestTheReportedFileIsRefusedOrFixed:
    """The defect measured from both ends: as generated, and as normalized."""

    def test_reported_file_fails_the_run_validator(self, tmp_path: Path) -> None:
        # The premise: this is what `run` said about the delivered file.
        result = validate_tasks(_write(tmp_path, REPORTED))
        assert not result.ok
        assert any("meta line not recognized" in e for e in result.errors)

    def test_normalization_makes_it_run_ready(self, tmp_path: Path) -> None:
        path = _write(tmp_path, normalize_task_meta(REPORTED))
        assert validate_tasks(path).ok
        tasks = parse_tasks(path)
        assert [(t.id, t.priority, t.status) for t in tasks] == [
            ("TASK-001", "p0", "todo"),
            ("TASK-002", "p1", "todo"),
        ]
        # The rest of the meta line survives the rewrite: the estimate stops
        # being invisible, and the dependency markers still parse.
        assert [t.estimate for t in tasks] == ["2h", "3h"]
        assert tasks[0].blocks == ["TASK-002"]
        assert tasks[1].depends_on == ["TASK-001"]

    def test_generation_gate_accepts_the_normalized_file(self, tmp_path: Path) -> None:
        assert validate_generated_tasks(_write(tmp_path, normalize_task_meta(REPORTED))) == 2


class TestGenerationGateIsTheRunValidator:
    """A discrepancy comes back as a generation error, never as success."""

    def test_unfixable_meta_exits_1_with_the_validators_own_words(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Parses as a task (the header is canonical), so the old gate passed it.
        broken = "# Tasks\n\n### TASK-001: No meta at all\n\nPriority: high, 2 hours\n"
        path = _write(tmp_path, normalize_task_meta(broken))
        assert parse_tasks(path)  # the weaker question still answers yes
        with pytest.raises(SystemExit) as excinfo:
            validate_generated_tasks(path)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "meta line not recognized" in out
        assert str(path) in out

    def test_dangling_dependency_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same class, different error: `run` refuses it, so generation must too.
        text = CANONICAL.replace("**Depends on:** —", "**Depends on:** [TASK-404]")
        with pytest.raises(SystemExit) as excinfo:
            validate_generated_tasks(_write(tmp_path, text))
        assert excinfo.value.code == 1
        assert "TASK-404" in capsys.readouterr().out

    def test_the_file_is_kept_for_inspection(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, CANONICAL.replace("**Depends on:** —", "**Depends on:** [TASK-404]")
        )
        with pytest.raises(SystemExit):
            validate_generated_tasks(path)
        assert path.exists()

    def test_a_valid_file_still_passes(self, tmp_path: Path) -> None:
        assert validate_generated_tasks(_write(tmp_path, CANONICAL)) == 1

    def test_warnings_alone_do_not_fail_generation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No estimate, no traceability: `run` runs this file, so plan returns it.
        text = "# Tasks\n\n### TASK-001: Bare\n\n🔴 P0 | ⬜ TODO\n\n- [ ] do it\n"
        assert validate_generated_tasks(_write(tmp_path, text)) == 1
        capsys.readouterr()


class TestMetaNormalizationIsConservative:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("🔴 **P0** | ⬜ TODO | Est: 1d", "🔴 P0 | ⬜ TODO | Est: 1d"),
            ("🔴 P0 | ⬜ **TODO** | Est: 1d", "🔴 P0 | ⬜ TODO | Est: 1d"),
            ("🔴 P0 | ⬜ TODO | Est: **2h**", "🔴 P0 | ⬜ TODO | Est: 2h"),
            ("**🔴 P0** | ⬜ TODO | Est: 1d", "🔴 P0 | ⬜ TODO | Est: 1d"),
            ("🔴 **P0** | Est: **2h**", "🔴 P0 | ⬜ TODO | Est: 2h"),
            ("- 🟢 **P3** | Est: 1d", "- 🟢 P3 | ⬜ TODO | Est: 1d"),
            ("P0 | Est: 1d", "P0 | ⬜ TODO | Est: 1d"),
            # The bold wraps the whole segment, emoji included (Copilot, PR
            # #302): matching only the word left the status unrecognized, so
            # the missing-status branch appended a second one and the bolded
            # original stayed behind as a stray `**⬜ TODO**` line.
            ("🔴 P0 | **⬜ TODO** | Est: 1d", "🔴 P0 | ⬜ TODO | Est: 1d"),
            ("**🔴 P0** | **⬜ TODO** | Est: **2h**", "🔴 P0 | ⬜ TODO | Est: 2h"),
            ("🟡 P2 | **🔄 IN_PROGRESS** | Est: 1d", "🟡 P2 | 🔄 IN_PROGRESS | Est: 1d"),
        ],
    )
    def test_variants_normalized(self, raw: str, expected: str) -> None:
        assert (
            normalize_task_meta(f"### TASK-001: T\n\n{raw}\n") == f"### TASK-001: T\n\n{expected}\n"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "🔴 P0 | ⬜ TODO | Est: 1d",  # already canonical
            "🟡 P2 | 🔄 IN_PROGRESS | Est: 1d",  # a status that is not TODO
            "**Depends on:** [TASK-001] | **Blocks:** —",  # not a meta line
            "The P0 tasks | come first",  # prose that mentions P0
            "- [ ] P0 | checklist item",  # a checklist item
        ],
    )
    def test_lines_left_alone(self, line: str) -> None:
        text = f"### TASK-001: T\n\n{line}\n"
        assert normalize_task_meta(text) == text

    def test_inline_fields_move_off_the_meta_line(self) -> None:
        # `parse_tasks` stops at the estimate, so a dependency declared here
        # validates cleanly and orders nothing.
        raw = "### TASK-002: T\n\n🔴 P0 | ⬜ TODO | Est: 1d | **Depends on:** [TASK-001]\n"
        assert normalize_task_meta(raw) == (
            "### TASK-002: T\n\n🔴 P0 | ⬜ TODO | Est: 1d\n**Depends on:** [TASK-001]\n"
        )

    def test_declared_status_is_never_rewritten(self) -> None:
        text = "### TASK-001: T\n\n🔴 **P0** | ✅ **DONE** | Est: 1d\n"
        out = normalize_task_meta(text)
        assert "DONE" in out and "TODO" not in out

    def test_a_bolded_status_leaves_no_stray_line(self) -> None:
        out = normalize_task_meta("### TASK-001: T\n\n🔴 P0 | **✅ DONE** | Est: 1d\n")
        assert out.count("DONE") == 1
        assert "**" not in out

    def test_idempotent(self) -> None:
        once = normalize_task_meta(REPORTED)
        assert normalize_task_meta(once) == once

    def test_composes_with_header_normalization(self, tmp_path: Path) -> None:
        raw = "# Tasks\n\n## TASK-001 — Add rule\n\n🔴 **P0** | Est: **2h**\n\n- [ ] do it\n"
        path = _write(tmp_path, normalize_task_meta(normalize_task_headers(raw)))
        assert validate_generated_tasks(path) == 1


class TestTheFullPipelinePromptStatesTheFormat:
    """Why the model deviated: `--full` never told it the shape.

    `build_gated_generation_prompt` embeds the whole bundled template (which
    shows the canonical meta line throughout); `build_generation_prompt` —
    the `--full` path — sends only the stage's `prompt_text`, and that asked
    for "priorities (P0-P3), estimates, checklists" without ever showing the
    line the parser reads.
    """

    def test_tasks_prompt_shows_the_meta_line(self) -> None:
        prompt = build_generation_prompt("tasks", "build a thing")
        assert "🔴 P0 | ⬜ TODO | Est: 2d" in prompt
        assert "**P0**" in prompt  # named as the thing not to emit

    def test_markers_survive(self) -> None:
        prompt = build_generation_prompt("tasks", "build a thing")
        assert "SPEC_TASKS_READY" in prompt and "SPEC_TASKS_END" in prompt


class TestInteractivePlanNormalizesToo:
    """`plan` (no --full) appends the proposal to tasks.md; same generator."""

    def test_appended_block_is_normalized(self, tmp_path: Path) -> None:
        tasks_file = _write(tmp_path, "# Tasks\n")
        cfg = SimpleNamespace(tasks_file=tasks_file)
        apply_plan_confirmation(
            "y",
            ["TASK-001: Add rule\n🔴 **P0** | Est: **2h** | **Depends on:** —\n\n- [ ] do it"],
            cfg,  # type: ignore[arg-type]
        )
        assert validate_tasks(tasks_file).ok
        (task,) = parse_tasks(tasks_file)
        assert (task.priority, task.status, task.estimate) == ("p0", "todo", "2h")

    def test_existing_content_is_untouched(self, tmp_path: Path) -> None:
        before = "# Tasks\n\n### TASK-000: Old\n\n🔴 **P0** | Est: **9h**\n"
        tasks_file = _write(tmp_path, before)
        apply_plan_confirmation(
            "y",
            ["TASK-001: New\n🔴 P0 | ⬜ TODO | Est: 1d"],
            SimpleNamespace(tasks_file=tasks_file),  # type: ignore[arg-type]
        )
        assert tasks_file.read_text().startswith(before)
