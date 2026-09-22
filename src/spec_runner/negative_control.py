"""Исполняемый negative control для characterisation-задач (#428).

Здесь живёт весь контроль, кроме шва реплея (`tdd._replay_selector`) и
проводки в прогон (`execution.py` — отказ до платного вызова, `hooks.py` —
исполнение до ревью и запись свидетельства, `gates.py` — чтение вердикта):

- отказ waived-задаче ДО работы: конфигурация, при которой контроль
  неисполним в принципе (`structural_impossibility`), и кандидат-коммит,
  которого нет (`candidate_refusal`);
- исполнение обеих половин в нужном порядке (`replay_both_halves`) и мера
  «стенд годен» (`clean_half_is_green`);
- классификация исходов в вердикт (`_classify_clean`/`_classify_mutated`,
  `run_negative_control`).

Разделительная линия между вердиктами одна и проходит везде одинаково:
детерминированный исход — факт об ОБЪЯВЛЕНИИ или работе (`unsatisfied`, без
переисполнений), неопределённый — о машине (`instrument_error`, с ними).
Половины отвечают на один и тот же вопрос, поэтому на один и тот же факт
обязаны отвечать одинаково — асимметрия здесь и была источником находок
кругов 9–12.

Контракт: `workstreams/executable-negative-control-under-standard-20260921/spec/`
(design §3, §4, §5; FR-02…FR-05, FR-07, FR-09).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
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
        path_refusal = patch_path_refusal(control.patch)
        if path_refusal is not None:
            return path_refusal
        parsed = adapter.parse_selector(control.selector)
        from .tdd_runners import SelectorRefusal

        if isinstance(parsed, SelectorRefusal):
            return (
                f"the declared negative-control selector is not one the "
                f"{adapter.name} adapter can read: {parsed.message}"
            )

    return None


def patch_path_refusal(patch) -> str | None:
    """Отказ, когда объявленный путь патча не может указывать в коммит.

    AP-01: патч берётся ИЗ КАНДИДАТ-КОММИТА и применяется к нему же. Единственной
    проверкой этого была `(worktree / mutate).is_file()`, но `pathlib` для
    абсолютного правого операнда возвращает сам абсолютный путь: объявление
    `/tmp/mutant.patch` читало файл ВНЕ одноразового дерева, `git apply`
    применял его, мутант ронял тест — и контроль объявлялся удовлетворённым
    патчем, которого нет ни в одном коммите. То же по `..`-пути.

    Факт статический: он виден по самой строке, до любого прогона. Поэтому
    здесь, а не в разборе (`task.py` судит ФОРМУ строки) и не в классификации
    (та судит исход прогона). Один читатель на три места — `validate`, предикат
    неисполнимости и сам шов реплея, — иначе они разойдутся в том, что считают
    путём внутрь дерева.

    Прецедент в этом же репо: `parse_group_element` отвергает элемент, который
    `resolves outside the repository`.
    """
    raw = PurePosixPath(patch)
    if raw.is_absolute():
        return (
            f"the declared negative-control patch {str(raw)!r} is an absolute path: a "
            "patch is read from the candidate commit, and an absolute path names a file "
            "on this machine that no commit contains"
        )
    depth = 0
    for part in raw.parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return (
                    f"the declared negative-control patch {str(raw)!r} resolves outside "
                    "the repository: a patch is read from the candidate commit, and "
                    "nothing outside the tree is in it"
                )
        elif part != ".":
            depth += 1
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
    if clean.stage == "run" and clean.outcome in (
        RunOutcome.SELECTION_FAILED,
        RunOutcome.COLLECTION_OR_COMPILE_ERROR,
    ):
        # На мутированной половине оба исхода уже названы фактами об
        # объявлении/работе («selected no test at all», «the mutant broke the
        # build»). На ЧИСТОЙ половине они тем более не про машину: без
        # всякого патча объявленный селектор не находит теста либо дерево
        # кандидата его не собирает. Ответ детерминирован — переисполнять
        # его значит платить полными прогонами за то же и отдавать exit 2
        # «о работе ничего не известно» про исправный инструмент.
        return ControlResult(
            "unsatisfied",
            "the declared selector did not yield a runnable test on the clean candidate: "
            "there is nothing for the mutant to turn red — the selector names no test in "
            f"this commit, or the commit does not collect it ({clean.detail[:200]})",
            clean=clean,
        )
    if clean.stage == "run" and clean.outcome is RunOutcome.TESTS_PASSED:
        # Тесты ПРОШЛИ, но прогон не доказал, что исполнился ровно
        # объявленный тест: селектор адресует больше одного (параметризация)
        # либо сводка несёт вторую не-нейтральную категорию. Ответ на это
        # детерминирован — переспрашивать нечего, — и относится к
        # ОБЪЯВЛЕНИЮ, а не к машине. Отнести его к стенду значило бы
        # переисполнять полные прогоны ради того же ответа, отдавать exit 2
        # «о работе ничего не известно» про исправный тест и перезапускать
        # задачу с новым платным вызовом: она не завершилась бы никогда.
        return ControlResult(
            "unsatisfied",
            "the declared selector did not run exactly one test on the clean "
            "candidate: the control compares one test's outcome with and without "
            "the mutant, and a selector that addresses several proves nothing about "
            f"any of them ({clean.detail[:200]})",
            clean=clean,
        )
    if clean.stage == "preflight" and clean.refusal_code in _PATCH_PREFLIGHT_CODES:
        # Та же таблица, что у мутированной половины. Эти коды говорят про
        # ОБЪЯВЛЕНИЕ — файла нет, строка не определяет теста, в файле нет
        # тестов, — и на чистой половине тем более: патч тут ни при чём.
        # Ответ детерминирован, переисполнять его нечем.
        return ControlResult(
            "unsatisfied",
            f"the declared selector does not describe a test in the candidate commit: "
            f"{clean.detail[:200]}",
            clean=clean,
        )
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
        if mutated.refusal_code == "patch_unreadable":
            # Сбой ввода-вывода — факт о МАШИНЕ: предъявлять его автору как
            # «патч не применяется» значит называть его работу негодной за
            # чужую поломку, и без переисполнений (BEH-11 б).
            return result("instrument_error", mutated.detail)
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
                config,
                sha=clean.sha,
                control=_ControlLike(clean.selector),
                mutate=None,
                order=3,
                # Спрашивается РАЗБОР, а не исход: прогон ответил бы на другой
                # вопрос и стоил бы полного разворачивания окружения (на
                # ExUnit — компиляции проекта). Бюджет Р-2: «+1 разбор, 0
                # прогонов».
                preflight_only=True,
            )
            # «Не удовлетворён» следует ТОЛЬКО из положительного наблюдения
            # «чистое дерево разбирается». Замыкающая строка читала любую
            # неудачу самого переспроса — не удался `git worktree add`,
            # таймаут, пропавший тулчейн, сорвавшийся разбор — как «патч
            # виноват»: автору предъявлялось как факт о его работе то, что
            # случилось с машиной между половинами, причём `retriable` при
            # таком вердикте False, то есть без единого ретрая. Отсутствие
            # наблюдения — не наблюдение.
            if recheck.stage == "preflight" and recheck.refusal_code is None:
                return result("unsatisfied", mutated.detail)
            if recheck.stage == "preflight" and recheck.refusal_code == "unparseable_test_file":
                return result(
                    "instrument_error",
                    f"the clean tree does not parse either: the toolchain is broken, "
                    f"not the patch ({mutated.detail})",
                )
            return result(
                "instrument_error",
                f"the recheck could not establish whether the clean tree parses "
                f"({recheck.stage}: {recheck.detail[:160]}), so what the mutated half "
                f"reported cannot be read as a fact about the patch ({mutated.detail})",
            )
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
    if mutated.outcome is RunOutcome.TESTS_PASSED:
        # Тот же факт, что и на чистой половине, — и ответ тот же. Прогон не
        # доказал, что исполнился ровно объявленный тест (мутант выключил его
        # `skipif`-ом, размножил параметризацией, добавил вторую категорию в
        # сводку). Разница между половинами по построению вызвана ПАТЧЕМ, и
        # ответ детерминирован: переисполнять его значит платить двумя
        # полными прогонами за тот же ответ и отдавать exit 2 «о работе
        # ничего не известно» про исправный стенд.
        return result(
            "unsatisfied",
            "under the mutant the declared selector did not run exactly one test, so "
            f"nothing can be concluded about the test it named ({mutated.detail[:200]})",
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
