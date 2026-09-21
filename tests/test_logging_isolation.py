"""Глобальная настройка логирования не переживает тест, который её сменил.

spec-runner#536. `TestOrphanedSuccessWarning` падал при прогоне пары файлов
и проходил в полном сборе — разница была в порядке: pytest собирает файлы по
алфавиту, и `test_exit_contract` идёт до `test_watch`. Виноват был не он:
`TestWatchTui::test_tui_flag_launches_app` гоняет настоящий `cmd_watch` с
`tui=True`, тот зовёт `setup_logging(tui_mode=True)` → `obs.init_logging(
console=False)`, а это `structlog.configure()` — глобальная переконфигурация
ПРОЦЕССА, выбрасывающая stderr-приёмник. Возвращать её было некому, и все
последующие тесты сессии видели пустой stderr.

Класс дефекта, а не случай: любой тест, доехавший до кода, который
настраивает логирование, молча менял поведение остальных. Ловится он только
селективным прогоном — то есть ровно тем приёмом, которым пользуются при
отладке и в split-джобах CI.

Два теста ниже воспроизводят это как последовательность: первый портит,
второй проверяет, что порча не доехала. Порядок внутри файла —
определения, то есть детерминированный.
"""

from __future__ import annotations

from pathlib import Path

from spec_runner.logging import get_logger, setup_logging


def test_a_tui_mode_drops_the_stderr_sink(tmp_path: Path) -> None:
    """Порча: TUI-режим переконфигурирует structlog без консольного приёмника.

    Это не дефект продакшена — `watch --tui` обязан освободить stderr, иначе
    он изуродует экран. Тест лишь фиксирует, что вызов действительно меняет
    глобальное состояние: без этого второй тест ничего бы не доказывал.
    """
    setup_logging(level="info", log_file=tmp_path / "logs" / "watch.log", tui_mode=True)


def test_b_stderr_sink_survives_the_previous_test(capsys) -> None:
    """То, ради чего всё: предупреждение доезжает до stderr после теста выше."""
    get_logger("test").warning("canary for spec-runner#536", task_ids=["TASK-999"])
    assert "TASK-999" in capsys.readouterr().err, (
        "stderr-приёмник не восстановлен после теста, сменившего глобальную "
        "настройку логирования (#536): предупреждения следующих тестов уходят "
        "в файловый sink, и их проверки на capsys молча зеленеют или краснеют "
        "в зависимости от порядка файлов"
    )
