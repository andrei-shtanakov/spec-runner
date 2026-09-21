"""BEH-04, BEH-14, BEH-15, BEH-16, BEH-23 (DT-01) — статические отказы и
отказ до платного вызова.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Source: .../20-design.md §1, §4, §5
Traces: FR-01, FR-07, FR-09, FR-10

Предмет — всё, что отказывает waived-задаче **до** того, как начнётся
работа: негодное объявление, невыполнимая конфигурация, нерезолвящийся
кандидат-коммит. Исполнение контроля и вердикты — DT-02 и DT-03; здесь ни
одного прогона селектора не делается.

Две границы `validate` (design §1) проверяются отдельными тестами, потому
что нарушение каждой ломает фичу по-своему: без фильтра по статусу
закрытые waived-задачи этого же репо остановили бы каждый прогон, без
предупреждения вместо ошибки новая waived-задача не смогла бы стартовать
никогда.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.task import Task

WAIVER = "characterisation · sanction: batch-approve-2026-09-09"
PATCH = "spec/negative-controls/TASK-008.patch"
SELECTOR = "tests/test_x.py::test_y"
CONTROL = f"{PATCH} :: {SELECTOR}"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-008",
        "name": "characterisation",
        "priority": "p2",
        "status": "todo",
        "estimate": "0.5d",
        "execution_mode": "standard",
        "tdd_waiver": WAIVER,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _control(raw: str = CONTROL):
    """Разобранное объявление — через настоящий парсер, не литералом.

    Собирать `NegativeControl` руками значило бы пинать проверки против
    формы, которой парсер, может быть, и не отдаёт.
    """
    from spec_runner.task import _parse_negative_control

    parsed, refusal = _parse_negative_control(raw)
    assert refusal is None, refusal
    return parsed


def _validate(tasks, cfg):
    from spec_runner.validate import _validate_verify_first_declarations

    return _validate_verify_first_declarations(tasks, cfg)


class TestBEH15ValidateFiltersByStatus:
    """kind: contract — BEH-15: закрытая waived-задача не блокирует ничего.

    Без фильтра три `✅ DONE` задачи этого же репо (они несут
    `**TDD-waiver:**` и не несут `**Negative-control:**`, которого тогда не
    существовало) сделали бы `pre_result.ok` ложным, и `run`/`watch` не
    стартовали бы вовсе — ретроспективный эффект, который бандл сам
    запрещает (OUT-03).
    """

    def test_a_done_waived_task_without_a_control_is_not_an_error(self, tmp_path):
        done = _task(status="done")

        result = _validate([done], _cfg(tmp_path))

        assert not [e for e in result.errors if "TASK-008" in e], (
            f"закрытая задача блокирует прогон: {result.errors}"
        )

    def test_an_open_waived_task_without_a_control_is_an_error(self, tmp_path):
        """Вторая половина: фильтр не должен выключать проверку вовсе."""
        result = _validate([_task(status="todo")], _cfg(tmp_path))

        assert any("TASK-008" in e and "Negative-control" in e for e in result.errors), (
            f"незакрытая waived-задача без объявления не названа: {result.errors}"
        )

    def test_a_task_in_progress_is_still_checked(self, tmp_path):
        """`done` — единственный статус, снимающий проверку: задача в работе
        ещё дойдёт до гейта, и молчать про неё значит дать ей дойти."""
        result = _validate([_task(status="in_progress")], _cfg(tmp_path))

        assert any("TASK-008" in e for e in result.errors), result.errors


class TestBEH23MissingPatchIsAWarningNotAnError:
    """kind: contract — BEH-23: патч создаётся исполнением задачи.

    Ошибка здесь дала бы замкнутый круг: `validate` не пускает прогон →
    задача не исполняется → патч не пишется → `validate` снова не пускает.
    Репо уже решило этот же вопрос так же для verify-first
    (`validate.py`, `not_a_regular_file` → warning).
    """

    def test_absent_patch_warns_and_keeps_the_verdict_usable(self, tmp_path):
        result = _validate([_task(negative_control=_control())], _cfg(tmp_path))

        assert result.ok, f"отсутствующий патч сделал вердикт негодным: {result.errors}"
        assert any(PATCH in w for w in result.warnings), (
            f"отсутствие патча не названо предупреждением: {result.warnings}"
        )

    def test_present_patch_warns_about_nothing(self, tmp_path):
        (tmp_path / "spec" / "negative-controls").mkdir(parents=True)
        (tmp_path / PATCH).write_text("--- a\n+++ b\n", encoding="utf-8")

        result = _validate([_task(negative_control=_control())], _cfg(tmp_path))

        assert result.ok
        assert not [w for w in result.warnings if PATCH in w], result.warnings


class TestBEH03ValidateHalfControlWithoutWaiver:
    """kind: contract — BEH-03, validate-половина: контроль без снятой
    обязанности не имеет предмета."""

    def test_a_control_on_a_task_without_a_waiver_is_named(self, tmp_path):
        task = _task(tdd_waiver=None, negative_control=_control())

        result = _validate([task], _cfg(tmp_path))

        assert any("TASK-008" in e and "TDD-waiver" in e for e in result.errors), result.errors

    def test_an_unparseable_declaration_is_quoted_back(self, tmp_path):
        task = _task(negative_control_error="**Negative-control:** separator is missing")

        result = _validate([task], _cfg(tmp_path))

        assert any("TASK-008" in e and "separator" in e for e in result.errors), result.errors


class TestBEH14StructuralImpossibilityIsOnePredicate:
    """kind: contract — BEH-14: конфигурация, при которой контроль не может
    быть исполнен в принципе, отвергается по имени и потасково.

    Четыре члена проверяются одним и тем же предикатом: четвёртый появится,
    и место для него должно быть одно.
    """

    @pytest.mark.parametrize(
        ("overrides", "names"),
        [
            ({"auto_commit": False}, "auto_commit"),
            ({"test_command": "make lint && make test"}, "composite"),
            ({"test_command": "npm test", "tdd_runner": ""}, "adapter"),
        ],
        ids=["no-auto-commit", "composite-test-command", "unresolvable-adapter"],
    )
    def test_each_member_is_refused_by_name(self, tmp_path, overrides, names):
        from spec_runner.negative_control import structural_impossibility

        refusal = structural_impossibility(
            _task(negative_control=_control()), _cfg(tmp_path, **overrides)
        )

        assert refusal is not None, f"{overrides} не отвергнута"
        assert names in refusal.lower(), f"отказ не называет причину ({names}): {refusal!r}"

    def test_an_unparseable_selector_is_a_member_too(self, tmp_path):
        """Находка ревью: без этой строки неразбираемый селектор уходил бы в
        реплей и возвращался как instrument_error с ретраями — ровно тот
        вред, ради которого FR-09 и написан."""
        from spec_runner.negative_control import structural_impossibility

        refusal = structural_impossibility(
            _task(negative_control=_control("spec/x.patch :: not-a-node-id")), _cfg(tmp_path)
        )

        assert refusal is not None, "неразбираемый селектор не отвергнут до прогона"
        assert "selector" in refusal.lower(), refusal

    def test_a_workable_configuration_is_not_refused(self, tmp_path):
        """Половина «не отвергает всё подряд»."""
        from spec_runner.negative_control import structural_impossibility

        assert structural_impossibility(_task(negative_control=_control()), _cfg(tmp_path)) is None

    def test_a_task_without_a_waiver_is_never_refused(self, tmp_path):
        """Дормантность предиката: он спрашивается только про waived-задачу."""
        from spec_runner.negative_control import structural_impossibility

        plain = _task(tdd_waiver=None, negative_control=None)

        assert structural_impossibility(plain, _cfg(tmp_path, auto_commit=False)) is None


class TestBEH04RefusalCostsNoPaidCall:
    """kind: integration — BEH-04: негодное объявление отвергается до
    первого платного вызова, а не на пре-терминальном сайте."""

    def _repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        (root / "spec").mkdir(parents=True)
        for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
        (root / "README.md").write_text("x\n", encoding="utf-8")
        # Отказ происходит ПОСЛЕ `update_task_status`, то есть настоящий
        # прогон читает и пишет `tasks.md`. Стенд без него проверял бы не
        # тот путь.
        (root / "spec" / "tasks.md").write_text(
            "## Tasks\n\n### TASK-008: characterisation\n"
            "P2 | TODO   Est: 0.5d\n"
            f"**Mode:** standard\n**TDD-waiver:** {WAIVER}\n\n"
            "**Checklist:**\n- [ ] пункт\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "c"], cwd=root, capture_output=True, check=True)
        return root

    def test_a_waived_task_without_a_control_never_reaches_a_paid_call(self, tmp_path, monkeypatch):
        from spec_runner import execution
        from spec_runner.state import ExecutorState

        root = self._repo(tmp_path)
        cfg = _cfg(root, state_file=root / "spec" / "state.db", logs_dir=root / "spec" / "logs")
        paid: list = []
        monkeypatch.setattr(execution, "build_cli_invocation", lambda **k: paid.append(k) or None)

        with ExecutorState(cfg) as state:
            result = execution.execute_task(_task(negative_control=None), cfg, state)
            attempts = state.get_task_state("TASK-008").attempts

        # Отказ ТЕРМИНАЛЬНЫЙ, а не обычный провал: повторять его
        # `max_retries` раз со сном между попытками значит задавать один и
        # тот же вопрос тем же байтам и получать тот же ответ, медленнее.
        # Та же трактовка, что у несовместимости `auto_commit: false`
        # (#380).
        assert result == "TERMINAL_REFUSAL", result
        assert paid == [], "платный вызов состоялся, хотя объявления нет"
        assert attempts, "попытка обязана быть записана"
        assert "Negative-control" in (attempts[-1].error or ""), attempts[-1].error

    def test_an_impossible_configuration_is_refused_through_the_real_caller(
        self, tmp_path, monkeypatch
    ):
        """Проводка предиката, а не сам предикат.

        Предикат проверен выше поштучно, но объявленный и никем не
        читаемый предикат — отдельный класс дефекта, за который этот репо
        уже платил (`supports_file_targets`, #448): мутация «убрать вызов
        из `execute_task`» без этого теста проходит зелёной.
        """
        from spec_runner import execution
        from spec_runner.state import ExecutorState

        root = self._repo(tmp_path)
        cfg = _cfg(
            root,
            state_file=root / "spec" / "state.db",
            logs_dir=root / "spec" / "logs",
            test_command="make lint && make test",
        )
        paid: list = []
        monkeypatch.setattr(execution, "build_cli_invocation", lambda **k: paid.append(k) or None)

        with ExecutorState(cfg) as state:
            result = execution.execute_task(_task(negative_control=_control()), cfg, state)
            attempts = state.get_task_state("TASK-008").attempts

        assert result == "TERMINAL_REFUSAL", result
        assert paid == [], "платный вызов состоялся при неисполнимой конфигурации"
        assert "composite" in (attempts[-1].error or "").lower(), attempts[-1].error


class TestBEH16UnresolvableCandidateIsRefusedNotSkipped:
    """kind: contract — BEH-16: пустой кандидат-коммит заставляет
    пре-терминальный сайт считать пройденными ВСЕ гейты; для waived-задачи
    это дыра, и она закрывается отказом, а не молчанием."""

    def test_an_empty_candidate_refuses_a_waived_task(self, tmp_path):
        from spec_runner.negative_control import candidate_refusal

        refusal = candidate_refusal(_task(negative_control=_control()), _cfg(tmp_path), "")

        assert refusal is not None, "waived-задача завершилась бы без исполненного контроля"
        assert "candidate" in refusal.lower(), refusal

    def test_a_resolved_candidate_is_not_refused(self, tmp_path):
        from spec_runner.negative_control import candidate_refusal

        assert (
            candidate_refusal(_task(negative_control=_control()), _cfg(tmp_path), "abc1234") is None
        )

    def test_a_task_without_a_waiver_is_untouched(self, tmp_path):
        """Общая ветка не трогается: для задач вне waiver'а поведение
        пустого кандидата остаётся сегодняшним."""
        from spec_runner.negative_control import candidate_refusal

        plain = _task(tdd_waiver=None, negative_control=None)

        assert candidate_refusal(plain, _cfg(tmp_path), "") is None


class TestThePatchPathIsBoundToTheCommitTree:
    """kind: contract — находка ревью цепи DT-01…DT-04 (блокирующая).

    AP-01: патч берётся ИЗ КАНДИДАТ-КОММИТА. Единственной проверкой этого
    была `(worktree / mutate).is_file()`, а `pathlib` для абсолютного
    правого операнда возвращает сам абсолютный путь — то есть объявление
    `/tmp/mutant.patch` читало файл ВНЕ одноразового дерева, `git apply`
    применял его, мутант ронял тест, и контроль объявлялся удовлетворённым
    патчем, которого нет ни в одном коммите. Ровно та дисциплина, которая
    у соседней фичи уже есть: `parse_group_element` отвергает
    `outside_repository`.
    """

    @pytest.mark.parametrize(
        "path",
        ["/tmp/mutant.patch", "../outside/mutant.patch", "spec/../../mutant.patch"],
        ids=["absolute", "parent-escape", "escape-mid-path"],
    )
    def test_a_path_outside_the_tree_is_refused_before_any_run(self, tmp_path, path):
        from spec_runner.negative_control import structural_impossibility

        refusal = structural_impossibility(
            _task(negative_control=_control(f"{path} :: tests/test_x.py::test_y")),
            _cfg(tmp_path),
        )

        assert refusal is not None, f"путь {path!r} не отвергнут"
        assert "patch" in refusal.lower(), refusal

    def test_a_path_inside_the_tree_is_not_refused(self, tmp_path):
        from spec_runner.negative_control import structural_impossibility

        refusal = structural_impossibility(
            _task(negative_control=_control("spec/negative-controls/TASK-008.patch :: t.py::a")),
            _cfg(tmp_path),
        )

        assert refusal is None, refusal

    def test_validate_refuses_the_same_path_rather_than_finding_the_file(self, tmp_path):
        """`validate` читает путь через `project_root / patch`, и для
        абсолютного операнда это тот же файл на машине оператора: он
        существует, проверка молчит, и объявление доезжает до прогона."""
        outside = tmp_path / "mutant.patch"
        outside.write_text("--- a/x\n", encoding="utf-8")
        task = _task(negative_control=_control(f"{outside} :: tests/test_x.py::test_y"))

        result = _validate([task], _cfg(tmp_path))

        assert any("TASK-008" in e and "patch" in e.lower() for e in result.errors), result.errors


class TestARefusalBeforeThePaidCallLeavesTheTaskReady:
    """kind: integration — находка ревью цепи.

    Обе соседние отказные ветки (`claims` выше, запись waiver'а ниже) явно
    возвращают статус в `todo`, потому что флип в `in_progress` уже
    произошёл. Ветка контроля этого не делала, и харнессовый флип оставался
    в `tasks.md` незакоммиченным: следующий прогон либо уносил его в
    `git stash` через `rescue_uncommitted`, либо коммитил через
    `recover_interrupted_flip`, печатая «Recovered an interrupted run» про
    задачу, которая ни разу не запускалась.
    """

    def test_the_task_is_left_todo_not_in_progress(self, tmp_path, monkeypatch):
        from spec_runner import execution
        from spec_runner.state import ExecutorState

        root = TestBEH04RefusalCostsNoPaidCall()._repo(tmp_path)
        cfg = _cfg(root, state_file=root / "spec" / "state.db", logs_dir=root / "spec" / "logs")
        monkeypatch.setattr(execution, "build_cli_invocation", lambda **k: None)

        with ExecutorState(cfg) as state:
            result = execution.execute_task(_task(negative_control=None), cfg, state)

        assert result == "TERMINAL_REFUSAL", result
        body = (root / "spec" / "tasks.md").read_text(encoding="utf-8")
        assert "IN_PROGRESS" not in body, f"флип не отменён:\n{body}"
        assert "TODO" in body, body


class TestOneDefectIsReportedOnce:
    """kind: contract — находка ревью цепи DT-01…DT-04.

    `**TDD-waiver:**` на задаче, которая не `standard`, поднимает
    `ConfigError`; обработчик пишет ошибку и ставит `waiver = None`. Ветка
    контроля читала это значение как «маркера нет» и добавляла вторую
    строку — неверную: «carries no **TDD-waiver:**» про задачу, которая
    маркер несёт. Оператор уходил искать то, что у него есть. Правило про
    одну ошибку двумя читателями в этом файле уже сформулировано (#431 п.3).
    """

    def test_a_waiver_that_does_not_resolve_is_not_called_absent(self, tmp_path):
        task = _task(
            execution_mode="tdd",
            tdd_waiver=WAIVER,
            negative_control=_control(),
        )

        result = _validate([task], _cfg(tmp_path))

        assert result.errors, "нерезолвящийся waiver обязан оставаться ошибкой"
        assert not any("carries no **TDD-waiver:**" in e for e in result.errors), result.errors

    def test_a_genuinely_absent_waiver_is_still_named(self, tmp_path):
        """Починка не должна проглотить настоящий случай."""
        task = _task(tdd_waiver=None, negative_control=_control())

        result = _validate([task], _cfg(tmp_path))

        assert any("TDD-waiver" in e for e in result.errors), result.errors


class TestTheRefusalIsTypedAsPolicy:
    """kind: contract — находка ревью круга 7.

    AC-11 требует, чтобы ни один член класса структурной невозможности не
    давал «переисполнений И infrastructure-исхода». Отказ шёл через
    `refusal_for(INSTRUMENT_ERROR, …)` → `ErrorCode.INFRASTRUCTURE` → exit 2,
    то есть CI и дашборд читали «сломан инструмент, о работе ничего не
    известно» там, где дефектно ОБЪЯВЛЕНИЕ. Design §4 предписывал
    `RefusalKind.INSTRUMENT` — противоречие в самой базе; авторитетна
    приёмка, по прецеденту владельца (AP-12: FR-04 против AP-08).
    """

    def test_a_declaration_defect_is_a_policy_refusal_not_a_broken_machine(
        self, tmp_path, monkeypatch
    ):
        from spec_runner import execution
        from spec_runner.state import ErrorCode, ExecutorState

        root = TestBEH04RefusalCostsNoPaidCall()._repo(tmp_path)
        cfg = _cfg(root, state_file=root / "spec" / "state.db", logs_dir=root / "spec" / "logs")
        monkeypatch.setattr(execution, "build_cli_invocation", lambda **k: None)

        with ExecutorState(cfg) as state:
            result = execution.execute_task(_task(negative_control=None), cfg, state)
            attempt = state.get_task_state("TASK-008").attempts[-1]

        assert result == "TERMINAL_REFUSAL", result
        assert attempt.error_code == ErrorCode.HOOK_FAILURE, attempt.error_code
        assert attempt.error_kind != "instrument", attempt.error_kind


class TestTheCanonicalPatchPathIsCommittable:
    """kind: contract — находка ревью круга 7 (блокирующая).

    Канонический путь мутанта — `spec/negative-controls/<TASK-ID>.patch`, а
    `/spec/*` в .gitignore этого репо игнорирует его: задача физически не
    может доставить патч в кандидат-коммит, контроль всегда `patch_absent`
    → `unsatisfied`, и waived-задача не завершается никогда. Фича была бы
    неприменима в собственном репозитории — проверяется на самом .gitignore,
    потому что доказывает это git, а не прочтение правила.
    """

    def test_git_does_not_ignore_a_control_patch(self):
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        probe = "spec/negative-controls/TASK-999.patch"
        # БЕЗ `-v`: с ним git печатает и отрицающий паттерн, и код выхода
        # становится 0 у пути, который на самом деле НЕ игнорируется, —
        # измерялась бы не та величина. Без флага rc=1 значит «не игнорируется».
        ignored = subprocess.run(
            ["git", "check-ignore", probe], cwd=root, capture_output=True, text=True
        )
        why = subprocess.run(
            ["git", "check-ignore", "-v", probe], cwd=root, capture_output=True, text=True
        )

        assert ignored.returncode == 1, (
            f"канонический путь мутанта игнорируется: {why.stdout.strip()}"
        )


class TestTwoIndependentDefectsAreBothNamed:
    """kind: contract — находка ревью круга 7, обратная сторона починки
    круга 1.

    Молчание при нерезолвящемся waiver'е убрало ложную строку «маркера
    нет», но вместе с ней — и НЕЗАВИСИМУЮ ошибку: неразбираемое
    `**Negative-control:**` перестало называться вовсе, и оператор,
    починив waiver, возвращался за вторым кругом за тем, что было видно
    сразу. `validate` существует ровно затем, чтобы этого не было.
    """

    def test_an_unparseable_declaration_is_named_even_when_the_waiver_fails(self, tmp_path):
        task = _task(
            execution_mode="tdd",  # из-за этого waiver не резолвится
            tdd_waiver=WAIVER,
            negative_control_error="**Negative-control:** separator ' :: ' is missing",
        )

        result = _validate([task], _cfg(tmp_path))

        assert any("execution mode" in e for e in result.errors), result.errors
        assert any("separator" in e for e in result.errors), (
            f"независимый дефект объявления проглочен: {result.errors}"
        )

    def test_the_false_line_stays_gone(self, tmp_path):
        task = _task(execution_mode="tdd", tdd_waiver=WAIVER, negative_control=_control())

        result = _validate([task], _cfg(tmp_path))

        assert not any("carries no **TDD-waiver:**" in e for e in result.errors), result.errors


class TestValidateWarnsAboutAnIgnoredPatch:
    """kind: contract — вторая половина той же находки: `validate` видит
    файл на месте и молчит, хотя `git add -A` его не застейджит. Названо
    ДО прогона — там, где починка ещё бесплатна."""

    def test_a_patch_git_ignores_is_named_before_the_run(self, tmp_path):
        import subprocess

        root = tmp_path / "repo"
        (root / "spec" / "negative-controls").mkdir(parents=True)
        (root / PATCH).write_text("--- a/x\n", encoding="utf-8")
        (root / ".gitignore").write_text("spec/negative-controls/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

        result = _validate([_task(negative_control=_control())], _cfg(root))

        assert any("ignore" in w.lower() for w in result.warnings + result.errors), (
            f"игнорируемый патч не назван: {result.warnings + result.errors}"
        )
