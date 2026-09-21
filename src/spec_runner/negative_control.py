"""Исполняемый negative control для characterisation-задач (#428).

Пока здесь только то, что отказывает waived-задаче **до** работы:
конфигурация, при которой контроль не может быть исполнен в принципе, и
кандидат-коммит, которого нет. Исполнение обеих половин и классификация
приходят с DT-02/DT-03.

Контракт: `workstreams/executable-negative-control-under-standard-20260921/spec/`
(design §4, §5; FR-09, FR-07).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .task import Task
    from .tdd import ReplayAttempt


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


def clean_half_is_green(attempt) -> bool:
    """Прошла ли чистая половина так, чтобы мутированную имело смысл гонять.

    Мера — `execution_proven` verify-first-класса, а НЕ
    `SelectionProof.PROVEN` (AP-12.1, Р-1). Причина структурная: `PROVEN`
    для прошедшего теста на pytest недостижим там, где сводка несёт вторую
    категорию (`1 passed, 1 warning`), потому что node id печатается только
    у падения. Требовать `PROVEN` у зелёного значило бы instrument_error на
    каждом прогоне обычного проекта.

    Определение КРАСНОГО (`TESTS_FAILED` + `PROVEN`) этим не трогается: оно
    про падения, где мера надёжна, и живёт в таблице классификации.
    """
    from .tdd_runners import RunOutcome

    return (
        attempt.stage == "run"
        and attempt.outcome is RunOutcome.TESTS_PASSED
        and bool(attempt.execution_proven)
    )


def _replay_for_control(config, *, sha: str, control, mutate, order: int, preflight_only=False):
    """Один вызов шва — отдельной функцией, чтобы тест мог подменить ОДНУ
    половину, не трогая другую.

    Без этого шва «стенд сломался между половинами» (BEH-12) нечем
    предъявить: настоящий таймаут или исчезнувший тулчейн в тесте
    невоспроизводим дёшево, а подменять весь реплей значило бы перестать
    проверять чистую половину живьём.
    """
    from .tdd import _replay_selector

    return _replay_selector(
        config,
        sha=sha,
        selector=control.selector,
        mutate=mutate,
        order=order,
        preflight_only=preflight_only,
    )


def replay_both_halves(config: ExecutorConfig, *, sha: str, control):
    """`(чистая, мутированная|None)` — исполнение контроля без вердикта.

    Порядок фиксирован (AP-08): чистая половина первой, и она служит
    доказательством исправности стенда. Мутированная не запускается вовсе,
    пока стенд не доказан, — негодный стенд стоит один прогон, а не два
    (NFR-04).
    """
    clean = _replay_for_control(config, sha=sha, control=control, mutate=None, order=1)
    if not clean_half_is_green(clean):
        return clean, None
    mutated = _replay_for_control(config, sha=sha, control=control, mutate=control.patch, order=2)
    return clean, mutated


@dataclass(frozen=True)
class ControlResult:
    """Вердикт контроля и факты, на которых он получен (design §3)."""

    verdict: str  # satisfied | unsatisfied | instrument_error
    detail: str
    clean: ReplayAttempt | None = None
    mutated: ReplayAttempt | None = None
    sha: str = ""

    @property
    def retriable(self) -> bool:
        """Повторяется ТОЛЬКО инструментальная неудача.

        `satisfied` и `unsatisfied` детерминированы: повторять их значит
        задавать тот же вопрос тем же байтам. Повторяет исполнитель
        контроля, а не гейт (Р-3): гейт читает готовый вердикт и перечитал
        бы тот же кэш.
        """
        return self.verdict == "instrument_error"


#: Коды preflight, детерминированно вызванные содержимым патча: на чистой
#: половине та же стадия прошла, значит разница между половинами — патч.
_PATCH_PREFLIGHT_CODES = frozenset(
    {"not_a_definition_line", "missing_test_file", "no_tests_in_file"}
)
#: Коды preflight про МАШИНУ, а не про патч. Отнести их к патчу значило бы
#: сказать автору «твой мутант не различил», когда пропал тулчейн.
_STAND_PREFLIGHT_CODES = frozenset({"runner_toolchain_missing", "preflight_failed"})


def _classify_clean(clean) -> ControlResult | None:
    """Чистая половина: три строки, сверху вниз (design §3).

    Строка «красен без мутанта» стоит ПЕРВОЙ и судится `PROVEN`: для
    падения мера надёжна, а FR-05 прямо запрещает отправлять этот вердикт в
    instrument — иначе оператор читает «сломан харнесс» про исправный.
    """
    from .tdd_runners import RunOutcome, SelectionProof

    if (
        clean.stage == "run"
        and clean.outcome is RunOutcome.TESTS_FAILED
        and clean.proof is SelectionProof.PROVEN
    ):
        return ControlResult(
            "unsatisfied",
            "the task's test is already red on the clean candidate: a test that fails "
            "without the mutant proves nothing about the mutant",
            clean=clean,
        )
    if clean_half_is_green(clean):
        return None
    return ControlResult(
        "instrument_error",
        f"the clean half reached no verdict ({clean.stage}): {clean.detail}",
        clean=clean,
    )


def _classify_mutated(config, clean, mutated) -> ControlResult:
    """Мутированная половина: восемь строк, сверху вниз (design §3).

    Строки взаимно исключающи по построению, читается первое совпадение.
    """
    from .tdd_runners import RunOutcome, SelectionProof

    def result(verdict: str, detail: str) -> ControlResult:
        return ControlResult(verdict, detail, clean=clean, mutated=mutated)

    if mutated.stage == "mutate":
        if mutated.refusal_code in ("patch_absent", "patch_inapplicable"):
            # AP-12.4: оба — факты о РАБОТЕ задачи, а не о стенде.
            # Применимость детерминирована: патч и цель один коммит.
            return result("unsatisfied", mutated.detail)
        return result("instrument_error", mutated.detail)

    if mutated.stage == "preflight":
        if mutated.refusal_code in _STAND_PREFLIGHT_CODES:
            return result("instrument_error", mutated.detail)
        if mutated.refusal_code == "unparseable_test_file":
            # Единственный код, который нельзя отнести заранее: адаптер
            # возвращает его и при настоящей ошибке разбора, и при сбое
            # самого разбора. Сомнение снимается НАБЛЮДЕНИЕМ — разбор
            # переспрашивается на чистом дереве, — а не толкованием.
            recheck = _replay_for_control(
                config, sha=clean.sha, control=_ControlLike(clean.selector), mutate=None, order=3
            )
            if recheck.stage == "preflight" and recheck.refusal_code == "unparseable_test_file":
                return result(
                    "instrument_error",
                    f"the clean tree does not parse either: the toolchain is broken, "
                    f"not the patch ({mutated.detail})",
                )
            return result("unsatisfied", mutated.detail)
        if mutated.refusal_code in _PATCH_PREFLIGHT_CODES:
            return result("unsatisfied", mutated.detail)
        return result("instrument_error", mutated.detail)

    if mutated.stage != "run":
        return result("instrument_error", mutated.detail)

    if (
        clean.selector_identity is not None
        and mutated.selector_identity is not None
        and clean.selector_identity != mutated.selector_identity
    ):
        return result(
            "unsatisfied",
            "the patch changed the test's declaration line, so the declared selector no "
            "longer addresses what it declared",
        )
    if mutated.outcome in (RunOutcome.RUNNER_ERROR, RunOutcome.UNRECOGNIZED):
        return result("instrument_error", f"the runner itself failed: {mutated.detail}")
    if mutated.proof is SelectionProof.REFUTED:
        return result("unsatisfied", "a test other than the declared one was executed")
    if mutated.outcome is RunOutcome.COLLECTION_OR_COMPILE_ERROR:
        return result("unsatisfied", "the mutant broke the build, so the declared test never ran")
    if mutated.outcome is RunOutcome.SELECTION_FAILED:
        return result("unsatisfied", "after the patch the selector selected no test at all")
    if mutated.outcome is RunOutcome.TESTS_FAILED and mutated.proof is SelectionProof.PROVEN:
        return result("satisfied", "the declared test goes red under the declared mutant")
    if mutated.outcome is RunOutcome.TESTS_PASSED and mutated.execution_proven:
        return result(
            "unsatisfied",
            "the mutant did not discriminate: the test stayed green under it, so it does "
            "not prove the test can fail",
        )
    return result("instrument_error", f"the mutated half reached no verdict: {mutated.detail}")


class _ControlLike:
    """Минимальный носитель селектора для переспроса разбора."""

    def __init__(self, selector: str) -> None:
        self.selector = selector


def run_negative_control(config: ExecutorConfig, *, sha: str, control) -> ControlResult:
    """Исполнить контроль и вынести вердикт (design §3).

    Исполнение и классификация разделены намеренно: `ReplayAttempt` — факт
    о прогоне, таблица — его толкование, и они не должны существовать в
    двух редакциях одного правила.
    """
    clean, mutated = replay_both_halves(config, sha=sha, control=control)
    early = _classify_clean(clean)
    if early is not None:
        return ControlResult(early.verdict, early.detail, clean=clean, mutated=None, sha=sha)
    assert mutated is not None
    verdict = _classify_mutated(config, clean, mutated)
    return ControlResult(verdict.verdict, verdict.detail, clean=clean, mutated=mutated, sha=sha)
