# Task-status integrity — Implementation Plan (issues #123, #124)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Закрыть три нарушения контракта исполнителя, найденные боевым прогоном
disputatio (D3, 2026-08-08): (1) `update_task_status` перекрашивает соседнюю
задачу; (2) `TASK_META` не читает bullet-формат, который генерирует собственный
`plan --full`; (3) `run --all` выходит 0 при расхождении state-DB ↔ tasks.md.

**Architecture:** Точечные fail-closed правки в `src/spec_runner/task.py`
(парсер/апдейтер, + синхронизация bundled-копии в
`src/spec_runner/skills/spec-generator-skill/templates/task.py`) и в
run-цикле `src/spec_runner/cli.py` (реконсиляция после каждой задачи + backstop
в конце). Golden fixture — ЖИВОЙ maestro-tasks.md из forensic-снапшота инцидента.

**Tech Stack / conventions:** по CLAUDE.md репо (uv, `uv run pytest`, ruff,
существующие паттерны тестов в `tests/`). Ветка `fix/task-status-integrity` от
master. PR мержит человек.

## Global Constraints

- Только uv; `uv run pytest` / `uv run ruff check .`; следовать стилю репо.
- Fail-closed везде: «не нашёл/не уверен» = отказ без записи, не догадка.
- Обратная совместимость: легитимные существующие tasks.md (эмодзи и plain
  формат без bullet) парсятся байт-в-байт как раньше — прогон полного suite
  обязателен после каждой задачи.
- Владелец зафиксировал семантику exit run --all: (а) все done → 0;
  (б) документированный terminal failure/blocking → существующая
  nonzero/stop-семантика; (в) state↔spec mismatch ИЛИ nonterminal-задачи без
  объяснимого блокера → nonzero `state_spec_mismatch`; (г) «неизвестно, почему
  нет ready» НИКОГДА не 0. Грубое `remaining>0 → error` запрещено.
- Golden fixture: скопировать
  `/Users/Andrei_Shtanakov/labs/disputatio-ws/forensics/w-contracts/tasks-193012.md`
  (первый снапшот, все статусы TODO/IN_PROGRESS, чередование `- 🔴 P0 |` и
  `🔴 P0 |`) в `tests/fixtures/maestro-interop/` под именем
  `alternating-bullet-tasks.md` — использовать в задачах 2 и 4.
- Версия: patch bump в `pyproject.toml` + запись в CHANGELOG.md (Task 4).

---

### Task 1: task-bounded `update_task_status`

**Files:** Modify `src/spec_runner/task.py` (~:218); Test
`tests/test_task_status_update.py` (новый или существующий по конвенции репо).

**Контракт (владелец, дословно):** найти header с ТОЧНЫМ ID
(`TASK_HEADER.match(line)` и `match.group(1) == task_id`, не substring) →
искать meta ТОЛЬКО до следующего TASK_HEADER → meta не найдена → `return False`
БЕЗ записи файла и БЕЗ log_change → менять ровно одну строку → после записи
перечитать файл и подтвердить, что статус ЦЕЛЕВОЙ задачи стал new_status
(не подтвердился → False + warning-лог).

- [ ] Падающие тесты: (1) meta целевой задачи не распознана (bullet-формат при
  старом TASK_META) → соседняя задача НЕ изменена, return False, history-файл
  не пополнен; (2) `update_task_status(file, "TASK-001", ...)` при
  существующем `TASK-0011` не трогает его (точный ID); (3) после успешного
  апдейта изменена ровно одна строка файла (сравнение построчно);
  (4) повторение живого сценария: файл с `- `-meta у TASK-001 и голой meta у
  TASK-002 → апдейт TASK-001 НЕ красит TASK-002 (регресс инцидента).
- [ ] Красный прогон зафиксирован → реализация → зелёный.
- [ ] Полный suite + ruff. Commit.

### Task 2: `TASK_META` принимает bullet-префикс + синхронизация template-копии + golden parse

**Files:** Modify `src/spec_runner/task.py:26`; Modify
`src/spec_runner/skills/spec-generator-skill/templates/task.py` (там свой
старый TASK_META И небезопасный update_task_status — перенести ОБЕ правки);
Create fixture `tests/fixtures/maestro-interop/alternating-bullet-tasks.md`;
Test-файл по конвенции.

