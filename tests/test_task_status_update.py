"""Regression tests for the task-bounded `update_task_status` contract.

Incident (issues #123/#124, disputatio D3, 2026-08-08): `update_task_status`
scanned forward past the target task's header looking for a meta line to
rewrite, with no boundary at the next `### TASK-...` header and only a
substring check on the task id. That let it (a) skip past an unrecognized
meta line for the target task and repaint the *next* task it happened to
find meta for, and (b) let `TASK-001` match inside `TASK-0011`.
"""

from pathlib import Path

from spec_runner.task import history_file_for, update_task_status


def test_unrecognized_meta_does_not_repaint_next_task(tmp_path: Path) -> None:
    """Target task's meta isn't in a format `TASK_META` recognizes.

    `TASK_META` was later taught to recognize `- `/`* ` bullet-prefixed meta
    lines (#123 part 2 — agents editing tasks.md mid-run introduce that
    form), so a genuinely *unrecognized* format is needed here: `+ ` is not
    an allowed bullet char, so this meta line still doesn't match. The old
    implementation kept scanning past TASK-001's (unmatched) meta line,
    found TASK-002's bare meta line, and repainted TASK-002 instead. The
    fix must refuse entirely: no write, no history entry.
    """
    p = tmp_path / "tasks.md"
    p.write_text(
        "### TASK-001: First\n"
        "+ 🔴 P0 | ⬜ TODO | Est: 1d\n"
        "### TASK-002: Second\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
    )
    before = p.read_text()

    assert update_task_status(p, "TASK-001", "in_progress") is False
    assert p.read_text() == before

    history = history_file_for(p)
    assert not history.exists()


def test_exact_id_match_does_not_touch_prefixed_task(tmp_path: Path) -> None:
    """`TASK-001` must not match inside `TASK-0011` (substring bug)."""
    p = tmp_path / "tasks.md"
    p.write_text(
        "### TASK-0011: Eleven\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
        "### TASK-001: One\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
    )

    assert update_task_status(p, "TASK-001", "in_progress") is True

    lines = p.read_text().split("\n")
    assert "TODO" in lines[1]
    assert "IN_PROGRESS" in lines[3]


def test_successful_update_changes_exactly_one_line(tmp_path: Path) -> None:
    p = tmp_path / "tasks.md"
    original = (
        "### TASK-001: First\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
        "### TASK-002: Second\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
    )
    p.write_text(original)

    assert update_task_status(p, "TASK-001", "in_progress") is True

    before_lines = original.split("\n")
    after_lines = p.read_text().split("\n")
    assert len(before_lines) == len(after_lines)
    changed = [i for i in range(len(before_lines)) if before_lines[i] != after_lines[i]]
    assert changed == [1]


def test_live_incident_scenario_bullet_then_bare_meta(tmp_path: Path) -> None:
    """Repeat the live scenario: unrecognized-meta TASK-001, bare-meta TASK-002.

    `+ ` isn't an allowed bullet char (see the test above for why `- ` no
    longer serves this role), so TASK-001's meta is still unrecognized here.
    Updating TASK-001 must never repaint TASK-002.
    """
    p = tmp_path / "tasks.md"
    p.write_text(
        "### TASK-001: First\n"
        "+ 🔴 P0 | ⬜ TODO | Est: 1d\n"
        "### TASK-002: Second\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
    )

    result = update_task_status(p, "TASK-001", "done")

    assert result is False
    text = p.read_text()
    task_002_line = text.split("\n")[3]
    assert "TODO" in task_002_line
    assert "DONE" not in task_002_line
