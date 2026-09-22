"""Проводка негативного контроля в `post_done_hook` (#428, FR-06/§4a).

Source: workstreams/executable-negative-control-under-standard-20260921/spec/20-design.md §4a, §6
Traces: FR-01, FR-04, FR-06
Acceptance: AC-08, AC-14

Находка ревью цепи (блокирующая): весь производственный путь — исполнение
контроля до платного ревью, переисполнение инструментальной неудачи, запись
свидетельства, перенос вердикта в гейт — не наблюдался ни одним тестом.
Тесты DT-04 писали строку в `negative_controls` сами, то есть AC-14 («после
прогона запись содержит…») доказывался тестом, который эту строку и создавал.
Мутация «убрать вызов `_record_negative_control`» проходила зелёной.

Поэтому здесь НИЧЕГО не подставляется, кроме платного ревьюера: стенд —
настоящий git-репозиторий с настоящим pytest, и строка свидетельства
читается из состояния ПОСЛЕ прогона, а не кладётся туда рукой.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from spec_runner import hooks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import NegativeControl, Task

WAIVER = "characterisation · sanction: batch-approve-2026-09-09"
SELECTOR = "tests/test_subject.py::test_property"
PATCH_PATH = "spec/negative-controls/TASK-008.patch"

SUBJECT = "def value():\n    return 1\n"
TEST_FILE = "from subject import value\n\n\ndef test_property():\n    assert value() == 1\n"

#: Различает: ломает свойство, строку объявления теста не трогает.
GOOD_PATCH = """--- a/subject.py
+++ b/subject.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""

#: Применяется, но свойство цело — тест остаётся зелёным, контроль не различил.
USELESS_PATCH = """--- a/subject.py
+++ b/subject.py
@@ -1,2 +1,3 @@
 def value():
+    # harmless comment
     return 1
"""


