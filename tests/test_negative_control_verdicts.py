"""BEH-09…BEH-13 (DT-03) — классификация исходов контроля.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Source: .../20-design.md §3
Traces: FR-03, FR-04, FR-05

Две таблицы дизайна проверяются здесь строка за строкой. Дисциплина §3 —
**два измерения, а не два определения «красного»**: `PROVEN` там, где он
работает (падения), `execution_proven` только там, где `PROVEN` структурно
недостижим (проходы). Красный остаётся `TESTS_FAILED` + `PROVEN` (FR-03,
AP-12.1).

BEH-13 гоняется на ОБОИХ измеренных раннерах: классификация по стадии
давала там разные ответы, и pytest этот дефект не ловит.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.task import NegativeControl

SELECTOR = "tests/test_subject.py::test_property"
PATCH_PATH = PurePosixPath("spec/negative-controls/TASK-008.patch")

SUBJECT = "def value():\n    return 1\n"
TEST_FILE = "from subject import value\n\n\ndef test_property():\n    assert value() == 1\n"
OTHER_TEST = "def test_other():\n    assert True\n"

#: Благословлённая форма: ломает СВОЙСТВО, строку объявления не трогает.
GOOD_PATCH = """--- a/subject.py
+++ b/subject.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""

#: Не различает: патч ПРИМЕНЯЕТСЯ, но свойство цело и тест остаётся
#: зелёным. Счётчики ханка обязаны сходиться (`-1,2 +1,3`): первая
#: редакция этой фикстуры была повреждённым патчем, не применялась вовсе,
#: и тест про «мутант не различил» измерял отказ применения.
USELESS_PATCH = """--- a/subject.py
+++ b/subject.py
@@ -1,2 +1,3 @@
 def value():
+    # harmless comment
     return 1
"""

#: Ломает СБОРКУ файла, а не свойство.
BROKEN_PATCH = """--- a/subject.py
+++ b/subject.py
@@ -1,2 +1,2 @@
-def value():
-    return 1
+def value(:
+    return 1
"""


def _git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, *, patch: str = GOOD_PATCH, subject: str = SUBJECT) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "spec" / "negative-controls").mkdir(parents=True)
    (root / "subject.py").write_text(subject, encoding="utf-8")
    (root / "tests" / "test_subject.py").write_text(TEST_FILE, encoding="utf-8")
    (root / "tests" / "test_other.py").write_text(OTHER_TEST, encoding="utf-8")
    (root / "spec" / "negative-controls" / "TASK-008.patch").write_text(patch, encoding="utf-8")
    (root / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n",
        encoding="utf-8",
    )
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "candidate")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    return root, head


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / "state.db",
        "logs_dir": root / "spec" / "logs",
        "test_command": f"{sys.executable} -m pytest",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _control(selector: str = SELECTOR) -> NegativeControl:
    return NegativeControl(patch=PATCH_PATH, selector=selector)


def _run(root: Path, head: str, control: NegativeControl | None = None, **cfg_overrides):
    from spec_runner.negative_control import run_negative_control

    return run_negative_control(
        _cfg(root, **cfg_overrides), sha=head, control=control or _control()
    )


class TestBEH09RedOnlyForTheExpectedReason:
    """kind: integration — BEH-09: удовлетворён только тот контроль, где
    патч роняет ОБЪЯВЛЕННЫЙ тест."""

    def test_a_mutant_that_drops_the_declared_test_satisfies(self, tmp_path):
        root, head = _repo(tmp_path)

        result = _run(root, head)

        assert result.verdict == "satisfied", result.detail

    def test_a_mutant_that_changes_nothing_is_unsatisfied(self, tmp_path):
        root, head = _repo(tmp_path, patch=USELESS_PATCH)

        result = _run(root, head)

        assert result.verdict == "unsatisfied", result.detail
        assert "не различ" in result.detail or "did not discriminate" in result.detail

    def test_a_mutant_that_breaks_the_build_is_unsatisfied_not_satisfied(self, tmp_path):
        """Сломанная сборка — не красный: тест не исполнялся вовсе."""
        root, head = _repo(tmp_path, patch=BROKEN_PATCH)

        result = _run(root, head)

        assert result.verdict == "unsatisfied", result.detail

    def test_the_evidence_names_how_each_half_ended(self, tmp_path):
        root, head = _repo(tmp_path, patch=USELESS_PATCH)

        result = _run(root, head)

        assert result.clean is not None and result.clean.outcome is not None
        assert result.mutated is not None and result.mutated.outcome is not None


