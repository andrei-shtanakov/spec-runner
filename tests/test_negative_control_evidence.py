"""BEH-17, BEH-18, BEH-19 (DT-04) — durable-свидетельство контроля.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Source: .../20-design.md §6
Traces: FR-06

Свидетельство существует не для отчёта: без него «контроль был исполнен»
проверяется только тем, что задача завершилась, а это ровно то доверие,
которое механика и заменяет. Поэтому записываются ОБЕ половины — и для
неудовлетворённого контроля тоже.

Читается через `tdd_status.collect`, то есть одним читателем на текстовую
и JSON-поверхности: расхождение этих двух — дефект, который репо уже
ловило (находка круга 8 по PR #558).
"""

from __future__ import annotations

import json
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


def _cfg(tmp_path: Path) -> ExecutorConfig:
    (tmp_path / "spec").mkdir(parents=True, exist_ok=True)
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / "spec" / "state.db",
        logs_dir=tmp_path / "spec" / "logs",
    )


def _record(state: ExecutorState, cfg: ExecutorConfig, **overrides) -> dict:
    from spec_runner.tdd import resolve_namespace

    row: dict = {
        "task_id": "TASK-008",
        "namespace": resolve_namespace(cfg),
        "commit_sha": "abc1234def5678",
        "selector": "tests/test_x.py::test_y",
        "patch_blob_sha": "0" * 40,
        "clean_outcome": "tests_passed",
        "mutated_outcome": "tests_failed",
        "verdict": "satisfied",
        "environment_id": "uv.lock:deadbeef",
        "config_hash": "cfg0001",
    }
    row.update(overrides)
    state.record_negative_control(**row)
    return row


class TestBEH17BothHalvesAreRecorded:
    """kind: integration — BEH-17: запись несёт то, что делает её читаемой
    позже, и существует не только для успеха."""

    def test_the_row_carries_every_field_the_contract_names(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            expected = _record(state, cfg)
            rows = state.negative_controls(expected["namespace"])

        assert len(rows) == 1
        row = rows[0]
        for key, value in expected.items():
            assert row[key] == value, f"{key}: {row.get(key)!r} != {value!r}"
        assert row["timestamp"], "отметка времени не записана"

    def test_both_halves_are_told_apart(self, tmp_path):
        """Исход КАЖДОЙ половины отдельно: «красный мутант» и «зелёный
        оригинал» — разные факты, и слитая запись не различит контроль,
        который не гонял вторую половину."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg, verdict="unsatisfied", mutated_outcome="")
            rows = state.negative_controls(rows_ns(cfg))

        assert rows[0]["clean_outcome"] == "tests_passed"
        assert rows[0]["mutated_outcome"] == ""

    def test_an_unsatisfied_control_is_recorded_too(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg, verdict="unsatisfied")
            rows = state.negative_controls(rows_ns(cfg))

        assert rows[0]["verdict"] == "unsatisfied"

    def test_another_namespace_is_not_read_as_ours(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg, namespace="other-workstream")
            ours = state.negative_controls(rows_ns(cfg))

        assert ours == []


def rows_ns(cfg) -> str:
    from spec_runner.tdd import resolve_namespace

    return resolve_namespace(cfg)


class TestBEH18OneReaderTwoSurfaces:
    """kind: integration — BEH-18: `tdd status` и `--json` показывают одно и
    то же, потому что приходят из одного чтения."""

    def test_collect_carries_the_evidence(self, tmp_path):
        from spec_runner.tdd_status import collect

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg)

        data = collect(cfg, "TASK-008")

        assert data["negative_controls"], "свидетельства нет в чтении"
        assert data["negative_controls"][0]["verdict"] == "satisfied"

    def test_both_surfaces_agree(self, tmp_path):
        from spec_runner.tdd_status import collect, render

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg)

        data = collect(cfg, "TASK-008")
        text = render(data, "TASK-008")
        payload = json.loads(json.dumps(data))  # та же форма, что уходит в --json

        assert "satisfied" in text
        assert "abc1234def56" in text, f"кандидат-коммит не показан: {text}"
        assert payload["negative_controls"][0]["commit_sha"] == "abc1234def5678"

    def test_a_task_without_evidence_shows_none(self, tmp_path):
        from spec_runner.tdd_status import collect

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg, task_id="TASK-009")

        data = collect(cfg, "TASK-008")

        assert data["negative_controls"] == []


class TestBEH19AFailedWriteIsNotSuccess:
    """kind: integration — BEH-19: недоступная запись не роняет прогон и не
    читается позже как «контроль был удовлетворён»."""

    def test_a_broken_write_does_not_raise(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            monkeypatch.setattr(
                state,
                "_insert_negative_control",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk is gone")),
            )
            _record(state, cfg)  # не должно поднять
            rows = state.negative_controls(rows_ns(cfg))

        assert rows == [], "строка появилась там, где запись провалилась"

    def test_absent_evidence_is_not_read_as_satisfied(self, tmp_path):
        """Вердикт живёт в своём месте; свидетельство — объяснение, а не
        источник истины о допуске."""
        from spec_runner.tdd_status import collect

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-008", False, 0.0, error="x")

        data = collect(cfg, "TASK-008")

        assert data["negative_controls"] == []
        assert "satisfied" not in json.dumps(data["negative_controls"])


class TestEvidenceIsVisibleWithoutATaskId:
    """kind: integration — задача, у которой есть ТОЛЬКО свидетельство
    контроля, обязана попасть в список `tdd status`.

    Без этого текст печатает «(nothing recorded)», пока `--json` несёт
    строку: две поверхности одного чтения расходятся — дефект, который
    этот модуль и существует не допускать.
    """

    def test_a_task_with_only_control_evidence_is_listed(self, tmp_path):
        from spec_runner.tdd_status import collect, render

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            _record(state, cfg)

        data = collect(cfg, None)
        text = render(data, None)

        assert "TASK-008" in text, f"задача выпала из списка: {text}"
        assert "nothing recorded" not in text, text
        assert data["negative_controls"], "в JSON запись есть, а в тексте её не было бы"
