"""Tests for spec_runner.stages module (v2.3.0)."""

import pytest

from spec_runner.stages import STAGES, StageReporter


class TestStageReporter:
    def test_enter_updates_current_and_calls_mirror(self):
        events: list[str] = []
        rep = StageReporter("TASK-001", events.append)
        rep.enter("codex")
        assert rep.current == "codex"
        assert events == ["[TASK-001] ⏳ stage: codex"]

    def test_multiple_enters_record_latest(self):
        events: list[str] = []
        rep = StageReporter("T1", events.append)
        for s in ("sync_deps", "codex", "parse", "tests"):
            rep.enter(s)
        assert rep.current == "tests"
        assert events == [
            "[T1] ⏳ stage: sync_deps",
            "[T1] ⏳ stage: codex",
            "[T1] ⏳ stage: parse",
            "[T1] ⏳ stage: tests",
        ]

    def test_invalid_stage_raises_assertion(self):
        rep = StageReporter("T1", lambda _: None)
        with pytest.raises(AssertionError):
            rep.enter("not_a_real_stage")

    def test_two_reporters_do_not_share_state(self):
        ev1: list[str] = []
        ev2: list[str] = []
        r1 = StageReporter("T1", ev1.append)
        r2 = StageReporter("T2", ev2.append)
        r1.enter("codex")
        r2.enter("tests")
        assert r1.current == "codex"
        assert r2.current == "tests"
        assert ev1 == ["[T1] ⏳ stage: codex"]
        assert ev2 == ["[T2] ⏳ stage: tests"]

    def test_stages_tuple_contains_expected_values(self):
        for expected in (
            "sync_deps",
            "branch",
            "codex",
            "parse",
            "tests",
            "lint",
            "commit",
            "merge",
            "review",
        ):
            assert expected in STAGES
