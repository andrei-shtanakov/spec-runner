"""Исполняемый negative control для characterisation-задач (#428).

Пока здесь только то, что отказывает waived-задаче **до** работы:
конфигурация, при которой контроль не может быть исполнен в принципе, и
кандидат-коммит, которого нет. Исполнение обеих половин и классификация
приходят с DT-02/DT-03.

Контракт: `workstreams/executable-negative-control-under-standard-20260921/spec/`
(design §4, §5; FR-09, FR-07).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .task import Task


def _is_waived(task: Task, config: ExecutorConfig) -> bool:
    """Несёт ли задача ПРИМЕНИМЫЙ waiver.

    Нечитаемый маркер — не наш вопрос: его отвергает `execute_task` выше по
    стеку и называет `validate`. Здесь он просто означает «не наша задача»,
    иначе один дефект получил бы два разных сообщения из двух мест.
    """
    from .config import ConfigError

    try:
        return config.resolve_waiver(task) is not None
    except ConfigError:
        return False


def structural_impossibility(task: Task, config: ExecutorConfig) -> str | None:
    """Названная причина, по которой контроль неисполним в принципе, или None.

    **Один предикат на весь класс, а не три разрозненных `if`** (design §4).
    Критерий членства — «известно до прогона и делает контроль
    неисполнимым»; перечень открыт по построению, и четвёртому члену место
    здесь же.

    Почему это отказ, а не instrument error: каждая причина
    детерминирована. Гейт на instrument error повторяет оценку
    `gate_recovery_attempts` раз, получает тот же ответ и отдаёт
    infrastructure — задача не завершается **никогда**, оператор читает
    «сломан харнесс» про исправный, и каждое возобновление стоит полного
    платного вызова, потому что единственная точка оценки стоит после него.

    Спрашивается только про waived-задачу: проект без waiver'ов за эту
    механику не платит ничем (NFR-02).
    """
    if not _is_waived(task, config):
        return None

    from .git_ops import is_composite_shell_command
    from .tdd_runners import adapter_for

    if not config.auto_commit:
        return (
            "negative control needs a candidate commit to judge, and "
            "auto_commit is off — there would be nothing to replay. Enable "
            "auto_commit, or drop the **TDD-waiver:** marker and show a "
            "baseline RED instead."
        )

    if is_composite_shell_command(config.test_command):
        return (
            f"negative control cannot narrow a composite test_command to one "
            f"selector: {config.test_command!r}. Guessing which component "
            "accepts a node id is how the wrong program runs and its answer "
            "is believed."
        )

    try:
        adapter_name = config.resolve_tdd_runner()
    except Exception as exc:  # ConfigError и всё, что резолвер сочтёт отказом
        return f"negative control cannot resolve the test adapter: {exc}"
    adapter = adapter_for(adapter_name) if adapter_name else None
    if adapter is None:
        return (
            f"negative control has no measured adapter for test_command "
            f"{config.test_command!r}: a runner whose exit codes were never "
            "measured cannot say what its red means."
        )

    control = task.negative_control
    if control is not None:
        parsed = adapter.parse_selector(control.selector)
        from .tdd_runners import SelectorRefusal

        if isinstance(parsed, SelectorRefusal):
            return (
                f"the declared negative-control selector is not one the "
                f"{adapter.name} adapter can read: {parsed.message}"
            )

    return None


def candidate_refusal(task: Task, config: ExecutorConfig, candidate_sha: str | None) -> str | None:
    """Отказ, когда судить нечего: кандидат-коммит не резолвится (FR-07).

    Пре-терминальный сайт при пустом кандидате логирует предупреждение и
    возвращает `None` **до** обхода реестра, то есть считает пройденными
    все гейты. Для задачи вне waiver'а это сегодняшнее поведение и здесь не
    меняется; для waived-задачи это дыра — контроль не исполнен, а задача
    завершилась.
    """
    if not _is_waived(task, config):
        return None
    if candidate_sha:
        return None
    return (
        "negative control cannot run: the candidate commit does not resolve, "
        "so there is nothing to replay the declared selector against. The "
        "task is not completed rather than silently passing every gate."
    )
