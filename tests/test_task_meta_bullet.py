"""`TASK_META` bullet-prefix support (issue #123, disputatio D3).

Agents editing `tasks.md` mid-run introduce a leading `- ` bullet on some
meta lines (confirmed forensically via git-status correlation on the
disputatio snapshots: a task's meta line gains a `- ` prefix between one
snapshot and the next, while sibling tasks keep the bare `🔴 P0 | ...`
form in the same file — the generator templates themselves emit the bare
form). The old `TASK_META` regex didn't recognize the bullet-prefixed
form at all, which is what let `update_task_status` (fixed task-bounded
in #123 part 1) walk straight past an unrecognized meta line. The parser
must accept both formats regardless of source.

This module locks down: (1) the allowed bullet prefix (`-`/`*`, optionally
indented) is accepted without turning `TASK_META` into a match-anything
pattern — plain description bullets and checklist items must still not
match; (2) `parse_tasks`/`update_task_status` work correctly on a real
golden file that alternates bullet and bare meta lines in the same
document.
"""

import importlib.util
from pathlib import Path

import pytest

from spec_runner.task import TASK_META, get_task_by_id, parse_tasks, update_task_status

GOLDEN_FIXTURE = (
    Path(__file__).parent / "fixtures" / "maestro-interop" / "alternating-bullet-tasks.md"
)

# id -> (priority, status) exactly as authored in the golden fixture: TASK-001
# carries the `- ` bullet prefix (mid-incident artifact), TASK-002..011 are
# bare `🔴 P0 | ...` meta lines.
EXPECTED_GOLDEN_TASKS = {
    "TASK-001": ("p0", "in_progress"),
    "TASK-002": ("p0", "todo"),
    "TASK-003": ("p0", "todo"),
    "TASK-004": ("p1", "todo"),
    "TASK-005": ("p0", "todo"),
    "TASK-006": ("p0", "todo"),
    "TASK-007": ("p1", "todo"),
    "TASK-008": ("p1", "todo"),
    "TASK-009": ("p0", "todo"),
    "TASK-010": ("p0", "todo"),
    "TASK-011": ("p0", "todo"),
}


class TestTaskMetaBulletPrefix:
    """Direct regex-level checks, isolated from the parse loop."""

    def test_matches_dash_bullet_prefix(self) -> None:
        assert TASK_META.match("- 🔴 P0 | ⬜ TODO | Est: 1h")

    def test_matches_star_bullet_prefix(self) -> None:
        assert TASK_META.match("* 🔴 P0 | ⬜ TODO | Est: 1h")

    def test_matches_indented_bullet_prefix(self) -> None:
        assert TASK_META.match("  - 🔴 P0 | ⬜ TODO | Est: 1h")

    def test_still_matches_bare_meta_no_regression(self) -> None:
        assert TASK_META.match("🔴 P0 | ⬜ TODO | Est: 1h")

    def test_does_not_match_description_bullet_without_pipe_form(self) -> None:
        """A `- `-prefixed description line is not a `P\\d |` meta line."""
        assert TASK_META.match("- Some descriptive prose about the task") is None

    def test_does_not_match_checklist_item(self) -> None:
        assert TASK_META.match("- [ ] `uv add pydantic pyyaml`") is None

    def test_does_not_match_checked_checklist_item(self) -> None:
        assert TASK_META.match("- [x] done already") is None

    def test_does_not_match_unsupported_bullet_char(self) -> None:
        """Only `-`/`*` are recognized bullets — `+` stays unmatched."""
        assert TASK_META.match("+ 🔴 P0 | ⬜ TODO | Est: 1h") is None


def test_parse_tasks_golden_fixture_all_eleven_tasks_correct_status() -> None:
    """`parse_tasks` on the live incident snapshot recovers all 11 tasks.

    Both meta formats present in the same file (bullet-prefixed TASK-001,
    bare TASK-002..011) must resolve to the correct priority/status.
    """
    tasks = parse_tasks(GOLDEN_FIXTURE)
    assert [t.id for t in tasks] == [f"TASK-{i:03d}" for i in range(1, 12)]

    actual = {t.id: (t.priority, t.status) for t in tasks}
    assert actual == EXPECTED_GOLDEN_TASKS


def test_parse_tasks_golden_fixture_bullet_task_checklist_unaffected() -> None:
    """The bullet-meta task's own checklist items still parse as checklist,
    not as (falsely matched) meta lines."""
    tasks = parse_tasks(GOLDEN_FIXTURE)
    task_001 = get_task_by_id(tasks, "TASK-001")
    assert task_001 is not None
    assert len(task_001.checklist) == 6
    assert all(not checked for _, checked in task_001.checklist)


