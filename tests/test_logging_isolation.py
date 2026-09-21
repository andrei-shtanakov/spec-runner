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

from spec_runner import cli
from spec_runner.logging import get_logger, setup_logging


def test_a_tui_mode_drops_the_stderr_sink(tmp_path: Path, capsys) -> None:
    """Порча: TUI-режим переконфигурирует structlog без консольного приёмника.

    Это не дефект продакшена — `watch --tui` обязан освободить stderr, иначе
    он изуродует экран. Тест утверждает, что вызов ДЕЙСТВИТЕЛЬНО меняет
    глобальное состояние: без этого утверждения пара могла бы позеленеть
    вхолостую — перестань `setup_logging` ронять stderr-приёмник, и второй
    тест доказывал бы не изоляцию, а отсутствие порчи.
    """
    setup_logging(level="info", log_file=tmp_path / "logs" / "watch.log", tui_mode=True)

    get_logger("test").warning("into the file sink", task_ids=["TASK-998"])
    assert "TASK-998" not in capsys.readouterr().err, (
        "ожидалась порча: после TUI-режима предупреждение не должно попадать "
        "в stderr — если попадает, второй тест пары ничего не проверяет"
    )


def test_b_stderr_sink_survives_the_previous_test(capsys) -> None:
    """То, ради чего всё: предупреждение доезжает до stderr после теста выше."""
    get_logger("test").warning("canary for spec-runner#536", task_ids=["TASK-999"])
    assert "TASK-999" in capsys.readouterr().err, (
        "stderr-приёмник не восстановлен после теста, сменившего глобальную "
        "настройку логирования (#536): предупреждения следующих тестов уходят "
        "в файловый sink, и их проверки на capsys молча зеленеют или краснеют "
        "в зависимости от порядка файлов"
    )


def test_c_module_logger_caches_under_the_polluted_config(tmp_path: Path, capsys) -> None:
    """Вторая половина течи: кэш логгера, а не только настройка.

    При `cache_logger_on_first_use=True` первое обращение подменяет `bind` у
    прокси замыканием над уже собранным логгером, и `structlog.configure()`
    подмену не отменяет. Значит логгер модуля, впервые использованный ВНУТРИ
    испорченного теста, держал бы файловый sink до конца сессии — даже при
    восстановленной настройке.

    Здесь он кэшируется именно так: под испорченной конфигурацией. Холодным к
    началу теста его делает та же фикстура (она снимает кэш в teardown), и это
    условие, без которого тест ниже ничего не проверял бы.
    """
    setup_logging(level="info", log_file=tmp_path / "logs" / "watch.log", tui_mode=True)

    cli.logger.warning("caching under the polluted config", task_ids=["TASK-997"])
    assert "TASK-997" not in capsys.readouterr().err


def test_d_module_logger_is_rebuilt_after_restore(capsys) -> None:
    """И он пересобирается: сброс кэша в фикстуре обязателен, одного
    `configure()` мало — проверено мутацией."""
    cli.logger.warning("after restore", task_ids=["TASK-996"])
    assert "TASK-996" in capsys.readouterr().err, (
        "логгер модуля остался закэширован на файловом sink: восстановления "
        "настройки без сброса кэша недостаточно (#536)"
    )
