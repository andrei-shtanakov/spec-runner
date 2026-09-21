"""BEH-05…BEH-08 (DT-02) — общий шов реплея и исполнение обеих половин.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Source: .../20-design.md §2
Traces: FR-02, NFR-01, NFR-03, NFR-04

Предмет — ИСПОЛНЕНИЕ, а не вердикт: `ReplayAttempt` — факт о прогоне,
классификация в DT-03. Здесь проверяется, что прогоны происходят в нужном
порядке, против нужного дерева, ровно один раз каждый, и что после них не
остаётся ни worktree, ни платных вызовов.

Стенд — настоящий git-репозиторий с настоящим pytest: реплей судит коммит,
и подменить его двойником значит проверить не то.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.task import NegativeControl

SELECTOR = "tests/test_subject.py::test_property"

SUBJECT_GREEN = """def value():
    return 1
"""

TEST_FILE = """from subject import value


def test_property():
    assert value() == 1
"""

#: Мутант благословлённой формы: ломает СВОЙСТВО, а не тест. Строку
#: объявления теста не трогает — ограничение Q-G соблюдено.
PATCH = """--- a/subject.py
+++ b/subject.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""


def _git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, *, patch: str = PATCH) -> tuple[Path, str]:
    """Репозиторий с тестом, предметом и патчем; возвращает root и HEAD."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "spec" / "negative-controls").mkdir(parents=True)
    (root / "subject.py").write_text(SUBJECT_GREEN, encoding="utf-8")
    (root / "tests" / "test_subject.py").write_text(TEST_FILE, encoding="utf-8")
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


def _cfg(root: Path) -> ExecutorConfig:
    return ExecutorConfig(
        project_root=root,
        state_file=root / "spec" / "state.db",
        logs_dir=root / "spec" / "logs",
        # Настоящий pytest текущего окружения, а не `pytest` из PATH
        # оператора: стенд обязан быть воспроизводим на чужой машине.
        test_command=f"{sys.executable} -m pytest",
    )


def _control() -> NegativeControl:
    from pathlib import PurePosixPath

    return NegativeControl(
        patch=PurePosixPath("spec/negative-controls/TASK-008.patch"), selector=SELECTOR
    )


def _worktrees(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "worktree", "list"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if "repo " not in line and str(root) not in line]


class TestBEH05CleanHalfRunsFirst:
    """kind: integration — BEH-05: порядок половин наблюдаем, обе судят один
    коммит, обе используют один и тот же объявленный селектор."""

    def test_clean_half_is_executed_before_the_mutated_one(self, tmp_path):
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        assert clean.mutated is False, "первой исполнена не чистая половина"
        assert mutated is not None and mutated.mutated is True
        assert clean.order < mutated.order, (
            f"порядок нарушен: чистая {clean.order}, мутированная {mutated.order}"
        )

    def test_both_halves_judge_the_same_commit_and_selector(self, tmp_path):
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        assert clean.sha == head and mutated.sha == head
        assert clean.selector == SELECTOR and mutated.selector == SELECTOR


class TestBEH06MutatedHalfNeedsAGreenCleanHalf:
    """kind: integration — BEH-06: без доказанной исправности стенда вторая
    половина не запускается вовсе, и негодный стенд стоит один прогон, а не
    два (NFR-04)."""

    def test_a_red_clean_half_stops_before_the_mutant(self, tmp_path):
        from spec_runner.negative_control import replay_both_halves

        # Тест падает БЕЗ всякого мутанта — то самое мошенничество, ради
        # которого требование обеих половин и существует.
        root, head = _repo(tmp_path)
        (root / "subject.py").write_text("def value():\n    return 99\n", encoding="utf-8")
        _git(root, "commit", "-am", "broken subject")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        assert mutated is None, "мутированная половина запущена на негодном стенде"
        assert clean.outcome is not None, "чистая половина не дошла до прогона"

    def test_a_missing_selector_stops_before_the_mutant(self, tmp_path):
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)
        control = NegativeControl(
            patch=_control().patch, selector="tests/test_subject.py::test_absent"
        )

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=control)

        assert mutated is None
        assert clean.stage in ("preflight", "run"), clean.stage


class TestBEH07TheCommitIsJudgedNotTheWorkingTree:
    """kind: integration — BEH-07: реплей судит коммит; посторонняя правка
    рабочего дерева на исход не влияет."""

    def test_a_dirty_working_tree_does_not_change_the_verdict(self, tmp_path):
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)
        before, _ = replay_both_halves(_cfg(root), sha=head, control=_control())

        # HEAD уходит ВПЕРЁД и ломает предмет, кандидат остаётся прежним.
        # Двигать только рабочее дерево недостаточно: `git worktree add`
        # без sha берёт HEAD, и пока кандидат совпадает с HEAD, «судит
        # коммит» и «судит HEAD» неразличимы — мутация, снимающая sha,
        # проходила зелёной.
        (root / "subject.py").write_text("def value():\n    return 777\n", encoding="utf-8")
        _git(root, "commit", "-am", "later commit breaks the subject")
        (root / "subject.py").write_text("def value():\n    return 555\n", encoding="utf-8")

        after, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        assert after.outcome == before.outcome, (
            f"рабочее дерево повлияло на вердикт: {before.outcome} → {after.outcome}"
        )
        assert mutated is not None

    def test_selector_identity_is_read_inside_each_half(self, tmp_path):
        """Q-G: идентичность объявления читается в КАЖДОЙ половине, пока её
        дерево живо — одновременных worktree не существует."""
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        # На pytest node id устойчив к сдвигам, и идентичность не читается
        # вовсе: поле объявлено и честно пусто, а не выдумано.
        assert clean.selector_identity is None
        assert mutated.selector_identity is None


class TestBEH08NoPaidCallsNoLeftoverWorktrees:
    """kind: integration — NFR-01, NFR-03: контроль не платит агенту и не
    оставляет за собой деревьев ни на одном пути."""

    def test_the_happy_path_leaves_nothing_behind(self, tmp_path):
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)

        replay_both_halves(_cfg(root), sha=head, control=_control())

        assert _worktrees(root) == [], f"worktree остался: {_worktrees(root)}"

    def test_an_inapplicable_patch_leaves_nothing_behind(self, tmp_path):
        """Отказной путь — тот, на котором `finally` и проверяется."""
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path, patch="this is not a patch at all\n")

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        assert mutated is not None and mutated.stage == "mutate", mutated
        assert _worktrees(root) == [], f"worktree остался после отказа: {_worktrees(root)}"

    def test_no_paid_binary_is_ever_reached(self, tmp_path):
        """Пояс `PaidBinaryReached` активен во всех тестах; отдельный тест
        нужен потому, что «не поднялось» и «не проверялось» неразличимы без
        утверждения."""
        from spec_runner.negative_control import replay_both_halves

        root, head = _repo(tmp_path)

        clean, mutated = replay_both_halves(_cfg(root), sha=head, control=_control())

        assert clean.outcome is not None and mutated is not None
