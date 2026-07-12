"""H-1/H-2 from the first governed orchestrate run (vault note 2026-07-12).

H-1: `run` must exit non-zero when pre-run validation fails — Maestro (and
any orchestrator) treats rc=0 as workstream success, so a silent `return`
turned "No tasks found" into a mergeable empty run.

H-2: `plan --full` must validate its own tasks output against the SAME
parser `run` uses — the generated `## TASK-001 — Title` was unparseable by
`^### (TASK-\\d+): ` and produced a spec its own runner could not consume.
"""

import argparse
from pathlib import Path

import pytest

from spec_runner.cli import _run_tasks
from spec_runner.cli_plan import validate_generated_tasks
from spec_runner.config import ExecutorConfig


def _unparseable_tasks_md(tmp_path: Path) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    p = spec / "tasks.md"
    # The exact live failure shape: h2 heading + em-dash, zero parseable tasks.
    p.write_text("# Tasks\n\n## TASK-001 — Add docs rule\n\n**Checklist:**\n- [ ] do it\n")
    return p


def _cfg(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(
        state_file=tmp_path / "state.db",
        project_root=tmp_path,
        logs_dir=tmp_path / "logs",
        create_git_branch=False,
        auto_commit=False,
        run_tests_on_done=False,
        run_review=False,
    )


def _run_args() -> argparse.Namespace:
    return argparse.Namespace(
        command="run",
        all=True,
        no_reset_failed=False,
        force=True,
        task=None,
        milestone=None,
        restart=False,
        dry_run=False,
        json_result=False,
        max_retries=None,
        timeout=None,
        no_tests=False,
        no_branch=False,
        no_commit=False,
        no_review=False,
        hitl_review=False,
        callback_url="",
        tui=False,
    )


class TestH1ValidationFailureExitsNonzero:
    def test_run_all_on_unparseable_spec_exits_1(self, tmp_path, capsys):
        _unparseable_tasks_md(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            _run_tasks(_run_args(), _cfg(tmp_path))
        assert excinfo.value.code == 1
        assert "No tasks found" in capsys.readouterr().out


class TestH2PlanValidatesOwnOutput:
    def test_unparseable_generated_tasks_exit_1(self, tmp_path):
        path = _unparseable_tasks_md(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            validate_generated_tasks(path)
        assert excinfo.value.code == 1

    def test_parseable_generated_tasks_pass(self, tmp_path):
        spec = tmp_path / "spec"
        spec.mkdir(parents=True, exist_ok=True)
        p = spec / "tasks.md"
        p.write_text(
            "# Tasks\n\n## M1\n\n### TASK-001: Add docs rule\n"
            "🔴 P0 | ⬜ TODO | Est: 0.1d\n\n"
            "**Checklist:**\n- [ ] do it\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n"
        )
        assert validate_generated_tasks(p) >= 1


class TestH2bHeaderNormalization:
    """Governed run #2: the LLM systematically emits '### TASK-001 — Title'
    (em-dash) despite the template. Recoverable deviations are normalized
    before validation; validation stays as the backstop."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("### TASK-001 — Add rule", "### TASK-001: Add rule"),
            ("### TASK-002 – Title", "### TASK-002: Title"),  # en-dash
            ("### TASK-003 - Title", "### TASK-003: Title"),  # hyphen
            ("## TASK-004 — Title", "### TASK-004: Title"),  # h2 -> h3
            ("### TASK-005: Already fine", "### TASK-005: Already fine"),
        ],
    )
    def test_variants_normalized(self, raw: str, expected: str) -> None:
        from spec_runner.cli_plan import normalize_task_headers

        assert normalize_task_headers(raw + "\nbody\n") == expected + "\nbody\n"

    def test_non_task_headings_untouched(self) -> None:
        from spec_runner.cli_plan import normalize_task_headers

        text = "# Tasks\n\n## Milestone — one\n\nplain — dash text\n"
        assert normalize_task_headers(text) == text

    def test_normalized_output_parses(self, tmp_path: Path) -> None:
        from spec_runner.cli_plan import normalize_task_headers, validate_generated_tasks

        raw = (
            "# Tasks\n\n### TASK-001 — Add docs rule\n"
            "🔴 P0 | ⬜ TODO | Est: 0.1d\n\n"
            "**Checklist:**\n- [ ] do it\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n"
        )
        spec = tmp_path / "spec"
        spec.mkdir()
        p = spec / "tasks.md"
        p.write_text(normalize_task_headers(raw))
        assert validate_generated_tasks(p) == 1