def _git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, *, patch: str = GOOD_PATCH) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "spec" / "negative-controls").mkdir(parents=True)
    (root / "subject.py").write_text(SUBJECT, encoding="utf-8")
    (root / "tests" / "test_subject.py").write_text(TEST_FILE, encoding="utf-8")
    (root / PATCH_PATH).write_text(patch, encoding="utf-8")
    (root / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n",
        encoding="utf-8",
    )
    (root / "spec" / "tasks.md").write_text(
        "## Tasks\n\n### TASK-008: characterisation\n"
        "P2 | 🔄 IN_PROGRESS   Est: 0.5d\n"
        f"**Mode:** standard\n**TDD-waiver:** {WAIVER}\n"
        f"**Negative-control:** {PATCH_PATH} :: {SELECTOR}\n\n"
        "**Checklist:**\n- [ ] пункт\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "candidate")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / ".executor-state.db",
        "logs_dir": root / "spec" / ".logs",
        "test_command": f"{sys.executable} -m pytest",
        "create_git_branch": False,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": True,
        "auto_commit": True,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task() -> Task:
    return Task(
        id="TASK-008",
        name="characterisation",
        priority="p2",
        status="in_progress",
        estimate="0.5d",
        execution_mode="standard",
        tdd_waiver=WAIVER,
        negative_control=NegativeControl(patch=PurePosixPath(PATCH_PATH), selector=SELECTOR),
    )


def _run(root: Path, cfg: ExecutorConfig, monkeypatch) -> tuple[tuple, list]:
    """Гнать настоящий `post_done_hook`; подставлен только платный ревьюер."""
    from spec_runner.state import ReviewVerdict

    reviewed: list = []

    def _review(*a, **k):
        reviewed.append(k or a)
        return (ReviewVerdict.PASSED, None, "ok")

    monkeypatch.setattr(hooks, "run_code_review", _review)
    (root / "widget.py").write_text("x = 1\n", encoding="utf-8")
    return hooks.post_done_hook(_task(), cfg, True), reviewed


def _rows(cfg: ExecutorConfig) -> list[dict]:
    from spec_runner.tdd import resolve_namespace

    with ExecutorState(cfg) as state:
        return state.negative_controls(resolve_namespace(cfg))


class TestTheEvidenceComesFromTheRun:
    """kind: integration — AC-14 в тех словах, в которых написан: «ПОСЛЕ
    прогона запись содержит…». Строку кладёт прогон, а не тест."""

    def test_a_satisfied_control_writes_its_row_and_lets_review_run(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)

        (ok, error, *_), reviewed = _run(root, cfg, monkeypatch)
        rows = _rows(cfg)

        assert rows, "прогон не записал свидетельство контроля"
        assert rows[0]["verdict"] == "satisfied", rows[0]
        assert rows[0]["task_id"] == "TASK-008"
        assert rows[0]["selector"] == SELECTOR
        assert rows[0]["clean_outcome"] and rows[0]["mutated_outcome"], rows[0]
        assert rows[0]["patch_blob_sha"], "blob патча не снят с кандидат-коммита"
        assert reviewed, "удовлетворённый контроль не должен мешать ревью"
        assert ok, error

    def test_an_unsatisfied_control_refuses_before_the_paid_review(self, tmp_path, monkeypatch):
        """§4a: отказ НА МЕСТЕ и до платного вызова. Мутант, который не
        различает, — факт о работе задачи, и ревьюеру нечего подтверждать."""
        root = _repo(tmp_path, patch=USELESS_PATCH)
        cfg = _cfg(root)

        (ok, error, *_), reviewed = _run(root, cfg, monkeypatch)
        rows = _rows(cfg)

        assert ok is False, "контроль не различил, а задача прошла"
        assert reviewed == [], "платное ревью вызвано после неудовлетворённого контроля"
        assert rows and rows[0]["verdict"] == "unsatisfied", rows
        assert "mutant" in (error or "").lower() or "control" in (error or "").lower(), error


class TestTheRecordSurvivesEveryVerdict:
    """kind: integration — FR-06: свидетельство пишется на ЛЮБОМ вердикте.
    Мутация «убрать вызов записи» обязана краснеть на обоих исходах."""

    @pytest.mark.parametrize(
        ("patch", "verdict"),
        [(GOOD_PATCH, "satisfied"), (USELESS_PATCH, "unsatisfied")],
        ids=["satisfied", "unsatisfied"],
    )
    def test_the_row_exists_after_the_run(self, tmp_path, monkeypatch, patch, verdict):
        root = _repo(tmp_path, patch=patch)
        cfg = _cfg(root)

        _run(root, cfg, monkeypatch)

        rows = _rows(cfg)
        assert len(rows) == 1, rows
        assert rows[0]["verdict"] == verdict, rows[0]


class TestAnInstrumentErrorIsReexecutedWithinBudget:
    """kind: integration — Р-2/Р-3: инструментальная неудача переисполняется
    ЗДЕСЬ, в пределах `gate_recovery_attempts`, а не в гейте (гейт перечитал
    бы тот же кэш). Число переисполнений наблюдаемо."""

    def _count_runs(self, monkeypatch, verdicts: list[str]) -> list:
        from spec_runner import negative_control as nc

        calls: list = []
        seq = list(verdicts)

        def _fake(config, *, sha, control):
            calls.append(sha)
            return nc.ControlResult(seq.pop(0) if seq else "satisfied", "измерено", None, None)

        monkeypatch.setattr(nc, "run_negative_control", _fake)
        return calls

    def test_it_stops_as_soon_as_the_verdict_is_determinate(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root, gate_recovery_attempts=2)
        calls = self._count_runs(monkeypatch, ["instrument_error", "satisfied"])

        _run(root, cfg, monkeypatch)

        assert len(calls) == 2, f"переисполнений {len(calls)}, ожидалось 2"

    def test_the_budget_is_a_ceiling_not_a_loop(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root, gate_recovery_attempts=2)
        calls = self._count_runs(monkeypatch, ["instrument_error"] * 9)

        _run(root, cfg, monkeypatch)

        assert len(calls) == 3, f"бюджет 2 даёт 3 попытки, было {len(calls)}"

    def test_a_determinate_verdict_is_never_repeated(self, tmp_path, monkeypatch):
        """Тот же вопрос тем же байтам: повтор детерминированного исхода —
        чистая трата, и бюджет Р-2 его не предусматривает."""
        root = _repo(tmp_path)
        cfg = _cfg(root, gate_recovery_attempts=3)
        calls = self._count_runs(monkeypatch, ["unsatisfied"])

        _run(root, cfg, monkeypatch)

        assert len(calls) == 1, f"детерминированный вердикт переисполнен {len(calls)} раз"


class TestClaimsAreNamedBeforeTheControlRuns:
    """kind: integration — находка ревью круга 5.

    Тот же порядок, что обоснован в точке 1: нарушенный claim — про уже
    нанесённый ущерб чужой замороженной эвиденции, неудовлетворённый
    контроль — про ещё не предъявленное доказательство. Обратный порядок
    стоил двух полных прогонов селектора в одноразовых worktree на
    кандидата, который не смержится ни при каком исходе, и оператор про
    сломанный byte-lock не узнавал вовсе.
    """

    def test_a_broken_claim_is_reported_and_the_control_never_runs(self, tmp_path, monkeypatch):
        from spec_runner import hooks
        from spec_runner import negative_control as nc
        from spec_runner.gates import REGISTRY, ensure_red_gate

        root = _repo(tmp_path, patch=USELESS_PATCH)  # контроль бы не различил
        cfg = _cfg(root)
        ran: list = []
        monkeypatch.setattr(
            nc,
            "run_negative_control",
            lambda *a, **k: ran.append(1) or nc.ControlResult("satisfied", "", None, None),
        )
        monkeypatch.setattr(
            hooks,
            "_claims_intact_before_review",
            lambda *a, **k: "TASK-009's frozen test was edited",
        )

        # Сайт claims спрашивает реестр: без регистрации ветка не достигается
        # вовсе, и тест измерял бы её отсутствие, а не порядок. Реестр
        # процессный, поэтому чистится на любом исходе.
        ensure_red_gate()
        try:
            (ok, error, *_), _reviewed = _run(root, cfg, monkeypatch)
        finally:
            REGISTRY.unregister("tdd.red", "tests")
            REGISTRY.unregister("tdd.claims", "tests")

        assert ok is False
        assert "frozen" in (error or ""), f"про сломанный claim не сказано: {error}"
        assert ran == [], "контроль исполнен на кандидате, который уже не мержится"


class TestAnUnrunnableControlIsStillRecorded:
    """kind: integration — BEH-16/AC-13 + FR-06: «контроль не мог быть
    исполнен» — такой же durable факт, как исход прогона. Без записи
    единственным следом остаётся `attempts.error`, то есть «проверка
    подтверждается тем, что задача не завершилась».
    """

    def test_an_unresolvable_candidate_blocks_and_leaves_evidence(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        # Кандидат не резолвится: ровно то, что `candidate_refusal` судит.
        monkeypatch.setattr(hooks, "_head_sha", lambda *a, **k: "")

        (ok, error, *_), reviewed = _run(root, cfg, monkeypatch)
        rows = _rows(cfg)

        assert ok is False, "waived-задача завершилась, хотя контроль не исполнялся"
        assert reviewed == [], "платное ревью вызвано без исполненного контроля"
        assert rows and rows[0]["verdict"] == "unsatisfied", rows
        assert rows[0]["clean_outcome"] == "" and rows[0]["mutated_outcome"] == "", rows[0]
        # `git rev-parse ":path"` ответил бы blob'ом из ИНДЕКСА — хэшем
        # содержимого, которого никакой коммит не фиксировал, в поле,
        # читаемом как «патч того кандидата». Отсутствие пишется отсутствием.
        assert rows[0]["patch_blob_sha"] == "", rows[0]
        assert rows[0]["commit_sha"] == "", rows[0]


class TestOnlyASatisfiedControlReachesTheReviewer:
    """kind: integration — находка ревью круга 8.

    Design §4a: «до гейта доезжает только удовлетворённый (и SHA, на котором
    получен)». Отказ на месте стоял только на `unsatisfied`, поэтому
    исчерпавший бюджет `instrument_error` проваливался дальше: ревьюер
    оплачивался, вердикт уносился в facts, гейт всё равно блокировал —
    платный вызов покупал ничего. Прецедент, на который ссылается сам
    комментарий сайта, `_claims_intact_before_review`, останавливается и на
    инструментальной неудаче тоже.
    """

    def test_an_exhausted_instrument_error_stops_before_the_paid_call(self, tmp_path, monkeypatch):
        from spec_runner import negative_control as nc
        from spec_runner.state import ErrorCode

        root = _repo(tmp_path)
        cfg = _cfg(root, gate_recovery_attempts=1)
        tries: list = []
        monkeypatch.setattr(
            nc,
            "run_negative_control",
            lambda *a, **k: tries.append(1)
            or nc.ControlResult("instrument_error", "the toolchain vanished", None, None),
        )

        (ok, error, *_), reviewed = _run(root, cfg, monkeypatch)

        assert reviewed == [], "ревьюер оплачен при нерешённом контроле"
        assert ok is False, error
        assert len(tries) == 2, f"бюджет 1 даёт 2 попытки, было {len(tries)}"
        # exit 2 — «о работе ничего не известно», а не «работа не доставлена».
        # Вид живёт НА отказе, а не в его словах: классификация по тексту —
        # ровно то, что #230 и убрал.
        assert getattr(error, "error_code", None) == ErrorCode.INFRASTRUCTURE, (
            f"отказ типизирован как {getattr(error, 'kind', None)}: {error}"
        )

    def test_a_satisfied_control_still_reaches_the_reviewer(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)

        (ok, error, *_), reviewed = _run(root, cfg, monkeypatch)

        assert reviewed, "удовлетворённый контроль не должен мешать ревью"
        assert ok, error


class TestWithoutACandidateOfItsOwnTheControlStaysSilent:
    """kind: integration — поймано полным сбором после круга 8.

    При `auto_commit: false` коммита, который сделал бы ЭТОТ прогон, не
    существует: реплей против HEAD судил бы чужую работу и возвращал бы
    вердикт о ней. Это член класса структурной невозможности, и отказ по
    нему стоит до первого платного вызова (точка 1) — здесь остаётся
    молчать. Заодно восстановлен порядок: claims называются раньше
    контроля и когда ревью выключено.
    """

    def test_no_replay_happens_without_auto_commit(self, tmp_path, monkeypatch):
        from spec_runner import negative_control as nc

        root = _repo(tmp_path)
        cfg = _cfg(root, auto_commit=False, run_review=False)
        ran: list = []
        monkeypatch.setattr(nc, "run_negative_control", lambda *a, **k: ran.append(1))

        _run(root, cfg, monkeypatch)

        assert ran == [], "контроль судил дерево, которое эта задача не коммитила"
        assert _rows(cfg) == [], "записано свидетельство о чужом коммите"

    def test_claims_are_named_before_the_control_when_review_is_off(self, tmp_path, monkeypatch):
        from spec_runner import hooks
        from spec_runner import negative_control as nc
        from spec_runner.gates import REGISTRY, ensure_red_gate

        root = _repo(tmp_path)
        cfg = _cfg(root, run_review=False)
        ran: list = []
        monkeypatch.setattr(
            nc,
            "run_negative_control",
            lambda *a, **k: ran.append(1) or nc.ControlResult("satisfied", "", None, None),
        )
        monkeypatch.setattr(
            hooks, "_claims_intact_before_review", lambda *a, **k: "a neighbour's claim broke"
        )

        ensure_red_gate()
        try:
            (ok, error, *_), _reviewed = _run(root, cfg, monkeypatch)
        finally:
            REGISTRY.unregister("tdd.red", "tests")
            REGISTRY.unregister("tdd.claims", "tests")

        assert ok is False
        assert "claim" in (error or "").lower(), error
        assert ran == [], "контроль исполнен раньше claims при выключенном ревью"


class TestTheGateIsToldWhatTheControlFound:
    """kind: integration — находка ревью круга 10.

    Гейт ЧИТАЕТ вердикт, а не исполняет контроль, — значит проверять надо
    вызывающую сторону: тест на самом гейте, которому facts передали рукой,
    проводку не проверяет вовсе. Тот же приём, что у точки 2 в
    `test_waived_standard.py`: перехват `_run_pre_terminal_gates` и
    утверждение о том, ЧТО ему передал прод.

    Привязку вердикта к коммиту держит ПЕРЕИСПОЛНЕНИЕ при смене кандидата, а
    не отдельное поле в facts: изобретать ключ ради теста значило бы
    проверять то, чего контракт не обещает.
    """

    def _capture_facts(self, root, cfg, monkeypatch) -> dict:
        from spec_runner import hooks

        seen: dict = {}
        monkeypatch.setattr(
            hooks,
            "_run_pre_terminal_gates",
            lambda task, config, candidate_sha=None, facts=None: seen.update(facts or {}),
        )
        monkeypatch.setattr(hooks, "has_gates", lambda *a, **k: True)
        _run(root, cfg, monkeypatch)
        return seen

    def test_a_satisfied_verdict_reaches_the_gate(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)

        facts = self._capture_facts(root, cfg, monkeypatch)

        assert facts.get("negative_control") == "satisfied", facts
        assert facts.get("waiver_applied") is True, facts
        assert facts.get("negative_control_detail"), facts

    def test_an_ordinary_task_reports_no_control_verdict(self, tmp_path, monkeypatch):
        """Дормантность: проект без waiver'ов не платит за механику ничем —
        ключа в facts нет вовсе, а не `None`, который гейт прочёл бы как
        молчание сайта."""
        from spec_runner.task import Task

        root = _repo(tmp_path)
        cfg = _cfg(root)
        plain = Task(
            id="TASK-007",
            name="ordinary",
            priority="p2",
            status="in_progress",
            estimate="0.5d",
            execution_mode="standard",
        )

        from spec_runner import hooks
        from spec_runner.state import ReviewVerdict

        seen: dict = {}
        monkeypatch.setattr(
            hooks,
            "_run_pre_terminal_gates",
            lambda task, config, candidate_sha=None, facts=None: seen.update(facts or {}),
        )
        monkeypatch.setattr(hooks, "has_gates", lambda *a, **k: True)
        monkeypatch.setattr(
            hooks, "run_code_review", lambda *a, **k: (ReviewVerdict.PASSED, None, "ok")
        )
        hooks.post_done_hook(plain, cfg, True)

        assert "negative_control" not in seen, seen

    def test_a_candidate_changed_by_review_is_judged_afresh(self, tmp_path, monkeypatch):
        """Ревью правит дерево — вердикт, снятый на прежнем коммите, про
        нового кандидата ничего не говорит, и гейт одобрил бы дерево, на
        котором контроль не исполнялся."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        shas: list = []
        monkeypatch.setattr(
            hooks,
            "_run_negative_control_before_review",
            lambda task, config, sha: (shas.append(sha) or ("satisfied", "ok", sha or "old")),
        )

        hooks._negative_control_facts(_task(), cfg, "newsha", "satisfied", "ok", "oldsha", True)

        assert shas == ["newsha"], f"вердикт не переснят на новом кандидате: {shas}"

    def test_an_unchanged_candidate_is_not_re_judged(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        shas: list = []
        monkeypatch.setattr(
            hooks,
            "_run_negative_control_before_review",
            lambda task, config, sha: (shas.append(sha) or ("satisfied", "ok", sha or "")),
        )

        hooks._negative_control_facts(_task(), cfg, "samesha", "satisfied", "ok", "samesha", True)

        assert shas == [], "контроль переисполнен без смены кандидата"


class TestTheControlNamesItsOwnStage:
    """kind: integration — находка ревью круга 13.

    Для waived-задачи последняя объявленная стадия перед контролем —
    `commit`, поэтому `error_stage` неудавшегося контроля читался как
    «сломалось на коммите». Контроль при этом — самый дорогой
    детерминированный шаг хука: до двух полных реплеев, до
    `gate_recovery_attempts + 1` раз. Этот же файл уже чинил такую
    приписку однажды (#367 BEH-30): пре-терминальный гейт объявляет
    `tests` ровно затем, чтобы отказ не читался как чужая стадия.
    """

    def test_the_stage_reported_is_not_the_commit(self, tmp_path, monkeypatch):
        from spec_runner import hooks
        from spec_runner.stages import StageReporter

        root = _repo(tmp_path, patch=USELESS_PATCH)  # контроль не различит
        cfg = _cfg(root)
        reporter = StageReporter("TASK-008", lambda *a, **k: None)
        from spec_runner.state import ReviewVerdict

        monkeypatch.setattr(
            hooks, "run_code_review", lambda *a, **k: (ReviewVerdict.PASSED, None, "ok")
        )
        (root / "widget.py").write_text("x = 1\n", encoding="utf-8")

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True, reporter=reporter)

        assert ok is False, error
        assert reporter.current != "commit", f"отказ контроля приписан стадии {reporter.current!r}"
        assert reporter.current == "tests", reporter.current
