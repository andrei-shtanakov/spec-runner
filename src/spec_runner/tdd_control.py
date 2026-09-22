"""`spec-runner tdd control <TASK-ID>` — прогон негативного контроля по требованию (#428, FR-12).

Автор задачи прогоняет обе половины, не доводя задачу до терминального
сайта, и видит ТОТ ЖЕ вердикт, который вынесет гейт. Два условия, и оба
держатся построением, а не обещанием:

- **вердикт — гейтовый**, потому что судья один: `hooks._judge_negative_control`
  зовут и гейтовый путь, и эта команда; отказы по объявлению — тем же
  читателем, что точка 1 (`negative_control.declaration_refusal`);
- **свидетельства нет**: судья ничего не пишет, запись живёт только у
  гейтового вызывающего (`_run_negative_control_before_review`). Команда не
  может стать способом обойти гейт: после неё гейт исполняет контроль сам.

Коды выхода — по видам отказа гейта: 0 satisfied; 1 unsatisfied и отказы по
объявлению (POLICY — работа); 2 instrument_error (о работе ничего не
известно).
"""

from __future__ import annotations

import json
from typing import Any

from .config import ExecutorConfig
from .task import Task, parse_tasks

_EXIT = {"satisfied": 0, "unsatisfied": 1, "instrument_error": 2}


def _half(attempt) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "stage": attempt.stage,
        "outcome": attempt.outcome.value if attempt.outcome else None,
        "proof": attempt.proof.value if attempt.proof else None,
        "execution_proven": attempt.execution_proven,
        "refusal_code": attempt.refusal_code,
        "detail": (attempt.detail or "")[:400],
    }


def _find_task(config: ExecutorConfig, task_id: str) -> Task | None:
    if not config.tasks_file.exists():
        return None
    for task in parse_tasks(config.tasks_file):
        if task.id == task_id:
            return task
    return None


def cmd_tdd_control(args, config: ExecutorConfig) -> int:
    """Прогнать контроль по требованию и напечатать гейтовый вердикт."""
    from .hooks import _judge_negative_control
    from .negative_control import declaration_refusal

    task_id = args.task_id
    as_json = bool(getattr(args, "json", False))
    task = _find_task(config, task_id)
    if task is None:
        _emit(as_json, task_id, "", "refused", f"{task_id} is not in {config.tasks_file}", None)
        return 1

    # Тем же порядком, что гейт: `execute_task` резолвит waiver ПЕРВЫМ и
    # отказывает текстом `ConfigError` до любой проверки контроля. Маркер,
    # который есть, но не резолвится, — не «маркера нет»: та же пара
    # дефектов, что `validate` различает у себя.
    from .config import ConfigError

    try:
        config.resolve_waiver(task)
    except ConfigError as exc:
        _emit(as_json, task_id, "", "refused", str(exc), None)
        return 1

    refusal = declaration_refusal(task, config)
    if refusal is not None:
        _emit(as_json, task_id, "", "refused", refusal, None)
        return 1

    judged = _judge_negative_control(task, config, getattr(args, "sha", None))
    if judged is None:
        _emit(
            as_json,
            task_id,
            "",
            "refused",
            f"{task_id} carries no addressed waiver with a negative control, or "
            "auto_commit is off — there is nothing to judge",
            None,
        )
        return 1

    result, sha = judged
    _emit(as_json, task_id, sha, result.verdict, result.detail, result)
    return _EXIT.get(result.verdict, 2)


def _emit(as_json: bool, task_id: str, sha: str, verdict: str, detail: str, result) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "sha": sha,
                    "verdict": verdict,
                    "detail": detail,
                    "clean": _half(result.clean) if result is not None else None,
                    "mutated": _half(result.mutated) if result is not None else None,
                    # Записано ровно затем, чтобы читатель не искал строку в
                    # `negative_controls`: её нет и не будет — BEH-22.
                    "evidence_written": False,
                },
                indent=2,
            )
        )
        return
    where = f" @ {sha[:12]}" if sha else ""
    print(f"{task_id}{where}: {verdict.upper()}")
    if result is not None:
        for name, half in (("clean", result.clean), ("mutated", result.mutated)):
            if half is None:
                print(f"  {name:<8} (not run)")
                continue
            outcome = half.outcome.value if half.outcome else half.stage
            proof = half.proof.value if half.proof else "—"
            print(f"  {name:<8} {outcome} · selection {proof}")
    print(f"  {detail}")
    print("  (no evidence written — the gate will run the control itself)")