class TestBEH10CleanHalfRedIsSubstantive:
    """kind: integration — BEH-10: тест, красный БЕЗ мутанта, — отказ по
    существу, а не поломка стенда; контроль не переисполняется (Р-3)."""

    def test_a_test_red_without_the_mutant_is_unsatisfied(self, tmp_path):
        root, head = _repo(tmp_path, subject="def value():\n    return 99\n")

        result = _run(root, head)

        assert result.verdict == "unsatisfied", result.detail
        assert result.mutated is None, "мутант гонялся на заведомо негодном стенде"

    def test_the_message_speaks_of_the_task_test_not_the_harness(self, tmp_path):
        root, head = _repo(tmp_path, subject="def value():\n    return 99\n")

        result = _run(root, head)

        assert "harness" not in result.detail.lower()
        assert "test" in result.detail.lower()


class TestBEH11PatchDefectsAreToldApart:
    """kind: integration — BEH-11 и AP-12.4: отсутствующий, неприменимый и
    нечитаемый патч дают РАЗНЫЕ вердикты."""

    def test_a_patch_absent_from_the_commit_is_unsatisfied(self, tmp_path):
        root, head = _repo(tmp_path)
        control = NegativeControl(
            patch=PurePosixPath("spec/negative-controls/absent.patch"), selector=SELECTOR
        )

        result = _run(root, head, control)

        assert result.verdict == "unsatisfied", result.detail
        assert "deliver" in result.detail.lower() or "не доставлен" in result.detail

    def test_an_inapplicable_patch_is_unsatisfied_without_retries(self, tmp_path):
        """AP-12.4 заменяет первоначальное AP-02: применимость
        детерминирована — патч и цель один коммит, — поэтому это факт о
        работе задачи, а не о стенде."""
        root, head = _repo(tmp_path, patch="this is not a patch at all\n")

        result = _run(root, head)

        assert result.verdict == "unsatisfied", result.detail
        assert result.retriable is False


class TestBEH12TheStandIsNotBlamedOnThePatch:
    """kind: integration — BEH-12: вина стенда остаётся instrument error
    даже после зелёной чистой половины."""

    def test_an_unavailable_runner_is_instrument_not_a_verdict(self, tmp_path, monkeypatch):
        """Тулчейн исчезает МЕЖДУ половинами: чистая прошла, мутированная
        упирается в сломанный стенд. Приписать это патчу значило бы сказать
        автору «твой мутант не различил», когда сломалась машина."""
        from spec_runner import negative_control as nc
        from spec_runner.tdd import ReplayAttempt

        root, head = _repo(tmp_path)
        real = nc._replay_for_control

        def fake(config, *, sha, control, mutate, order):
            if mutate is None:
                return real(config, sha=sha, control=control, mutate=mutate, order=order)
            return ReplayAttempt(
                stage="preflight",
                detail="elixir is not on PATH",
                environment_id="x",
                refusal_code="runner_toolchain_missing",
                mutated=True,
                order=order,
            )

        monkeypatch.setattr(nc, "_replay_for_control", fake)
        result = _run(root, head)

        assert result.verdict == "instrument_error", result.detail
        assert result.retriable is True

    def test_a_timeout_is_instrument_not_a_verdict(self, tmp_path, monkeypatch):
        from spec_runner import negative_control as nc
        from spec_runner.tdd import ReplayAttempt

        root, head = _repo(tmp_path)
        real = nc._replay_for_control

        def fake(config, *, sha, control, mutate, order):
            if mutate is None:
                return real(config, sha=sha, control=control, mutate=mutate, order=order)
            return ReplayAttempt(
                stage="timeout",
                detail="replay failed: timeout",
                environment_id="x",
                mutated=True,
                order=order,
            )

        monkeypatch.setattr(nc, "_replay_for_control", fake)
        result = _run(root, head)

        assert result.verdict == "instrument_error", result.detail
        assert result.retriable is True


_EXUNIT_MISSING = shutil.which("mix") is None or shutil.which("elixir") is None

