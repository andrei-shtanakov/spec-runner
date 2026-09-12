"""#433: компенсация обязана доходить до того, кто её исполняет.

Negative control — единственное условие класса waiver'а без исполняемого
гейта (#428). Пока ревьюер waived-задачи не видит ни класса, ни санкции,
ни требования, «за этим следит ревью» было утверждением ни о чём: проза
декомпозиции до него не доезжает, а проектный `review.txt` вытесняет
встроенный промпт целиком и подставляет только четыре переменные.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import WAIVER_REVIEW_OBLIGATIONS, ExecutorConfig
from spec_runner.review import WAIVER_HEADER, build_review_prompt
from spec_runner.task import Task

WAIVER = "characterisation · sanction: batch-approve-2026-09-09"


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


def _cfg(root: Path, **overrides) -> ExecutorConfig:
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


def _custom_template(cfg: ExecutorConfig) -> None:
    """Проектный `review.txt` БЕЗ чек-листа — наш живой случай.

    Кладётся ровно туда, откуда его читает `config.prompts_dir`, а не в
    угаданный путь: иначе тест «с пользовательским шаблоном» тихо
    исполнял бы встроенный, то есть проверял не тот случай.
    """
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.prompts_dir / "review.txt").write_text(
        "Review {{TASK_ID}}: {{TASK_NAME}}\n\nChanged:\n{{CHANGED_FILES}}\n\n{{GIT_DIFF}}\n",
        encoding="utf-8",
    )


class TestTheWaivedTaskReviewerIsTold:
    def test_with_the_builtin_prompt(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        prompt = build_review_prompt(_task(execution_mode="standard", tdd_waiver=WAIVER), cfg)
        assert WAIVER_HEADER in prompt
        assert "Class: characterisation" in prompt
        assert "Sanction: batch-approve-2026-09-09" in prompt
        assert WAIVER_REVIEW_OBLIGATIONS["characterisation"] in prompt

    def test_with_a_project_template_that_has_no_checklist(self, tmp_path):
        """Половина, ради которой блок ДОПИСЫВАЕТСЯ, а не вписывается.

        Вставь требование внутрь встроенного текста — и единственная
        конфигурация, где оно нужно (наша, со своим `review.txt`), стала
        бы единственной, где его нет.
        """
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _custom_template(cfg)
        prompt = build_review_prompt(_task(execution_mode="standard", tdd_waiver=WAIVER), cfg)
        assert prompt.startswith("Review TASK-008:"), (
            "проектный шаблон действительно вытеснил встроенный"
        )
        assert WAIVER_HEADER in prompt
        assert WAIVER_REVIEW_OBLIGATIONS["characterisation"] in prompt

    def test_the_block_demands_review_failed(self, tmp_path):
        """Требование обязано быть исполнимым действием, а не пожеланием."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        prompt = build_review_prompt(_task(execution_mode="standard", tdd_waiver=WAIVER), cfg)
        block = prompt.split(WAIVER_HEADER, 1)[1]
        assert "REVIEW_FAILED" in block
        assert "NEGATIVE CONTROL" in block or "negative control" in block.lower()

    def test_the_obligation_comes_from_the_class_not_the_marker(self, tmp_path):
        """Текст выводится из КЛАССА, а не из строки маркера.

        Иначе объявление диктовало бы условия, по которым его судят: вписал
        в санкцию «negative control не нужен» — и получил бы ревью,
        которому это сказано его же словами.
        """
        root = _repo(tmp_path)
        cfg = _cfg(root)
        task = _task(
            execution_mode="standard",
            tdd_waiver="characterisation · sanction: spec-runner#429",
        )
        prompt = build_review_prompt(task, cfg)
        assert WAIVER_REVIEW_OBLIGATIONS["characterisation"] in prompt
        # Санкция названа, но текст обязательства от неё не зависит.
        assert "Sanction: spec-runner#429" in prompt


class TestEveryoneElseIsUntouched:
    @pytest.mark.parametrize(
        "mode, waiver",
        [("standard", None), ("tdd", None), ("verify_first", None), (None, None)],
        ids=["обычный-standard", "tdd", "verify_first", "без-override"],
    )
    def test_no_block_without_a_marker(self, tmp_path, mode, waiver):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        prompt = build_review_prompt(_task(execution_mode=mode, tdd_waiver=waiver), cfg)
        assert WAIVER_HEADER not in prompt

    def test_no_block_with_a_project_template_either(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _custom_template(cfg)
        prompt = build_review_prompt(_task(execution_mode="standard"), cfg)
        assert prompt.startswith("Review TASK-008:")
        assert WAIVER_HEADER not in prompt

    def test_a_malformed_marker_adds_nothing(self, tmp_path):
        """Нечитаемое объявление не даёт полублока с выдуманными терминами.

        Проверяется САМА функция, а не `build_review_prompt`: по такому
        маркеру `append_frozen_files` (#430) поднимает `ConfigError`
        раньше — поведение прежнее и не этим PR заведённое, а до ревью
        такая задача и не доходит, её отвергает исполнение. Здесь важно
        одно: мы не сочиняем условия там, где объявление не прочитано.
        """
        from spec_runner.review import append_waiver_obligation

        root = _repo(tmp_path)
        cfg = _cfg(root)
        task = _task(execution_mode="standard", tdd_waiver="выдумка · sanction: нет")
        assert append_waiver_obligation("BODY", cfg, task) == "BODY"


def test_every_class_has_a_review_obligation():
    """Класс без обязательства невозможен ПОСТРОЕНИЕМ, а не по внимательности.

    `WAIVER_CLASSES` выводится из словаря обязательств, поэтому новый класс
    нельзя завести, не сказав, что ревьюер обязан по нему проверить.
    """
    from spec_runner.config import WAIVER_CLASSES

    assert set(WAIVER_CLASSES) == set(WAIVER_REVIEW_OBLIGATIONS)
    assert all(WAIVER_REVIEW_OBLIGATIONS[c].strip() for c in WAIVER_CLASSES)
