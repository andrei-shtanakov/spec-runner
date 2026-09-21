"""BEH-01, BEH-02, BEH-03 (DT-01) — разбор объявления негативного контроля.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md#BEH-01
Source: .../15-behaviour-spec.md#BEH-02, #BEH-03
Source: .../20-design.md §1
Traces: FR-01

Форма объявления — решение владельца (AP-10): отдельный маркер
`**Negative-control:** <путь> :: <селектор>`, грамматика `**TDD-waiver:**`
остаётся из двух полей и не трогается. Разделитель — ` :: ` с пробелами по
обе стороны именно затем, чтобы `::` внутри pytest-селектора
(`tests/test_x.py::test_y`) в делении не участвовал.

Предмет этого файла — только РАЗБОР. Что объявление значит, когда оно
годно и чем отказывает — BEH-04/14/15/16/23 и следующие задачи графа.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spec_runner.task import parse_tasks

WAIVER = "characterisation · sanction: batch-approve-2026-09-09"
PATCH = "spec/negative-controls/TASK-008.patch"
SELECTOR = "tests/test_x.py::test_y"


def _tasks_file(tmp_path: Path, *, waiver: str | None = WAIVER, control: str | None = None) -> Path:
    """Один и тот же файл на все случаи: меняются только две строки.

    Собирается из строк, а не из одного литерала, чтобы «нет маркера» и
    «маркер с негодным значением» отличались ровно одной строкой — иначе
    тест сравнивал бы два разных файла и приписывал разницу маркеру.
    """
    lines = [
        "## Tasks\n\n",
        "### TASK-008: characterisation\n",
        "P2 | TODO   Est: 0.5d\n\n",
        "Проза.\n",
        "**Mode:** standard\n",
    ]
    if waiver is not None:
        lines.append(f"**TDD-waiver:** {waiver}\n")
    if control is not None:
        lines.append(f"**Negative-control:** {control}\n")
    lines += ["\n**Checklist:**\n", "- [ ] пункт\n"]
    path = tmp_path / "tasks.md"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _task(tmp_path: Path, **kwargs):
    return {t.id: t for t in parse_tasks(_tasks_file(tmp_path, **kwargs))}["TASK-008"]


class TestBEH01DeclarationIsParsedIntoTwoFields:
    """kind: contract — BEH-01: путь и селектор доступны раздельно, и ни
    одно поле не содержит фрагмента другого."""

    def test_path_and_selector_are_separate(self, tmp_path):
        task = _task(tmp_path, control=f"{PATCH} :: {SELECTOR}")

        assert task.negative_control is not None, "объявление не разобрано"
        assert str(task.negative_control.patch) == PATCH
        assert task.negative_control.selector == SELECTOR
        assert task.negative_control_error is None

    def test_neither_field_carries_a_fragment_of_the_other(self, tmp_path):
        """Существенная половина BEH-01, и она не следует из предыдущего
        теста: деление могло бы «получиться» и оставить хвост."""
        task = _task(tmp_path, control=f"{PATCH} :: {SELECTOR}")

        assert "::" not in str(task.negative_control.patch), (
            f"путь несёт фрагмент селектора: {task.negative_control.patch!r}"
        )
        assert ".patch" not in task.negative_control.selector, (
            f"селектор несёт фрагмент пути: {task.negative_control.selector!r}"
        )

    def test_the_waiver_grammar_is_untouched(self, tmp_path):
        """`**TDD-waiver:**` остаётся из двух полей: третьего у него не
        появилось, и разбор прежнего значения не изменился."""
        task = _task(tmp_path, control=f"{PATCH} :: {SELECTOR}")

        assert task.tdd_waiver == WAIVER
        assert task.execution_mode == "standard"


class TestBEH02UnparseableDeclarationIsRefusedNotInterpreted:
    """kind: contract — BEH-02: каждая негодная форма даёт НАЗВАННЫЙ отказ,
    и ни одна не приводит к выбору одной из возможных интерпретаций."""

    @pytest.mark.parametrize(
        ("control", "names"),
        [
            (f"{SELECTOR}", "separator"),
            (f"{PATCH} :: a.patch :: {SELECTOR}", "ambiguous"),
            (f" :: {SELECTOR}", "empty path"),
            (f"{PATCH} :: ", "empty selector"),
        ],
        ids=["no-separator", "two-separators", "empty-path", "empty-selector"],
    )
    def test_each_defective_form_names_its_own_defect(self, tmp_path, control, names):
        """Не просто «отказано», а **чем именно** негодно.

        BEH-02 обещает, что текст говорит, чего не хватает или что
        неоднозначно. Без этой половины любой отказ на любой вход проходил
        бы тест — и, в частности, объявление с пустым путём можно было бы
        отвергать как «нет разделителя», отправляя оператора искать
        отсутствующую проблему.
        """
        task = _task(tmp_path, control=control)

        assert task.negative_control is None, (
            f"негодное объявление разобрано как годное: {task.negative_control!r}"
        )
        assert task.negative_control_error, f"отказ не назван для {control!r}"
        assert names in task.negative_control_error, (
            f"отказ не называет свой дефект ({names!r}): {task.negative_control_error!r}"
        )
        assert control.strip() in task.negative_control_error, (
            f"отказ не цитирует объявление: {task.negative_control_error!r}"
        )

    def test_two_separators_are_refused_not_split_at_the_first(self, tmp_path):
        """Главная половина BEH-02: неоднозначность — отказ, а не «возьмём
        первую точку деления». Иначе `a.patch :: b.patch :: t.py::x` тихо
        стал бы патчем `a.patch` с селектором `b.patch :: t.py::x`."""
        task = _task(tmp_path, control=f"{PATCH} :: a.patch :: {SELECTOR}")

        assert task.negative_control is None
        assert task.negative_control_error


class TestBEH03MarkerAndWaiverMustComeTogether:
    """kind: contract — BEH-03: объявление без waiver'а и waiver без
    объявления — оба дефект; задача без обоих не меняется вовсе."""

    def test_a_control_without_a_waiver_is_parsed_and_left_to_validate(self, tmp_path):
        """Разбор не судит: он читает обе строки и отдаёт факт.

        «Контроль без снятой обязанности» — дефект ОБЪЯВЛЕНИЯ ЗАДАЧИ, а не
        синтаксиса строки, и называет его `validate` (BEH-03 в своей
        validate-половине). Парсер обязан лишь не потерять факт: маркер
        есть, waiver'а нет.
        """
        task = _task(tmp_path, waiver=None, control=f"{PATCH} :: {SELECTOR}")

        assert task.tdd_waiver is None
        assert task.negative_control is not None, "маркер потерян, и validate его не увидит"

    def test_a_waiver_without_a_control_leaves_both_fields_empty(self, tmp_path):
        task = _task(tmp_path, control=None)

        assert task.tdd_waiver == WAIVER
        assert task.negative_control is None
        assert task.negative_control_error is None, (
            "отсутствие маркера — не ошибка РАЗБОРА: строки нет, читать нечего"
        )

    def test_a_task_with_neither_is_byte_for_byte_what_it_was(self, tmp_path):
        """Дормантность на уровне разбора: обычная задача не меняется.

        Сравнивается ВЕСЬ `Task`, а не пара полей: правка парсера, задевшая
        что-то соседнее, иначе прошла бы мимо (BEH-03, последнее And).
        """
        import dataclasses

        task = _task(tmp_path, waiver=None, control=None)
        fields = dataclasses.asdict(task)

        assert fields["negative_control"] is None
        assert fields["negative_control_error"] is None
        assert fields["tdd_waiver"] is None
        assert fields["execution_mode"] == "standard"
        assert fields["id"] == "TASK-008"
        assert fields["status"] == "todo"
        assert fields["priority"] == "p2"