#: Выставляется обязательным ExUnit-job'ом. С ним отсутствие тулчейна —
#: ОШИБКА СБОРА, а не skip: AC-10 объявлен обязательным, и «зелёный, потому
#: что не запускалось» — ровно то, против чего заведён этот приём у соседнего
#: контрактного файла (`tests/test_exunit_adapter.py`). Гейт живёт рядом с
#: тестами, а не в workflow, считающем, сколько их прошло.
_EXUNIT_REQUIRED = os.environ.get("SPEC_RUNNER_REQUIRE_EXUNIT") == "1"

if _EXUNIT_MISSING and _EXUNIT_REQUIRED:
    raise RuntimeError(
        "SPEC_RUNNER_REQUIRE_EXUNIT=1, но `mix`/`elixir` не на PATH — BEH-13 "
        "сравнивает вердикты ДВУХ измеренных раннеров, и пропущенный ExUnit "
        "оставляет ветки классификации без единой исполняемой проверки"
    )

MIX_EXS = """defmodule Probe.MixProject do
  use Mix.Project
  def project, do: [app: :probe, version: "0.1.0", elixir: "~> 1.0"]
end
"""

EX_TEST = """defmodule SubjectTest do
  use ExUnit.Case
  test "property" do
    assert Subject.value() == 1
  end
end
"""

EX_SUBJECT = """defmodule Subject do
  def value, do: 1
end
"""

#: Ломает тест-файл так, что объявленный тест исполнить нельзя. На pytest
#: это приходит прогоном, на ExUnit — preflight'ом; вердикт обязан быть
#: один (BEH-13).
EX_BROKEN_PATCH = """--- a/test/subject_test.exs
+++ b/test/subject_test.exs
@@ -1,6 +1,6 @@
 defmodule SubjectTest do
   use ExUnit.Case
-  test "property" do
+  test "property" do(((
     assert Subject.value() == 1
   end
 end
"""


@pytest.mark.slow
@pytest.mark.skipif(_EXUNIT_MISSING, reason="BEH-13 needs a real Elixir toolchain")
class TestBEH13SameVerdictOnBothMeasuredRunners:
    """kind: acceptance — BEH-13: патч-обусловленный отказ даёт «не
    удовлетворён» на ОБОИХ раннерах, хотя механизм отказа у них разный.

    Классификация по стадии давала здесь неверный ответ: на ExUnit такой
    патч ловится preflight'ом, то есть формально «прогон не состоялся», и
    по списку источников ушёл бы в instrument error с ретраями.
    """

    def test_selector_identity_is_filled_where_the_selector_is_a_line(self, tmp_path):
        """Q-G мёртв, если поле не заполняется.

        Первая редакция объявила `selector_identity`, сравнивала его в
        таблице и НЕ писала — правило существовало только на бумаге, а
        дыра «satisfied по чужому тесту» оставалась открытой.
        """
        from spec_runner.negative_control import replay_both_halves

        ex_root = tmp_path / "ex"
        (ex_root / "lib").mkdir(parents=True)
        (ex_root / "test").mkdir(parents=True)
        (ex_root / "spec" / "negative-controls").mkdir(parents=True)
        (ex_root / "mix.exs").write_text(MIX_EXS, encoding="utf-8")
        (ex_root / "lib" / "subject.ex").write_text(EX_SUBJECT, encoding="utf-8")
        (ex_root / "test" / "subject_test.exs").write_text(EX_TEST, encoding="utf-8")
        (ex_root / "test" / "test_helper.exs").write_text("ExUnit.start()\n", encoding="utf-8")
        (ex_root / "spec" / "negative-controls" / "TASK-008.patch").write_text(
            EX_BROKEN_PATCH, encoding="utf-8"
        )
        _git(ex_root, "init")
        _git(ex_root, "config", "user.email", "t@t")
        _git(ex_root, "config", "user.name", "t")
        _git(ex_root, "add", "-A")
        _git(ex_root, "commit", "-m", "candidate")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ex_root, capture_output=True, text=True, check=True
        ).stdout.strip()

        clean, _ = replay_both_halves(
            _cfg(ex_root, test_command="mix test", tdd_runner="exunit"),
            sha=head,
            control=NegativeControl(patch=PATCH_PATH, selector="test/subject_test.exs:3"),
        )

        assert clean.selector_identity is not None, "идентичность объявления не прочитана"
        assert "test" in clean.selector_identity, clean.selector_identity

    def test_pytest_and_exunit_agree(self, tmp_path):
        py_root, py_head = _repo(tmp_path / "py", patch=BROKEN_PATCH)
        py_result = _run(py_root, py_head)

        ex_root = tmp_path / "ex"
        (ex_root / "lib").mkdir(parents=True)
        (ex_root / "test").mkdir(parents=True)
        (ex_root / "spec" / "negative-controls").mkdir(parents=True)
        (ex_root / "mix.exs").write_text(MIX_EXS, encoding="utf-8")
        (ex_root / "lib" / "subject.ex").write_text(EX_SUBJECT, encoding="utf-8")
        (ex_root / "test" / "subject_test.exs").write_text(EX_TEST, encoding="utf-8")
        (ex_root / "test" / "test_helper.exs").write_text("ExUnit.start()\n", encoding="utf-8")
        (ex_root / "spec" / "negative-controls" / "TASK-008.patch").write_text(
            EX_BROKEN_PATCH, encoding="utf-8"
        )
        _git(ex_root, "init")
        _git(ex_root, "config", "user.email", "t@t")
        _git(ex_root, "config", "user.name", "t")
        _git(ex_root, "add", "-A")
        _git(ex_root, "commit", "-m", "candidate")
        ex_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ex_root, capture_output=True, text=True, check=True
        ).stdout.strip()

        from spec_runner.negative_control import run_negative_control

        ex_result = run_negative_control(
            # `tdd_runner` объявлен явно: инференс по `mix test` не
            # предусмотрен, и без объявления реплей отказался бы до
            # исполнения — это верный отказ, но не предмет BEH-13.
            _cfg(ex_root, test_command="mix test", tdd_runner="exunit"),
            sha=ex_head,
            control=NegativeControl(patch=PATCH_PATH, selector="test/subject_test.exs:3"),
        )

        assert py_result.verdict == "unsatisfied", py_result.detail
        assert ex_result.verdict == "unsatisfied", ex_result.detail
        assert py_result.retriable is False and ex_result.retriable is False


