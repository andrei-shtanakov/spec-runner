"""BEH-22 (DT-06, Could) — прогон контроля по требованию не подменяет гейт.

Source: workstreams/executable-negative-control-under-standard-20260921/spec/15-behaviour-spec.md
Traces: FR-12
Acceptance: AC-19

Единственное связывающее условие сформулировано сценарием: команда
печатает ТОТ ЖЕ вердикт, который вынесет гейт на тех же входах, и НЕ
оставляет свидетельства, которое гейт затем прочитает как своё, — иначе
она становится способом обойти гейт.

«Тот же вердикт» здесь измеряется, а не предполагается: та же задача на
том же репозитории проходит настоящий `post_done_hook`, и его вердикт
сравнивается с напечатанным.
"""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from tests.test_negative_control_wiring import (  # noqa: F401 — стенд общий
    GOOD_PATCH,
    USELESS_PATCH,
    _cfg,
    _repo,
    _rows,
    _run,
    _task,
)


def _args(**overrides) -> Namespace:
    defaults = {"tdd_command": "control", "task_id": "TASK-008", "sha": None, "json": False}
    defaults.update(overrides)
    return Namespace(**defaults)


def _on_demand(root: Path, cfg: ExecutorConfig, capsys, **overrides) -> tuple[int, str]:
    from spec_runner.tdd_control import cmd_tdd_control

    code = cmd_tdd_control(_args(**overrides), cfg)
    return code, capsys.readouterr().out


class TestBEH22TheVerdictMatchesTheGate:
    """kind: integration — BEH-22/AC-19, первая половина."""

    @pytest.mark.parametrize(
        ("patch", "expected", "exit_code"),
        [(GOOD_PATCH, "satisfied", 0), (USELESS_PATCH, "unsatisfied", 1)],
        ids=["satisfied", "unsatisfied"],
    )
    def test_the_printed_verdict_is_the_gates_verdict(
        self, tmp_path, monkeypatch, capsys, patch, expected, exit_code
    ):
        root = _repo(tmp_path, patch=patch)
        cfg = _cfg(root)

        code, out = _on_demand(root, cfg, capsys)
        # Теперь — настоящий гейтовый путь на тех же входах.
        _run(root, cfg, monkeypatch)
        gate_rows = _rows(cfg)

        assert expected.upper() in out, out
        assert code == exit_code, (code, out)
        assert gate_rows and gate_rows[-1]["verdict"] == expected, gate_rows

    def test_json_carries_both_halves_and_the_sha(self, tmp_path, capsys):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        code, out = _on_demand(root, cfg, capsys, json=True)
        payload = json.loads(out)

        assert code == 0
        assert payload["verdict"] == "satisfied"
        assert payload["sha"] == head
        assert payload["clean"]["outcome"] == "tests_passed"
        assert payload["mutated"]["outcome"] == "tests_failed"


class TestBEH22NoEvidenceIsLeftBehind:
    """kind: integration — BEH-22/AC-19, вторая половина: команда не может
    стать способом обойти гейт."""

    def test_the_command_writes_no_negative_controls_row(self, tmp_path, capsys):
        root = _repo(tmp_path)
        cfg = _cfg(root)

        _on_demand(root, cfg, capsys)

        assert _rows(cfg) == [], "прогон по требованию оставил свидетельство"

    def test_the_gate_still_runs_the_control_itself_afterwards(self, tmp_path, monkeypatch, capsys):
        """После команды гейтовый путь исполняет контроль САМ, а не читает
        чужой результат."""
        from spec_runner import negative_control as nc

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _on_demand(root, cfg, capsys)

        replays: list = []
        real = nc.run_negative_control
        monkeypatch.setattr(
            nc, "run_negative_control", lambda *a, **k: replays.append(1) or real(*a, **k)
        )
        _run(root, cfg, monkeypatch)

        assert len(replays) == 1, f"гейт не исполнил контроль сам: {len(replays)}"
        assert len(_rows(cfg)) == 1, "свидетельств больше одного — чьё второе?"

    def test_an_unsatisfied_on_demand_run_writes_nothing_either(self, tmp_path, capsys):
        root = _repo(tmp_path, patch=USELESS_PATCH)
        cfg = _cfg(root)

        code, _ = _on_demand(root, cfg, capsys)

        assert code == 1
        assert _rows(cfg) == []


class TestRefusalsAreTheGatesRefusals:
    """kind: contract — команда отказывает там же и теми же словами, где
    отказал бы гейт: ни один вход не получает вердикта, которого гейт не
    вынес бы."""

    def test_a_task_without_a_control_is_refused_not_judged(self, tmp_path, capsys):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        (root / "spec" / "tasks.md").write_text(
            "## Tasks\n\n### TASK-008: characterisation\n"
            "P2 | 🔄 IN_PROGRESS   Est: 0.5d\n**Mode:** standard\n"
            "**TDD-waiver:** characterisation · sanction: batch-approve-2026-09-09\n\n"
            "**Checklist:**\n- [ ] пункт\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "commit", "-qam", "no control"], cwd=root, check=True)

        code, out = _on_demand(root, cfg, capsys)

        assert code == 1, out
        assert "Negative-control" in out, out
        assert _rows(cfg) == []

    def test_an_unknown_task_is_refused_by_name(self, tmp_path, capsys):
        root = _repo(tmp_path)
        cfg = _cfg(root)

        code, out = _on_demand(root, cfg, capsys, task_id="TASK-999")

        assert code == 1 and "TASK-999" in out, out

    def test_uncommitted_work_is_refused_as_the_gate_refuses_it(self, tmp_path, capsys):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        (root / "subject.py").write_text("def value():\n    return 2\n", encoding="utf-8")

        code, out = _on_demand(root, cfg, capsys)

        assert code == 2, out
        assert "uncommitted" in out.lower(), out


class TestAnUnreadableWaiverIsRefusedInTheGatesWords:
    """kind: contract — находка локального круга DT-06.

    Маркер `**TDD-waiver:**` есть, но не резолвится (`Mode: tdd`). Гейт на
    этом входе отказывает текстом `ConfigError` из `resolve_waiver` — ДО
    любой проверки контроля. Команда отвечала «carries no addressed
    waiver»: неправда про файл, который маркер содержит, — та же пара
    «маркера нет» / «маркер негоден», что и у `validate` (круг 16).
    """

    def test_the_command_repeats_the_resolver_refusal(self, tmp_path, capsys):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        (root / "spec" / "tasks.md").write_text(
            "## Tasks\n\n### TASK-008: characterisation\n"
            "P2 | 🔄 IN_PROGRESS   Est: 0.5d\n**Mode:** tdd\n"
            "**TDD-waiver:** characterisation · sanction: batch-approve-2026-09-09\n"
            "**Negative-control:** spec/negative-controls/TASK-008.patch :: "
            "tests/test_subject.py::test_property\n\n"
            "**Checklist:**\n- [ ] пункт\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "commit", "-qam", "unreadable waiver"], cwd=root, check=True)

        code, out = _on_demand(root, cfg, capsys)

        assert code == 1, out
        assert "execution mode is 'tdd'" in out, out
        assert "carries no addressed waiver" not in out, out
        assert _rows(cfg) == []
