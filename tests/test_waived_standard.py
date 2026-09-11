"""#429: адресный TDD-waiver — механика режима `standard` для waived-задач.

Waiver снимает ОДНО — обязательный baseline-RED. Всё остальное остаётся в
силе: active claims проверяются на всех трёх точках, frozen-files block
доезжает до каждого платного промпта, а применение санкции пишется durable.

Вторая половина каждой проверки — **неизменность обычного режима**: без неё
правка, ломающая обычный `standard`, выглядела бы исправной, потому что
waived-половина зеленела бы одна.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import append_frozen_files, record_claims
from spec_runner.config import AppliedWaiver, ConfigError, ExecutorConfig
from spec_runner.gates import (
    GateContext,
    GateStatus,
    PhaseOutcome,
    ensure_red_gate,
    evaluate_claims,
    has_gates,
)
from spec_runner.state import ExecutorState
from spec_runner.task import Task, parse_tasks
from spec_runner.tdd import RedCheckpoint, RedOutcome, resolve_namespace

WAIVER = "characterisation · sanction: batch-approve-2026-09-09"


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
    }
    defaults.update(overrides)
    return Task(**defaults)


# --- ВЕТКИ ОТКАЗА: резолвер --------------------------------------------
#
# Отказы написаны первыми. Резолвер, у которого проверен только проход,
# свидетельствует о себе ровно столько же, сколько гвард, не смотревший
# половину цели: «не отказал» неотличимо от «не проверял».


class TestTheResolverRefusesWhatItCannotRecognise:
    def test_a_marker_on_a_task_that_is_not_standard_is_refused(self, tmp_path):
        """У `tdd`/`verify_first` свой baseline-RED, и снимать им нечего.

        Маркер здесь не избыточен, а противоречив: он объявлял бы задаче
        освобождение от требования, которого её режим не предъявляет.
        """
        config = _cfg(tmp_path, execution_mode="tdd")
        task = _task(execution_mode="tdd", tdd_waiver=WAIVER)
        with pytest.raises(ConfigError) as refusal:
            config.resolve_waiver(task)
        assert "TASK-008" in str(refusal.value)
        assert "standard" in str(refusal.value)

    @pytest.mark.parametrize(
        "marker",
        [
            "выдумка · sanction: batch-approve-2026-09-09",
            "characterisation · sanction: потому-что-можно",
            "characterisation · sanction: batch-approve-2026-13-45",
            "characterisation · sanction: batch-approve-2026-9-9",
            "characterisation",
            "· sanction: batch-approve-2026-09-09",
            "characterisation · batch-approve-2026-09-09",
        ],
        ids=[
            "класс-вне-словаря", "санкция-свободный-текст",
            "дата-несуществующая", "дата-не-ISO", "санкции-нет",
            "класса-нет", "ключа-sanction-нет",
        ],
    )
    def test_a_malformed_marker_is_refused_by_name(self, tmp_path, marker):
        """Молчаливый `None` вернул бы задачу в проектный режим — то есть
        под red-гейт, который её остановит, — и оператор узнал бы об этом
        отказом не про то."""
        config = _cfg(tmp_path, execution_mode="tdd")
        task = _task(execution_mode="standard", tdd_waiver=marker)
        with pytest.raises(ConfigError) as refusal:
            config.resolve_waiver(task)
        assert "TASK-008" in str(refusal.value)

    @pytest.mark.parametrize(
        "sanction",
        ["batch-approve-2026-09-09", "spec-runner#429", "devtools#1"],
        ids=["датированное-решение", "ссылка-на-PR", "однозначный-номер"],
    )
    def test_both_sanction_forms_are_accepted(self, tmp_path, sanction):
        """Вторая половина: грамматика, отвергающая ВСЁ, на одних отказных
        тестах выглядела бы исправной."""
        config = _cfg(tmp_path, execution_mode="tdd")
        task = _task(
            execution_mode="standard",
            tdd_waiver=f"characterisation · sanction: {sanction}",
        )
        assert config.resolve_waiver(task) == AppliedWaiver(
            node_class="characterisation", sanction=sanction
        )


class TestOrdinaryModesAreUnchanged:
    """Половина «ничего не сломано». Без неё правка, ломающая обычный
    `standard`, зеленела бы: waived-тесты проходят сами по себе."""

    @pytest.mark.parametrize(
        "project, task_mode",
        [("standard", None), ("tdd", None), ("verify_first", None),
         ("standard", "tdd"), ("tdd", "standard"), ("tdd", "verify_first")],
    )
    def test_a_task_without_a_marker_is_never_waived(
        self, tmp_path, project, task_mode
    ):
        config = _cfg(tmp_path, execution_mode=project)
        task = _task(execution_mode=task_mode)
        assert config.resolve_waiver(task) is None

    def test_the_resolved_mode_of_a_waived_task_is_still_standard(self, tmp_path):
        """Маркер НЕ создаёт четвёртого режима: `EXECUTION_MODES` не
        трогается, и всё, что ветвится по режиму, видит `standard`."""
        config = _cfg(tmp_path, execution_mode="tdd")
        task = _task(execution_mode="standard", tdd_waiver=WAIVER)
        assert config.resolve_execution_mode(task) == "standard"


# --- Разбор маркера из tasks.md ----------------------------------------


class TestTheMarkerIsReadFromTheTasksFile:
    def test_the_marker_is_parsed_verbatim_beside_mode(self, tmp_path):
        """Отдельная строка рядом с `**Mode:**`, как `**Verifies:**`.

        Значение хранится ДОСЛОВНО и не интерпретируется парсером — ровно
        по тем же основаниям, что и режим: нормализация здесь спрятала бы
        ту самую опечатку, ради отказа на которой резолвер и существует.
        """
        tasks = tmp_path / "tasks.md"
        tasks.write_text(
            "## Tasks\n\n"
            "### TASK-008: characterisation\n"
            "P2 | TODO   Est: 0.5d\n\n"
            "Проза.\n"
            "**Mode:** standard\n"
            f"**TDD-waiver:** {WAIVER}\n\n"
            "**Checklist:**\n"
            "- [ ] пункт\n",
            encoding="utf-8",
        )
        parsed = {t.id: t for t in parse_tasks(tasks)}
        assert parsed["TASK-008"].execution_mode == "standard"
        assert parsed["TASK-008"].tdd_waiver == WAIVER

    def test_a_task_without_the_line_carries_none(self, tmp_path):
        tasks = tmp_path / "tasks.md"
        tasks.write_text(
            "## Tasks\n\n"
            "### TASK-001: обычная\n"
            "P2 | TODO   Est: 0.5d\n\n"
            "Проза.\n"
            "**Mode:** standard\n\n"
            "**Checklist:**\n"
            "- [ ] пункт\n",
            encoding="utf-8",
        )
        parsed = {t.id: t for t in parse_tasks(tasks)}
        assert parsed["TASK-001"].tdd_waiver is None


# --- Claims: ТРИ ТОЧКИ, три отдельных теста -----------------------------
#
# Каждая точка — своим тестом, не одним общим: «claims проверяются на одной
# точке» и «на всех трёх» — разные утверждения, и общий тест их не различит.

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _repo_cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _commit(root: Path, files: dict, message: str = "c") -> str:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _frozen(cfg, state, sha, *, task="TASK-002", selector="tests/test_frozen.py::t"):
    """Заморозка СОСЕДНЕЙ задачи — именно она и есть предмет.

    Проверять «свои» claim'ы бессмысленно: waived-задача RED не проходит и
    своих заморозок не заводит вовсе. Опасен ровно чужой claim — его
    `check_claims` судит независимо от того, чья задача принесла кандидата
    (`active_claims`: «whoever made it»).
    """
    record_claims(cfg, state, RedCheckpoint(
        task_id=task,
        namespace=resolve_namespace(cfg),
        commit_sha=sha,
        baseline_sha=sha,
        selector=selector,
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash="h",
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-09-11T00:00:00",
    ))


def _judge(cfg, state, sha, *, waived: bool, mode: str = "standard"):
    return evaluate_claims(GateContext(
        task_id="TASK-008",
        checkpoint_sha=sha,
        config=cfg,
        state=state,
        facts={"execution_mode": mode, "waiver_applied": waived},
    ))


class TestClaimsAreJudgedForAWaivedTask:
    def test_a_waived_task_touching_a_neighbours_frozen_file_is_refused(self, tmp_path):
        """Ядро #429: под обычным `standard` этот кандидат прошёл бы молча."""
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        sha = _commit(root, {"tests/test_frozen.py": "def t():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            broken = _commit(root, {"tests/test_frozen.py": "def t():\n    assert True\n"})
            verdict = _judge(cfg, state, broken, waived=True)
        assert verdict.status is GateStatus.UNSATISFIED

    def test_an_untouched_tree_satisfies_the_gate(self, tmp_path):
        """Вторая половина: гейт, отвергающий ВСЁ, на одном отказном тесте
        выглядел бы работающим."""
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        sha = _commit(root, {"tests/test_frozen.py": "def t():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            untouched = _commit(root, {"other.py": "x = 1\n"})
            verdict = _judge(cfg, state, untouched, waived=True)
        assert verdict.status is GateStatus.SATISFIED
        assert verdict.outcome is not PhaseOutcome.SKIPPED

    def test_ordinary_standard_still_skips(self, tmp_path):
        """Половина «ничего не сломано»: без маркера гейт скипает как раньше."""
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        sha = _commit(root, {"tests/test_frozen.py": "def t():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            broken = _commit(root, {"tests/test_frozen.py": "def t():\n    assert True\n"})
            verdict = _judge(cfg, state, broken, waived=False)
        assert verdict.status is GateStatus.SATISFIED
        assert verdict.outcome is PhaseOutcome.SKIPPED


class TestEachOfTheThreePointsJudgesAWaivedTask:
    """Каждая точка — СВОИМ тестом и через свою реальную функцию.

    Проверка исходником («в файле есть нужная строка») здесь была бы той же
    слабой формой, которую мы весь прогон вычищаем: она подтверждает, что
    текст написан, а не что путь исполняется. Точка 1 это и доказала —
    строка факта стояла в `_run_red_phase_gate`, куда waived-задача не
    заходит вовсе, и общий предикат выглядел исправным.
    """

    def _stand(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        sha = _commit(root, {"tests/test_frozen.py": "def t():\n    assert False\n"})
        return root, cfg, sha

    def _waived_task(self):
        return _task(execution_mode="standard", tdd_waiver=WAIVER)

    def test_point_1_before_the_paid_call(self, tmp_path):
        """Точка 1 у waived-задачи живёт отдельной функцией: путь RED-фазы,
        внутри которого она стоит для `tdd`, `standard` не исполняет."""
        from spec_runner.execution import _run_waived_claims_gate

        class _Reporter:
            current = "tests"

            def enter(self, stage):
                self.current = stage

        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            ensure_red_gate()
            _frozen(cfg, state, sha)
            _commit(root, {"tests/test_frozen.py": "def t():\n    assert True\n"})
            refusal = _run_waived_claims_gate(
                self._waived_task(), cfg, state, _Reporter()
            )
        assert refusal is not None and "claim" in refusal.lower()

    def test_point_2_before_the_terminal_transition(self, tmp_path):
        """Точка 2 — единственная, где facts строит ВЫЗЫВАЮЩАЯ сторона.

        Поэтому здесь они передаются ровно так, как их строит прод
        (`hooks.py`), и утверждается ПРИЧИНА отказа, а не его наличие.
        Прежняя редакция звала функцию без facts: гейты отвечали
        instrument-error'ом «no execution_mode» ещё до ветки про waiver, и
        `refusal is not None` проходил бы при любой поломке проводки —
        сними факт в `hooks.py`, верни гейту старый skip, отцепи
        регистрацию, всё равно не-None. Тест, зеленеющий по неверной
        причине, хуже отсутствующего: он выглядит покрытием.
        """
        from spec_runner.hooks import _run_pre_terminal_gates

        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            ensure_red_gate()
            _frozen(cfg, state, sha)
        broken = _commit(root, {"tests/test_frozen.py": "def t():\n    assert True\n"})
        refusal = _run_pre_terminal_gates(
            self._waived_task(), cfg, candidate_sha=broken,
            facts={"execution_mode": "standard", "waiver_applied": True},
        )
        assert refusal is not None
        assert "claim" in str(refusal).lower(), (
            "отказ обязан быть про нарушенный claim, а не про сломанный инструмент"
        )
        assert "instrument" not in str(refusal).lower()

    def test_point_3_before_paying_a_reviewer(self, tmp_path):
        from spec_runner.hooks import _claims_intact_before_review

        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            ensure_red_gate()
            _frozen(cfg, state, sha)
        broken = _commit(root, {"tests/test_frozen.py": "def t():\n    assert True\n"})
        refusal = _claims_intact_before_review(self._waived_task(), cfg, broken)
        assert refusal is not None
        assert "claim" in str(refusal).lower()
        assert "instrument" not in str(refusal).lower()

    def test_ordinary_standard_passes_all_three_untouched(self, tmp_path):
        """Половина «ничего не сломано», на том же самом нарушающем дереве.

        Без неё три теста выше зеленели бы и у реализации, проверяющей
        claims у ЛЮБОЙ standard-задачи, — то есть у правки, ломающей
        обычный режим.
        """
        from spec_runner.hooks import _claims_intact_before_review, _run_pre_terminal_gates

        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            ensure_red_gate()
            _frozen(cfg, state, sha)
        broken = _commit(root, {"tests/test_frozen.py": "def t():\n    assert True\n"})
        plain = _task(execution_mode="standard")
        # Точка 2 получает факты от вызывающей стороны (`post_done_hook`),
        # поэтому здесь они передаются так же, как их строит прод, — иначе
        # гейт отвечает instrument-error про отсутствующий режим, и тест
        # утверждал бы про свой вызов, а не про поведение.
        assert _run_pre_terminal_gates(
            plain, cfg, candidate_sha=broken,
            facts={"execution_mode": "standard", "waiver_applied": False},
        ) is None
        # Точка 3 строит факты сама — её видно целиком.
        assert _claims_intact_before_review(plain, cfg, broken) is None


# --- Frozen-files block: ПО ПОТРЕБИТЕЛЮ, не одним тестом ----------------
#
# Три платных промпта — три теста. «Блок доезжает» на одном потребителе и на
# всех — разные утверждения: ровно из-за невыполнимости компенсации на
# ревью-промпте была отозвана предыдущая редакция гарантии.


class TestTheFrozenFilesBlockReachesEveryPaidPrompt:
    def _stand(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        sha = _commit(root, {"tests/test_frozen.py": "def t():\n    assert False\n"})
        return root, cfg, sha

    def _rendered(self, cfg, state, task, escape=None):
        kwargs = {"state": state} if escape is None else {"state": state, "escape": escape}
        return append_frozen_files("BODY", cfg, task, **kwargs)

    def test_the_executor_prompt_carries_it(self, tmp_path):
        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            waived = self._rendered(cfg, state, _task(
                execution_mode="standard", tdd_waiver=WAIVER))
            plain = self._rendered(cfg, state, _task(execution_mode="standard"))
        assert "tests/test_frozen.py" in waived
        assert plain == "BODY", "обычный standard не тронут"

    def test_the_review_prompt_carries_it(self, tmp_path):
        """Эта половина и есть причина отзыва прежней гарантии: ревьюер
        `standard`-задачи перечня заморозок не получал, а компенсация
        «ревью проверит» была на него и записана."""
        from spec_runner.claims import ESCAPE_REVIEW

        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            waived = self._rendered(cfg, state, _task(
                execution_mode="standard", tdd_waiver=WAIVER), escape=ESCAPE_REVIEW)
            plain = self._rendered(
                cfg, state, _task(execution_mode="standard"), escape=ESCAPE_REVIEW)
        assert "tests/test_frozen.py" in waived
        assert ESCAPE_REVIEW in waived
        assert plain == "BODY"

    def test_the_lint_round_prompt_carries_it(self, tmp_path):
        """Четвёртый платный промпт — тот, который «не должен быть тем, кто
        забудет» (#214).

        Зовётся РЕАЛЬНЫЙ строитель этого промпта, а не `append_frozen_files`
        ещё раз: прежняя редакция дублировала проверку промпта исполнителя и
        потребителя 20d не касалась вовсе — тест не делал того, что обещает
        именем, и был бы зелёным, забудь строитель про блок.
        """
        from spec_runner.tdd import _lint_agent_round_prompt

        root, cfg, sha = self._stand(tmp_path)
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            # Строитель читает названный файл — он существует в стенде.
            body = _lint_agent_round_prompt(
                cfg, "tests/test_frozen.py::t", ["tests/test_frozen.py"], "findings"
            )
            waived = append_frozen_files(
                body, cfg, _task(execution_mode="standard", tdd_waiver=WAIVER), state=state
            )
            plain = append_frozen_files(
                body, cfg, _task(execution_mode="standard"), state=state
            )
        assert "tests/test_frozen.py" in waived
        assert plain == body, "обычный standard не тронут и здесь"


# --- Durable-событие ----------------------------------------------------


class TestApplyingAWaiverIsRecordedDurably:
    def test_the_row_names_what_was_removed_and_what_stayed(self, tmp_path):
        """Запись обязана отвечать на оба вопроса.

        «Что снято» без «что осталось» читалось бы как «снят TDD целиком» —
        а снят ровно baseline-RED. Отсутствие lifecycle сказано словами, а
        не оставлено выводиться из пустых строк: пустые строки — это ещё и
        то, как выглядит падение.
        """
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        with ExecutorState(cfg) as state:
            state.record_waiver_applied(
                task_id="TASK-008",
                namespace=resolve_namespace(cfg),
                waiver_class="characterisation",
                sanction="batch-approve-2026-09-09",
                baseline_sha="abc1234",
            )
            rows = state.applied_waivers(resolve_namespace(cfg))
        assert len(rows) == 1
        row = rows[0]
        assert row["task_id"] == "TASK-008"
        assert row["sanction"] == "batch-approve-2026-09-09"
        assert "baseline-RED" in row["removed"]
        assert "three points" in row["retained"]
        assert "frozen-files" in row["retained"]
        assert "no TDD lifecycle" in row["lifecycle"]

    def test_an_unattributed_application_is_refused(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        with ExecutorState(cfg) as state:
            with pytest.raises(ValueError):
                state.record_waiver_applied(
                    task_id="TASK-008",
                    namespace=resolve_namespace(cfg),
                    waiver_class="characterisation",
                    sanction="   ",
                    baseline_sha="abc1234",
                )

    def test_it_is_not_written_into_the_operator_waiver_table(self, tmp_path):
        """`phase_waivers` — про то, что ОПЕРАТОР отменил наблюдённый исход.

        Здесь ничего не наблюдалось и оператора в этот момент не было:
        харнесс применил заранее данную санкцию. Запись в ту таблицу
        зафиксировала бы человека, которого не было.
        """
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        with ExecutorState(cfg) as state:
            state.record_waiver_applied(
                task_id="TASK-008",
                namespace=resolve_namespace(cfg),
                waiver_class="characterisation",
                sanction="batch-approve-2026-09-09",
                baseline_sha="abc1234",
            )
            count = state._conn.execute("SELECT COUNT(*) FROM phase_waivers").fetchone()[0]
        assert count == 0


# --- Регистрация гейтов при проектном standard --------------------------


class TestAWaivedTaskRegistersTheGatesItself:
    def test_gates_exist_for_a_waived_task_in_a_standard_project(self, tmp_path):
        """Дыра, которой не было в наряде: обе существующие пер-задачные
        точки регистрации лежат на путях RED и verify-first, а waived-задача
        не идёт ни по одному. При проектном `standard` она осталась бы без
        единого гейта — не мягче, а fail-open.

        На нашем воркстриме (проект `tdd`) это не проявилось бы никогда.
        """
        from spec_runner.gates import REGISTRY, register_builtin_gates

        root = _repo(tmp_path)
        cfg = _repo_cfg(root, execution_mode="standard")
        REGISTRY.unregister("tdd.red", "tests")
        REGISTRY.unregister("tdd.claims", "tests")
        register_builtin_gates(cfg)
        assert not has_gates(), "проектный standard гейтов не регистрирует"

        ensure_red_gate()
        assert has_gates(), "waived-путь обязан зарегистрировать их сам"


# --- Чекпоинты: маркер в них не протекает -------------------------------


class TestTheMarkerDoesNotLeakIntoCheckpoints:
    def test_the_recorded_mode_stays_a_plain_mode(self, tmp_path):
        """№22 описи: переиспользование чекпоинта сверяет
        `cp.execution_mode` с резолвнутым. Маркер живёт отдельной строкой и
        в это значение не входит — утверждается тестом, а не подразумевается.
        """
        cfg = _cfg(tmp_path, execution_mode="tdd")
        waived = _task(execution_mode="standard", tdd_waiver=WAIVER)
        assert cfg.resolve_execution_mode(waived) == "standard"
        assert "waiver" not in cfg.resolve_execution_mode(waived)
        assert "·" not in cfg.resolve_execution_mode(waived)


# --- Правки по ревью #430 ----------------------------------------------


class TestAWaivedTaskDoesNotChangeTheNextOrdinaryOne:
    def test_the_registry_is_restored_after_a_waived_task(self, tmp_path):
        """Регистрация ограничена исполнением waived-задачи.

        `ensure_red_gate` пишет в ПРОЦЕССНЫЙ реестр, а
        `register_builtin_gates` зовётся раз на процесс. Оставь мы гейты
        привязанными — следующая ОБЫЧНАЯ `standard`-задача увидела бы
        `has_gates()` True, и у неё поменялась бы форма коммитов
        (кандидат + bookkeeping) и заработали бы пред-терминальные гейты.
        Это расщепление истории для задачи, которая ни во что не
        записывалась, — и утверждение «обычный standard не тронут» было бы
        ложным.
        """
        from spec_runner.gates import REGISTRY, has_gates

        REGISTRY.unregister("tdd.red", "tests")
        REGISTRY.unregister("tdd.claims", "tests")
        cfg = _cfg(tmp_path, execution_mode="standard")
        waived = _task(execution_mode="standard", tdd_waiver=WAIVER)

        seen = {}

        def _inner(task, config, state, harness_baseline=None):
            seen["gates_during"] = has_gates()
            return True

        from spec_runner import execution

        original = execution._execute_task
        execution._execute_task = _inner
        try:
            execution.execute_task(waived, cfg, None)
            during_waived = seen["gates_during"]
            execution.execute_task(_task(execution_mode="standard"), cfg, None)
            during_plain = seen["gates_during"]
        finally:
            execution._execute_task = original

        assert during_waived is True, "waived-задача обязана иметь гейты"
        assert during_plain is False, "следующая обычная — как если бы waived не было"
        assert has_gates() is False, "реестр восстановлен"

    def test_a_review_gate_alone_does_not_look_like_inherited_tdd_gates(
        self, tmp_path
    ):
        """`standard` + `review_policy: required` — легальная комбинация.

        Review-гейт зарегистрирован, значит `has_gates()` True, а
        `tdd.claims` отсутствует: предикат «что-нибудь зарегистрировано»
        принял бы чужой гейт за наши и не отцепил бы свои. Утечка на весь
        процесс — ровно та, ради которой обёртка и заведена.

        Реестр здесь НЕ очищается намеренно: очистка делает обе
        формулировки предиката неразличимыми, и прежний тест был зелёным
        именно поэтому.
        """
        from spec_runner.gates import REGISTRY, is_registered, register_builtin_gates

        cfg = _cfg(tmp_path, execution_mode="standard", review_policy="required")
        REGISTRY.unregister("tdd.red", "tests")
        REGISTRY.unregister("tdd.claims", "tests")
        register_builtin_gates(cfg)
        assert has_gates(), "review-гейт привязан — общий предикат уже True"
        assert not is_registered("tdd.claims", "tests"), "а наших гейтов нет"

        from spec_runner import execution

        original = execution._execute_task
        execution._execute_task = lambda *a, **k: True
        try:
            execution.execute_task(
                _task(execution_mode="standard", tdd_waiver=WAIVER), cfg, None
            )
        finally:
            execution._execute_task = original

        assert not is_registered("tdd.claims", "tests"), (
            "гейты waived-задачи утекли: чужой review-гейт принят за наследство"
        )

    def test_gates_already_in_force_are_left_alone(self, tmp_path):
        """Проект под `tdd` привязывает гейты всем; снять их здесь значило
        бы тот же баг, только в другую сторону."""
        from spec_runner.gates import has_gates

        ensure_red_gate()
        cfg = _cfg(tmp_path, execution_mode="tdd")
        from spec_runner import execution

        original = execution._execute_task
        execution._execute_task = lambda *a, **k: True
        try:
            execution.execute_task(
                _task(execution_mode="standard", tdd_waiver=WAIVER), cfg, None
            )
        finally:
            execution._execute_task = original
        assert has_gates() is True


class TestTheEventRecordsApplicationNotIntention:
    def test_repeated_attempts_write_one_row(self, tmp_path):
        """`run_with_retries` зовёт `execute_task` до `max_retries` раз.

        Та же санкция, применённая на второй попытке, — ТО ЖЕ применение,
        а не второе: N одинаковых строк заставили бы `tdd status`
        напечатать waiver N раз, и читатель, считающий строки, увидел бы
        повторение там, где было одно решение.
        """
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            for _ in range(3):
                state.record_waiver_applied(
                    task_id="TASK-008", namespace=ns,
                    waiver_class="characterisation",
                    sanction="batch-approve-2026-09-09", baseline_sha="abc",
                )
            rows = state.applied_waivers(ns)
        assert len(rows) == 1

    def test_a_different_sanction_is_a_different_fact(self, tmp_path):
        """Дедупликация по (задача, неймспейс, санкция), а не по задаче:
        другая санкция — другое решение, и ему место на записи."""
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as state:
            state.record_waiver_applied(
                task_id="TASK-008", namespace=ns, waiver_class="characterisation",
                sanction="batch-approve-2026-09-09", baseline_sha="abc")
            state.record_waiver_applied(
                task_id="TASK-008", namespace=ns, waiver_class="characterisation",
                sanction="spec-runner#429", baseline_sha="abc")
            rows = state.applied_waivers(ns)
        assert len(rows) == 2

    def test_an_unwritable_row_refuses_the_task_instead_of_crashing(self, tmp_path):
        """«Обязательно» достигается ОТКАЗОМ задачи, а не крахом прогона.

        Трейсбек ушёл бы через `run_with_retries` в цикл, который его не
        ловит: попытка не записана, задача `in_progress`, остальные не
        идут. Отказ останавливает одну задачу, крах — весь прогон.
        """
        from spec_runner.config import AppliedWaiver
        from spec_runner.execution import _record_waiver_applied

        root = _repo(tmp_path)
        cfg = _repo_cfg(root)

        class _Broken:
            def record_waiver_applied(self, **kwargs):
                raise RuntimeError("disk is gone")

        refusal = _record_waiver_applied(
            _Broken(), cfg, _task(),
            AppliedWaiver(node_class="characterisation", sanction="spec-runner#429"),
        )
        assert refusal is not None
        assert "could not be recorded" in str(refusal)


class TestAMissingFactIsAnInstrumentErrorNotAVerdict:
    def test_the_claims_gate_refuses_to_guess(self, tmp_path):
        """Отсутствующий ключ — наша ошибка, а не вердикт о коде.

        Тот же контракт, которому следует `execution_mode`. И это не
        гипотеза: внутри этой же правки точка 1 сначала стояла там, куда
        waived-задача не заходит, — с разрешающим дефолтом её отсутствие
        выглядело бы обычным скипом.
        """
        root = _repo(tmp_path)
        cfg = _repo_cfg(root)
        sha = _commit(root, {"tests/test_frozen.py": "def t():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            _frozen(cfg, state, sha)
            verdict = evaluate_claims(GateContext(
                task_id="TASK-008", checkpoint_sha=sha, config=cfg, state=state,
                facts={"execution_mode": "standard"},
            ))
        assert verdict.status is GateStatus.INSTRUMENT_ERROR
        assert "waiver" in verdict.detail


class TestThePreTerminalWiringItselfIsCovered:
    """Точка 2 строит facts у ВЫЗЫВАЮЩЕЙ стороны — значит покрывать надо её.

    Тест на самом гейте, которому facts передали руками, проводку не
    проверяет вовсе: сними строку в `hooks.py` — он останется зелёным,
    потому что факт пришёл из теста, а не из прода. Именно это и
    случилось с первой правкой этого майора: я утвердил ПРИЧИНУ отказа,
    но по-прежнему подавал факт сам.

    Поэтому здесь перехватывается `_run_pre_terminal_gates` и
    утверждается, ЧТО ему передал прод.
    """

    def test_the_caller_reports_the_waiver_to_the_gate(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        seen: dict = {}

        def _capture(task, config, candidate_sha=None, facts=None):
            seen["facts"] = facts
            return None

        monkeypatch.setattr(hooks, "_run_pre_terminal_gates", _capture)
        monkeypatch.setattr(hooks, "has_gates", lambda *a, **k: True)

        root = _repo(tmp_path)
        cfg = _repo_cfg(
            root, run_review=False, auto_commit=False,
            run_tests_on_done=False, run_lint_on_done=False,
        )
        waived = _task(execution_mode="standard", tdd_waiver=WAIVER)
        try:
            hooks.post_done_hook(waived, cfg, True)
        except Exception:
            # Хук делает много лишнего для этого теста; нам нужен ровно
            # словарь, который он собрал для точки 2, — и если до него не
            # дошли, `seen` пуст и assert ниже это назовёт.
            pass

        assert "facts" in seen, "точка 2 не была вызвана — проводку проверять не на чем"
        assert seen["facts"].get("waiver_applied") is True
        assert seen["facts"].get("execution_mode") == "standard"

    def test_an_ordinary_task_reports_false_not_silence(self, tmp_path, monkeypatch):
        """Обычной задаче факт тоже проставляется — молчание гейт считает
        instrument-error'ом, и «забыли сказать» сломало бы обычный путь."""
        from spec_runner import hooks

        seen: dict = {}
        monkeypatch.setattr(
            hooks, "_run_pre_terminal_gates",
            lambda task, config, candidate_sha=None, facts=None: seen.update(facts=facts),
        )
        monkeypatch.setattr(hooks, "has_gates", lambda *a, **k: True)

        root = _repo(tmp_path)
        cfg = _repo_cfg(
            root, run_review=False, auto_commit=False,
            run_tests_on_done=False, run_lint_on_done=False,
        )
        try:
            hooks.post_done_hook(_task(execution_mode="standard"), cfg, True)
        except Exception:
            pass

        assert "facts" in seen
        assert seen["facts"].get("waiver_applied") is False