class TestTheVerdictReachesTheGate:
    """kind: contract — проводка §4a/§5: вердикт доезжает до гейта, а гейт
    молчит там, где судить нечего.

    Тесты живут в файле DT-03, потому что предмет тот же — вердикт: гейт
    его только читает, и «вердикт вынесен» без «вердикт дошёл» — половина
    работы, которую мутация проходит зелёной (`supports_file_targets`,
    #448).
    """

    def _ctx(self, facts: dict):
        from spec_runner.config import ExecutorConfig
        from spec_runner.gates import GateContext

        return GateContext(
            task_id="TASK-008",
            checkpoint_sha="abc1234",
            config=ExecutorConfig(
                project_root=Path("/tmp"), state_file=Path("/tmp/s.db"), logs_dir=Path("/tmp/l")
            ),
            state=None,
            facts=facts,
        )

    def test_the_gate_is_silent_before_the_implementation_call(self):
        """Точка 1 зовёт `evaluate_gates("tests", …)` — ВСЕ гейты фазы.
        Гейт, отказывающий там, отказал бы каждой waived-задаче на каждом
        прогоне, до первого платного вызова."""
        from spec_runner.gates import GateStatus, _negative_control_gate

        result = _negative_control_gate(
            self._ctx({"waiver_applied": True, "pre_implementation": True})
        )

        assert result.status is GateStatus.SATISFIED, result.detail

    def test_a_missing_key_is_judged_not_skipped(self):
        """Fail-open здесь нет: ОТСУТСТВИЕ ключа означает «судить».

        То же правило, по которому `evaluate_claims` требует
        `waiver_applied`: молчание сайта не отмывается в пропуск.
        """
        from spec_runner.gates import GateStatus, _negative_control_gate

        result = _negative_control_gate(self._ctx({"waiver_applied": True}))

        assert result.status is not GateStatus.SATISFIED, result.detail

    def test_a_task_without_a_waiver_is_skipped(self):
        from spec_runner.gates import GateStatus, _negative_control_gate

        result = _negative_control_gate(self._ctx({"waiver_applied": False}))

        assert result.status is GateStatus.SATISFIED

    def test_the_gate_reads_the_verdict_it_was_given(self):
        from spec_runner.gates import GateStatus, _negative_control_gate

        satisfied = _negative_control_gate(
            self._ctx({"waiver_applied": True, "negative_control": "satisfied"})
        )
        unsatisfied = _negative_control_gate(
            self._ctx({"waiver_applied": True, "negative_control": "unsatisfied"})
        )
        broken = _negative_control_gate(
            self._ctx({"waiver_applied": True, "negative_control": "instrument_error"})
        )

        assert satisfied.status is GateStatus.SATISFIED
        assert unsatisfied.status is GateStatus.UNSATISFIED
        assert broken.status is GateStatus.INSTRUMENT_ERROR

    def test_the_gate_is_registered_per_task_with_its_own_borrow(self):
        """Свой признак заимствования, а не чужой.

        `borrowed` сегодня считается по `tdd.claims`. Переиспользовать его
        нельзя: проект, где claims уже зарегистрированы, дал бы
        `borrowed = False`, и новый гейт остался бы в реестре НА ВЕСЬ
        ПРОЦЕСС — ровно та утечка, ради предотвращения которой обёртка
        `execute_task` и написана.
        """
        from spec_runner.gates import REGISTRY, ensure_negative_control_gate, is_registered

        assert not is_registered("tdd.negative_control", "tests")
        try:
            ensure_negative_control_gate()
            assert is_registered("tdd.negative_control", "tests")
        finally:
            REGISTRY.unregister("tdd.negative_control", "tests")
        assert not is_registered("tdd.negative_control", "tests")


