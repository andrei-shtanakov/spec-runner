"""BEH-20 (DT-05) — проект без waived-задач не платит за механику ничем.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Traces: NFR-02
Acceptance: AC-16

Измерение дифференциальное и внутри одного прогона: обычная задача
исполняется дважды — с механикой как есть и с её предикатом входа,
подменённым на «не наша задача». Число git-вызовов, прогонов тестов и
открытий state DB обязано совпасть: тогда сам предикат ничего не стоит, а
всё остальное за ним не исполняется. Замер «до изменения» в тесте
недостижим — master до фичи не запустить, — а дифференциал внутри одного
дерева отвечает на тот же вопрос честнее, чем сравнение с числом из
памяти.

Почему здесь, а не в DT-01: до DT-04 замер показал бы отсутствие того, что
ещё не написано.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from spec_runner import hooks
from spec_runner.config import ExecutorConfig
from spec_runner.task import Task


def _git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "subject.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_subject.py").write_text(
        "from subject import value\n\n\ndef test_property():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    (root / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n",
        encoding="utf-8",
    )
    (root / "spec").mkdir()
    (root / "spec" / "tasks.md").write_text(
        "## Tasks\n\n### TASK-007: ordinary\nP2 | 🔄 IN_PROGRESS   Est: 0.5d\n"
        "**Mode:** standard\n\n**Checklist:**\n- [ ] пункт\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / ".executor-state.db",
        "logs_dir": root / "spec" / ".logs",
        "test_command": f"{sys.executable} -m pytest",
        "create_git_branch": False,
        "run_tests_on_done": True,
        "run_lint_on_done": False,
        "run_review": False,
        "auto_commit": True,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _plain_task() -> Task:
    return Task(
        id="TASK-007",
        name="ordinary",
        priority="p2",
        status="in_progress",
        estimate="0.5d",
        execution_mode="standard",
    )


class _Meter:
    """Счётчики трёх ресурсов, которые NFR-02 называет по имени."""

    def __init__(self, monkeypatch) -> None:
        from spec_runner import git_ops, state

        self.git = 0
        self.tests = 0
        self.db_opens = 0
        real_run = subprocess.run
        real_enter = state.ExecutorState.__enter__

        def _count(argv, *a, **k):
            cmd = argv if isinstance(argv, str) else " ".join(map(str, argv))
            if cmd.startswith("git") or (isinstance(argv, list) and argv and argv[0] == "git"):
                self.git += 1
            if "pytest" in cmd:
                self.tests += 1
            return real_run(argv, *a, **k)

        def _enter(inner):
            self.db_opens += 1
            return real_enter(inner)

        monkeypatch.setattr(subprocess, "run", _count)
        monkeypatch.setattr(hooks.subprocess, "run", _count, raising=False)
        monkeypatch.setattr(git_ops.subprocess, "run", _count, raising=False)
        monkeypatch.setattr(state.ExecutorState, "__enter__", _enter)

    def snapshot(self) -> tuple[int, int, int]:
        return (self.git, self.tests, self.db_opens)


def _run_once(tmp_path: Path, monkeypatch, *, mechanism: bool) -> tuple[tuple, str, str]:
    root = _repo(tmp_path / ("with" if mechanism else "without"))
    cfg = _cfg(root)
    (root / "widget.py").write_text("x = 1\n", encoding="utf-8")
    if not mechanism:
        # «Как до изменения»: механика вырезается ЦЕЛИКОМ, каждым своим
        # сайтом в хуке, а не только предикатом входа. Подмена одного
        # предиката измеряла бы лишь «предикат ничего не стоит»: стоимость,
        # стоящая ДО него (git-вызов перед проверкой, глобальная
        # регистрация гейта), оплачивалась бы в обоих плечах одинаково и
        # оставалась невидимой — ровно тот дефект NFR-02, который уже
        # случался (`hooks.py`, комментарий у вычисления SHA).
        monkeypatch.setattr(hooks, "_control_will_run", lambda *a, **k: False)
        monkeypatch.setattr(
            hooks, "_run_negative_control_before_review", lambda *a, **k: (None, "", "")
        )
        monkeypatch.setattr(hooks, "_negative_control_facts", lambda *a, **k: {})
        monkeypatch.setattr(hooks, "_record_negative_control", lambda *a, **k: None)
    before = _git(root, "rev-parse", "HEAD").stdout.strip()
    meter = _Meter(monkeypatch)
    hooks.post_done_hook(_plain_task(), cfg, True)
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    return meter.snapshot(), before, after


class TestBEH20AnOrdinaryProjectPaysNothing:
    """kind: integration — BEH-20/AC-16."""

    def test_git_calls_test_runs_and_db_opens_match_the_run_without_the_mechanism(
        self, tmp_path, monkeypatch
    ):
        import pytest

        # Каждому плечу — свой контекст подмен. `monkeypatch.undo()` между
        # плечами снял бы и автоподмены харнесса, в том числе пояс против
        # платных бинарей, на всё второе плечо.
        with pytest.MonkeyPatch.context() as mp:
            with_mech, _, _ = _run_once(tmp_path, mp, mechanism=True)
        with pytest.MonkeyPatch.context() as mp:
            without, _, _ = _run_once(tmp_path, mp, mechanism=False)

        assert with_mech == without, (
            f"механика стоит обычной задаче: с ней {with_mech}, без неё {without} "
            "(git-вызовы, прогоны тестов, открытия state DB)"
        )

    def test_the_mechanism_does_not_force_a_candidate_commit(self, tmp_path, monkeypatch):
        """Без ревью и без зарегистрированных гейтов кандидат-коммит раньше
        не создавался; наличие механики не должно этого менять."""
        from spec_runner.gates import REGISTRY

        for gate_id, phase in (
            list(getattr(REGISTRY, "_gates", {}).keys()) if hasattr(REGISTRY, "_gates") else []
        ):
            REGISTRY.unregister(gate_id, phase)
        _, before, after = _run_once(tmp_path, monkeypatch, mechanism=True)

        # Финальный `commit_task_work` — прежнее поведение (#103) и оно
        # остаётся; проверяется, что НЕ появился второй, промежуточный
        # кандидат: ровно один коммит поверх базы.
        root = tmp_path / "with" / "repo"
        count = subprocess.run(
            ["git", "rev-list", "--count", f"{before}..{after}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert count in ("0", "1"), f"механика добавила коммитов: {count}"

    def test_no_control_evidence_is_written_for_an_ordinary_task(self, tmp_path, monkeypatch):
        from spec_runner.state import ExecutorState
        from spec_runner.tdd import resolve_namespace

        _run_once(tmp_path, monkeypatch, mechanism=True)
        cfg = _cfg(tmp_path / "with" / "repo")

        with ExecutorState(cfg) as state:
            rows = state.negative_controls(resolve_namespace(cfg))

        assert rows == [], rows