def test_update_task_status_bullet_format_task_round_trip(tmp_path: Path) -> None:
    """Updating a task whose *own* meta is bullet-prefixed must succeed and
    be confirmed by a post-write `parse_tasks` (Task 1's confirm step)."""
    target = tmp_path / "tasks.md"
    target.write_text(GOLDEN_FIXTURE.read_text())

    assert update_task_status(target, "TASK-001", "done") is True

    tasks = parse_tasks(target)
    task_001 = get_task_by_id(tasks, "TASK-001")
    assert task_001 is not None
    assert task_001.status == "done"

    # Neighbor untouched.
    task_002 = get_task_by_id(tasks, "TASK-002")
    assert task_002 is not None
    assert task_002.status == "todo"


def test_update_task_status_bare_format_task_round_trip(tmp_path: Path) -> None:
    """Updating a bare-meta task in the same alternating-format file."""
    target = tmp_path / "tasks.md"
    target.write_text(GOLDEN_FIXTURE.read_text())

    assert update_task_status(target, "TASK-002", "in_progress") is True

    tasks = parse_tasks(target)
    task_002 = get_task_by_id(tasks, "TASK-002")
    assert task_002 is not None
    assert task_002.status == "in_progress"

    # TASK-001 (bullet meta) untouched by the TASK-002 update.
    task_001 = get_task_by_id(tasks, "TASK-001")
    assert task_001 is not None
    assert task_001.status == "in_progress"  # unchanged from the fixture's own value


# --- Bundled template copy (src/spec_runner/skills/spec-generator-skill/templates/task.py) ---
# Must carry the same bullet-prefix TASK_META fix (Global Constraints: the
# bundled copy is what `spec-runner plan --full` ships to *generated*
# projects, so it needs the fix independently of the runtime module).

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    REPO_ROOT / "src" / "spec_runner" / "skills" / "spec-generator-skill" / "templates" / "task.py"
)


def _load_template_module():
    spec = importlib.util.spec_from_file_location("_bundled_task_template", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def template_module():
    return _load_template_module()


class TestBundledTemplateTaskMetaBulletPrefix:
    """The template's `TASK_META` requires the emoji (its own long-standing
    contract, unrelated to this fix) but must gain the same bullet-prefix
    allowance as the runtime module."""

    def test_matches_dash_bullet_prefix(self, template_module) -> None:
        assert template_module.TASK_META.match("- 🔴 P0 | ⬜ TODO | Est: 1h")

    def test_still_matches_bare_meta_no_regression(self, template_module) -> None:
        assert template_module.TASK_META.match("🔴 P0 | ⬜ TODO | Est: 1h")

    def test_does_not_match_checklist_item(self, template_module) -> None:
        assert template_module.TASK_META.match("- [ ] some checklist item") is None

    def test_does_not_match_description_bullet(self, template_module) -> None:
        assert template_module.TASK_META.match("- prose, not meta") is None


def test_bundled_template_update_task_status_is_task_bounded(tmp_path, template_module) -> None:
    """Port of Task 1's task-boundedness fix into the bundled template:
    an unrecognized meta line for the target task must never let the scan
    fall through to a neighboring task's meta line."""
    p = tmp_path / "tasks.md"
    p.write_text(
        "### TASK-001: First\n"
        "+ 🔴 P0 | ⬜ TODO | Est: 1d\n"
        "### TASK-002: Second\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
    )
    before = p.read_text()

    assert template_module.update_task_status(p, "TASK-001", "in_progress") is False
    assert p.read_text() == before


def test_bundled_template_update_task_status_bullet_round_trip(tmp_path, template_module) -> None:
    p = tmp_path / "tasks.md"
    p.write_text(
        "### TASK-001: First\n"
        "- 🔴 P0 | ⬜ TODO | Est: 1d\n"
        "### TASK-002: Second\n"
        "🔴 P0 | ⬜ TODO | Est: 1d\n"
    )

    assert template_module.update_task_status(p, "TASK-001", "in_progress") is True

    tasks = template_module.parse_tasks(p)
    task_001 = template_module.get_task_by_id(tasks, "TASK-001")
    task_002 = template_module.get_task_by_id(tasks, "TASK-002")
    assert task_001.status == "in_progress"
    assert task_002.status == "todo"