class TestTheGateDoesNotLeakAcrossTasks:
    """kind: integration — потасковая регистрация через НАСТОЯЩИЙ
    `execute_task`, а не через реестр напрямую.

    Прямая регистрация не видит дефекта: признак заимствования вычисляется
    в обёртке, и утечка возникает только при её собственном проходе.
    Мутация «переиспользовать чужой `borrowed`» без этого теста идёт
    зелёной — тот же класс, что `supports_file_targets` (#448).
    """

    def test_the_control_gate_is_detached_even_when_claims_were_already_registered(
        self, tmp_path, monkeypatch
    ):
        from spec_runner import execution
        from spec_runner.gates import REGISTRY, ensure_red_gate, is_registered
        from spec_runner.state import ExecutorState
        from spec_runner.task import Task

        root, head = _repo(tmp_path)
        # `tasks.md` КОММИТИТСЯ, а не просто пишется: `pre_start_hook`
        # заводит ветку и делает `git clean -fd`, то есть неотслеживаемый
        # файл был бы стёрт до того, как `execute_task` его прочитает.
        (root / "spec" / "tasks.md").write_text(
            "## Tasks\n\n### TASK-008: c\nP2 | TODO   Est: 0.5d\n"
            "**Mode:** standard\n"
            "**TDD-waiver:** characterisation · sanction: batch-approve-2026-09-09\n\n"
            "**Checklist:**\n- [ ] пункт\n",
            encoding="utf-8",
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "tasks")
        cfg = _cfg(root)
        monkeypatch.setattr(execution, "build_cli_invocation", lambda **k: None)

        # `tdd.claims` УЖЕ в реестре — значит чужой `borrowed` ложен, и
        # гейт контроля, снимаемый по нему, остался бы навсегда.
        ensure_red_gate()
        try:
            task = Task(
                id="TASK-008",
                name="c",
                priority="p2",
                status="todo",
                estimate="0.5d",
                execution_mode="standard",
                tdd_waiver="characterisation · sanction: batch-approve-2026-09-09",
            )
            with ExecutorState(cfg) as state:
                execution.execute_task(task, cfg, state)
            assert not is_registered("tdd.negative_control", "tests"), (
                "гейт контроля остался в реестре на весь процесс"
            )
        finally:
            REGISTRY.unregister("tdd.red", "tests")
            REGISTRY.unregister("tdd.claims", "tests")
            REGISTRY.unregister("tdd.negative_control", "tests")