**Контракт:** TASK_META дополнительно принимает разрешённый markdown-bullet
префикс (`- ` / `* ` c отступом) перед meta — БЕЗ превращения в
match-anything (описательные строки, начинающиеся с `- ` без `P\d |`-формы,
по-прежнему не матчатся; regression: строки описаний и checklist-строки не
стали meta). `parse_tasks` на golden fixture видит все 11 задач с корректными
статусами обоих форматов; `update_task_status` работает для задач в ОБОИХ
форматах (обновляет их собственную meta, Task 1 гарантирует границы).

- [ ] Падающие тесты (golden parse + оба формата update + не-регрессия
  description-строк) → красный → реализация (runtime + template) → зелёный.
- [ ] diff template-копии против runtime-версии функций — совпадение по
  смыслу зафиксировано в отчёте. Полный suite + ruff. Commit.

### Task 3: fail-closed reconciliation в run-цикле

**Files:** Modify `src/spec_runner/cli.py` (run-цикл, ~:695-770) и/или
`executor.py` по фактическому месту; Test по конвенции (юнит на хелпер +
интеграционный на цикл с фейковым исполнителем — поискать существующие
паттерны фейков в tests/).

**Контракт:**
1. Основной гейт СРАЗУ после успешного `run_with_retries`: state-DB говорит
   success → перечитать tasks.md → целевая задача обязана быть done; иначе
   `stop_reason=state_spec_mismatch`, немедленный стоп рана, exit nonzero.
2. Backstop в ветке «No more ready tasks»: если существуют nonterminal-задачи
   И множество done-ID в tasks.md расходится с success-ID в state-DB →
   exit nonzero со структурированным `state_spec_mismatch` (в лог — оба
   множества). Легитимный случай «TODO заблокированы объяснимым
   failure/skip» сохраняет текущую семантику (см. Global Constraints, матрица
   владельца).

- [ ] Падающие тесты: (1) подмена статуса в tasks.md после success →
  немедленный stop + nonzero; (2) «нет ready, есть nonterminal, DB/file
  расходятся» → nonzero state_spec_mismatch; (3) «нет ready, TODO блокированы
  задачей со статусом failed при on_task_failure=skip» → существующее
  поведение НЕ изменилось (exit как сейчас); (4) все done → 0.
- [ ] Красный → реализация → зелёный. Полный suite + ruff. Commit.

### Task 4: e2e-регресс инцидента + версия

**Files:** Test e2e по конвенции репо (фейковый агент-CLI — найти существующий
паттерн в tests/, НЕ живой claude); Modify `pyproject.toml` (patch bump),
`CHANGELOG.md`.

**Приёмка владельца (все пункты — ассерты):** fixture 9–11 задач с
чередованием bullet-meta; `run --all` (с фейковым исполнителем, творящим
тривиальные правки) исполняет ВСЕ задачи; после каждой — DB и markdown
согласованы; ни одна соседняя задача не меняется преждевременно;
искусственная порча meta после success → nonzero. (Пункт «Maestro не
переводит WS в DONE» — вне этого репо, покрывается nonzero-exit-ом.)

- [ ] e2e-тест красный на master-логике (запустить точечно против
  отреверченных правок не нужно — достаточно, что он зелёный после Task 1-3 и
  что его ассерты содержательны: обязательный анти-тавтологичный прогон —
  временно вернуть старый TASK_META локально и убедиться, что e2e падает;
  вернуть).
- [ ] Patch bump версии + CHANGELOG-запись (три фикса, ссылки #123/#124,
  провенанс: найдено боевым прогоном disputatio D3).
- [ ] Полный suite + ruff. Commit.

### Task 5: PR + Copilot

- [ ] Push `fix/task-status-integrity`, `gh pr create`: состав, живой инцидент
  (ссылки на #123/#124, maestro#164), семантическая матрица exit-кодов,
  «Closes #123, Closes #124». Мерж — человек.
- [ ] Copilot-ревью отработать (валидное чинить, невалидное аргументировать).

## Self-review

Покрытие: все три пункта scope владельца → Task 1/2/3; шаблонная копия — Task 2;
golden из живого forensic — Task 2/4; матрица exit — Task 3 + Global
Constraints; приёмка e2e — Task 4; анти-тавтологreleased-проверка e2e — Task 4.
Имена согласованы: `state_spec_mismatch` сквозной. Вне скоупа (задокументировано):
maestro-гейт минимальной версии — follow-up после релиза (#124 хвост).
