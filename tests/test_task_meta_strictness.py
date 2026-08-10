"""The meta line must be recognized exactly, or the spec must be refused.

Two issues, one seam (#128, #133). `TASK_META` read the status as `(\\w+)`, so
any prose that happened to start `P0 | ...` became a meta line — a hazard the
2.22.0 bullet allowance widened, because description bullets now reach the
pattern too. And when a meta line is *not* recognized (the `plan --full`
generator emits at least three different orderings), the task silently keeps
its parse defaults — `p0`/`todo` — validation passes, dependencies resolve, and
the run dies at the first task on the 2.22.0 reconciliation gate.

Both directions are the same defect: the parser guessing instead of refusing.
Tightening the pattern alone would make the second case worse (an unrecognized
meta would leave a task looking like a ready TODO), so the validation error is
not a separate nicety — it is the other half of the fix.
"""

from pathlib import Path

import pytest

from spec_runner.task import parse_tasks, update_task_status
from spec_runner.validate import validate_task_fields

CANONICAL = "🔴 P0 | ⬜ TODO | Est: 1d"


def _spec(tmp_path: Path, *task_blocks: str) -> Path:
    p = tmp_path / "tasks.md"
    p.write_text("# Spec\n\n## M0\n\n" + "\n\n".join(task_blocks) + "\n")
    return p


def _task(tid: str, meta: str, body: str = "") -> str:
    return (
        f"### {tid}: Demo\n{meta}\n\n"
        "**Description:** demo\n"
        f"{body}"
        "\n**Checklist:**\n- [ ] work\n\n"
        "**Traces to:** [REQ-1]\n**Depends on:** —\n"
    )


# --------------------------------------------------------------------------
# #128 — the status is one of five words, not any word
# --------------------------------------------------------------------------


class TestStatusWhitelist:
    @pytest.mark.parametrize(
        "meta,expected",
        [
            ("🔴 P0 | ⬜ TODO | Est: 1d", "todo"),
            ("🟠 P1 | 🔄 IN_PROGRESS | Est: 2h", "in_progress"),
            ("🟡 P2 | 🔍 REVIEW | Est: 1d", "review"),
            ("🟢 P3 | ✅ DONE | Est: 1d", "done"),
            ("🔴 P0 | ⏸️ BLOCKED | Est: 1d", "blocked"),
            ("P0 | TODO | Est: 1d", "todo"),  # bare form (generator templates)
            ("- 🔴 P0 | ⬜ TODO | Est: 1d", "todo"),  # bullet form (2.22.0)
            ("* P1 | done | Est: 1d", "done"),  # lower case still accepted
        ],
    )
    def test_known_statuses_still_parse(self, tmp_path, meta, expected):
        tasks = parse_tasks(_spec(tmp_path, _task("TASK-001", meta)))
        assert len(tasks) == 1
        assert tasks[0].status == expected

    def test_prose_bullet_is_not_a_meta_line(self, tmp_path):
        """`- P0 | high priority stuff` used to yield status "high".

        The real meta comes first here, so the prose must not overwrite it.
        """
        tasks = parse_tasks(
            _spec(
                tmp_path,
                _task("TASK-001", CANONICAL, body="- P0 | high priority stuff\n"),
            )
        )
        assert tasks[0].status == "todo", "prose bullet was read as the meta line"
        assert tasks[0].priority == "p0"

    def test_prose_bullet_does_not_hijack_the_status_write(self, tmp_path):
        """`update_task_status` rewrites the FIRST meta match after the header.

        With prose ahead of the real meta, that used to be the prose line — the
        status write landed in the description and the real meta kept its old
        value, which the 2.22.0 reconciliation gate then reports as a
        state/spec mismatch.
        """
        spec = _spec(
            tmp_path,
            "### TASK-001: Demo\n"
            "- P1 | high priority stuff\n"
            f"{CANONICAL}\n\n"
            "**Description:** demo\n\n**Checklist:**\n- [ ] work\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n",
        )
        assert update_task_status(spec, "TASK-001", "done") is True

        text = spec.read_text()
        assert "- P1 | high priority stuff" in text, "the prose line was rewritten"
        assert "✅ DONE" in text
        assert parse_tasks(spec)[0].status == "done"


# --------------------------------------------------------------------------
# #133 — an unrecognized meta line is a refusal, not a default
# --------------------------------------------------------------------------


class TestUnrecognizedMetaIsAnError:
    # The third meta ordering observed from `plan --full` in a single pilot:
    # id and status before the priority, which `TASK_META` cannot match.
    LOTTERY = "- TASK-023 | 🔄 IN_PROGRESS | P0 | est: 1h | src/x.py | refs: [REQ-1]"

    def test_unparsed_meta_defaults_are_not_silently_accepted(self, tmp_path):
        tasks = parse_tasks(_spec(tmp_path, _task("TASK-023", self.LOTTERY)))
        result = validate_task_fields(tasks)

        assert not result.ok, (
            "a task whose meta line was never recognized passed validation — "
            "it defaults to p0/todo and dies at runtime instead (#133)"
        )
        assert any("TASK-023" in e and "meta" in e.lower() for e in result.errors), (
            f"error does not name the task and its meta line: {result.errors}"
        )

    def test_error_not_warning(self, tmp_path):
        """`validate` without --strict must refuse: warnings would let the run
        start and fail on the first task."""
        tasks = parse_tasks(_spec(tmp_path, _task("TASK-023", self.LOTTERY)))
        result = validate_task_fields(tasks)
        assert not any("meta" in w.lower() for w in result.warnings)

    def test_canonical_meta_produces_no_such_error(self, tmp_path):
        tasks = parse_tasks(_spec(tmp_path, _task("TASK-001", CANONICAL)))
        result = validate_task_fields(tasks)
        assert not any("meta" in e.lower() for e in result.errors), result.errors

    def test_reports_every_offending_task(self, tmp_path):
        tasks = parse_tasks(
            _spec(
                tmp_path,
                _task("TASK-001", CANONICAL),
                _task("TASK-002", self.LOTTERY),
                _task("TASK-003", "P0 something entirely different"),
            )
        )
        result = validate_task_fields(tasks)
        offenders = {e.split(":")[0] for e in result.errors if "meta" in e.lower()}
        assert offenders == {"TASK-002", "TASK-003"}, offenders

    def test_defaults_are_still_defaults_for_a_recognized_partial_meta(self, tmp_path):
        """A meta line without an estimate is incomplete, not unrecognized —
        it keeps its existing warning and must not become an error."""
        tasks = parse_tasks(_spec(tmp_path, _task("TASK-001", "🔴 P0 | ⬜ TODO")))
        result = validate_task_fields(tasks)
        assert not any("meta" in e.lower() for e in result.errors)
        assert any("estimate" in w for w in result.warnings)