class TestTheRecheckParsesWithoutRunning:
    """kind: contract — находка ревью цепи DT-01…DT-04.

    `unparseable_test_file` — единственный код, который нельзя отнести
    заранее, и сомнение снимается переспросом разбора на чистом дереве.
    Дизайн §3 требует для него режим `preflight_only`, а бюджет Р-2 —
    «+1 разбор, 0 прогонов». Вызов флага не передавал: `preflight_only`
    был объявлен, проброшен и **никем не задан** — тот же класс дефекта,
    за который репо уже платило (`supports_file_targets`, #448). Цена
    расхождения не косметическая: на ExUnit третий вызов разворачивал
    окружение и компилировал проект целиком.
    """

    def test_the_third_call_never_reaches_the_runner(self, tmp_path, monkeypatch):
        from spec_runner import tdd
        from spec_runner.negative_control import _classify_mutated, _replay_for_control

        root, head = _repo(tmp_path)
        cfg = _cfg(root)

        # Чистая половина — настоящая: переспрос идёт по её дереву и её
        # селектору, и подменять их значит проверять не тот путь.
        clean = _replay_for_control(cfg, sha=head, control=_control(), mutate=None, order=1)
        mutated = tdd.ReplayAttempt(
            stage="preflight",
            detail="the test file does not parse",
            environment_id=clean.environment_id,
            sha=head,
            selector=SELECTOR,
            mutated=True,
            order=2,
            refusal_code="unparseable_test_file",
        )

        runs: list = []
        real_run = tdd._run_selector
        monkeypatch.setattr(
            tdd, "_run_selector", lambda *a, **k: runs.append(a) or real_run(*a, **k)
        )

        verdict = _classify_mutated(cfg, clean, mutated)

        assert runs == [], f"переспрос разбора прогнал тесты {len(runs)} раз(а)"
        assert verdict.verdict == "unsatisfied", verdict

    def test_the_recheck_still_tells_a_broken_toolchain_apart(self, tmp_path):
        """Экономия не должна стоить различения: разбор на чистом дереве
        обязан по-прежнему отвечать, сломан ли инструмент."""
        from spec_runner import tdd
        from spec_runner.negative_control import _classify_mutated, _replay_for_control

        root, head = _repo(tmp_path)
        cfg = _cfg(root)
        clean = _replay_for_control(cfg, sha=head, control=_control(), mutate=None, order=1)
        broken = tdd.ReplayAttempt(
            stage="preflight",
            detail="the test file does not parse",
            environment_id=clean.environment_id,
            sha=head,
            selector="tests/test_subject.py::test_absent",
            mutated=True,
            order=2,
            refusal_code="unparseable_test_file",
        )

        verdict = _classify_mutated(cfg, clean, broken)

        # Чистое дерево РАЗБИРАЕТСЯ (файл цел), значит отказ — про патч.
        assert verdict.verdict == "unsatisfied", verdict


class TestTheSilenceOfASiteIsNotASkip:
    """kind: contract — находка ревью цепи.

    Докстринг гейта заявляет «пропускает только явное `True`: отсутствие
    ключа означает судить», а код читал `not ctx.facts.get(...)`, то есть
    отмывал молчание сайта в пропуск — ровно то, что `evaluate_claims` в
    этом же файле объявлено НЕ делать и что #429 закрыл у claims после
    того, как точка 1 села на путь, которым waived-задача не ходит.
    """

    def _ctx(self, tmp_path, **facts):
        from spec_runner.gates import GateContext

        return GateContext(
            task_id="TASK-008",
            checkpoint_sha="deadbeef",
            config=ExecutorConfig(project_root=tmp_path),
            state=None,
            facts=facts,
        )

    def test_a_missing_waiver_fact_is_an_instrument_error(self, tmp_path):
        from spec_runner.gates import GateStatus, _negative_control_gate

        result = _negative_control_gate(self._ctx(tmp_path))

        assert result.status is GateStatus.INSTRUMENT_ERROR, result
        assert "waiver" in (result.detail or "").lower(), result.detail

    def test_an_explicit_false_still_skips(self, tmp_path):
        """Задача без waiver'а за эту механику не платит ничем (NFR-02)."""
        from spec_runner.gates import GateStatus, _negative_control_gate

        result = _negative_control_gate(self._ctx(tmp_path, waiver_applied=False))

        assert result.status is GateStatus.SATISFIED, result
        assert "no addressed waiver" in (result.detail or "")


