"""BEH-21 (DT-05) — текст обязательства описывает новую границу.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Source: .../20-design.md §7
Traces: FR-08
Acceptance: AC-17

До #428 негативный контроль был единственным условием класса без
исполняемого гейта, и текст честно говорил ревьюеру «you are the only
check». Теперь машина исполняет контроль ДО платного ревью (§4a) и до
ревьюера доезжает только удовлетворённый вердикт — говорить ему «ты
единственная проверка» значило бы утверждать как несостоявшееся то, что
уже произошло, и просить проверить то, что машина уже показала. Предмет
ревью сдвигается: не «есть ли эвиденция», а соответствует ли приложенный
мутант заявленному свойству.
"""

from __future__ import annotations

from spec_runner.config import WAIVER_REVIEW_OBLIGATIONS, ExecutorConfig
from spec_runner.review import append_waiver_obligation
from spec_runner.task import Task

WAIVER = "characterisation · sanction: batch-approve-2026-09-09"


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-008",
        "name": "characterisation",
        "priority": "p2",
        "status": "in_progress",
        "estimate": "0.5d",
        "execution_mode": "standard",
        "tdd_waiver": WAIVER,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _block(tmp_path) -> str:
    """Блок обязательства, как он приходит в промпт ревьюера."""
    cfg = ExecutorConfig(project_root=tmp_path)
    return append_waiver_obligation("BODY", cfg, _task())


class TestBEH21TheObligationDescribesTheNewBoundary:
    """kind: unit — BEH-21."""

    def test_the_reviewer_is_no_longer_told_it_is_the_only_check(self, tmp_path):
        block = _block(tmp_path)

        assert "only check" not in block.lower(), block
        assert "no gate anywhere" not in block.lower(), block

    def test_it_says_the_machine_showed_discrimination_against_the_applied_mutant(self, tmp_path):
        block = _block(tmp_path).lower()

        assert "mutant" in block, block
        assert "applied" in block or "against" in block, block
        # «Показала», а не «покажет» / «должна показать»: факт состоявшийся.
        assert "has already" in block or "already" in block, block

    def test_it_names_the_subject_of_review_as_mutant_versus_property(self, tmp_path):
        block = _block(tmp_path).lower()

        assert "property" in block, block
        assert "matches" in block or "corresponds" in block or "really breaks" in block, block

    def test_the_text_still_comes_from_the_class_not_the_marker(self, tmp_path):
        """Два маркера одного класса с РАЗНЫМИ словами дают один и тот же
        текст обязательства. Первая редакция сравнивала вход сам с собой —
        тест, который не умеет падать."""
        cfg = ExecutorConfig(project_root=tmp_path)
        by_batch = append_waiver_obligation("BODY", cfg, _task())
        by_ref = append_waiver_obligation(
            "BODY", cfg, _task(tdd_waiver="characterisation · sanction: spec-runner#428")
        )

        obligation = WAIVER_REVIEW_OBLIGATIONS["characterisation"]
        assert obligation in by_batch and obligation in by_ref
        # Различаются ТОЛЬКО строкой санкции — слова маркера в текст
        # обязательства не проникают.
        diff = set(by_batch.splitlines()) ^ set(by_ref.splitlines())
        assert diff == {
            "Sanction: batch-approve-2026-09-09",
            "Sanction: spec-runner#428",
        }, diff