class TestARealTimeoutIsSeenAsOne:
    """kind: contract — находка ревью цепи.

    `subprocess.run(timeout=…)` поднимает `subprocess.TimeoutExpired` —
    подкласс `SubprocessError`, а НЕ `TimeoutError`. Проверка
    `isinstance(exc, TimeoutError)` никогда не была истинной, стадия
    `timeout` не возникала ни на одном производственном пути, а тест
    BEH-12 фабриковал `ReplayAttempt(stage="timeout")` — форму, которой код
    не производит. Шов общий с RED-путём, так что мёртвой ветка была и там.
    """

    def test_a_replay_that_times_out_reports_the_timeout_stage(self, tmp_path, monkeypatch):
        import subprocess as sp

        from spec_runner import tdd
        from spec_runner.negative_control import _replay_for_control

        root, head = _repo(tmp_path)

        def _explode(*a, **k):
            raise sp.TimeoutExpired(cmd=["pytest"], timeout=1)

        monkeypatch.setattr(tdd, "_run_selector", _explode)

        attempt = _replay_for_control(
            _cfg(root), sha=head, control=_control(), mutate=None, order=1
        )

        assert attempt.stage == "timeout", attempt
        assert attempt.outcome is None, "у таймаута нет исхода прогона"


class TestUnsatisfiedNeedsAPositiveObservation:
    """kind: contract — находка ревью круга 3 (блокирующая).

    Переспрос разбора снимает сомнение ТОЛЬКО положительным наблюдением
    «чистое дерево разбирается». Замыкающая строка читала любую неудачу
    самого переспроса — не удался `git worktree add`, таймаут, пропавший
    тулчейн, сорвавшийся разбор — как «патч виноват»: автору предъявлялось
    как факт о его работе то, что случилось с машиной между двумя
    половинами, и `retriable` при этом False, то есть без единого ретрая.
    Ровно то, что `_STAND_PREFLIGHT_CODES` двумя экранами выше запрещает.
    """

    def _mutated(self, head: str):
        from spec_runner import tdd

        return tdd.ReplayAttempt(
            stage="preflight",
            detail="the test file does not parse",
            environment_id="unpinned",
            sha=head,
            selector=SELECTOR,
            mutated=True,
            order=2,
            refusal_code="unparseable_test_file",
        )

    def test_a_recheck_that_cannot_run_is_an_instrument_error(self, tmp_path):
        """Стенд ломается по-настоящему: переспрос идёт по коммиту, которого
        нет, и не доходит до разбора вовсе."""
        from spec_runner import tdd
        from spec_runner.negative_control import _classify_mutated

        root, head = _repo(tmp_path)
        cfg = _cfg(root)
        absent = "0" * 40
        clean = tdd.ReplayAttempt(
            stage="run",
            detail="",
            environment_id="unpinned",
            sha=absent,
            selector=SELECTOR,
            mutated=False,
            order=1,
        )

        verdict = _classify_mutated(cfg, clean, self._mutated(absent))

        assert verdict.verdict == "instrument_error", verdict
        assert verdict.retriable, "сбой стенда обязан переисполняться"

    def test_a_stand_preflight_code_is_not_the_patchs_fault(self, tmp_path, monkeypatch):
        """Центральный случай ExUnit: `preflight` зовёт `elixir`, и его
        таймаут возвращается кодом `preflight_failed` — факт о машине.
        Переспрос подменён на своей границе, потому что воспроизводить
        пропавший тулчейн значит проверять окружение, а не правило."""
        from spec_runner import negative_control as nc
        from spec_runner import tdd
        from spec_runner.negative_control import _classify_mutated

        root, head = _repo(tmp_path)
        cfg = _cfg(root)
        clean = nc._replay_for_control(cfg, sha=head, control=_control(), mutate=None, order=1)

        def _broken_recheck(config, *, sha, control, mutate, order, preflight_only=False):
            return tdd.ReplayAttempt(
                stage="preflight",
                detail="checking the file did not complete (timeout or a failed parse run)",
                environment_id="unpinned",
                sha=sha,
                selector=control.selector,
                mutated=False,
                order=order,
                refusal_code="preflight_failed",
            )

        monkeypatch.setattr(nc, "_replay_for_control", _broken_recheck)

        verdict = _classify_mutated(cfg, clean, self._mutated(head))

        assert verdict.verdict == "instrument_error", verdict

    def test_a_clean_tree_that_parses_still_convicts_the_patch(self, tmp_path):
        """Починка не должна проглотить настоящий случай: положительное
        наблюдение по-прежнему даёт `unsatisfied`."""
        from spec_runner.negative_control import _classify_mutated, _replay_for_control

        root, head = _repo(tmp_path)
        cfg = _cfg(root)
        clean = _replay_for_control(cfg, sha=head, control=_control(), mutate=None, order=1)

        verdict = _classify_mutated(cfg, clean, self._mutated(head))

        assert verdict.verdict == "unsatisfied", verdict
