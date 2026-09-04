---
traces_to:
- behaviour-spec
upstream_hashes:
  behaviour-spec: df3885c01babf4f043c1ad31829bba1e4df67ac8
spec_stage: tasks
status: approved
version: 2
generated_by: fleet-agent
generated_at: '2026-09-04T09:47:42'
source_prompt_version: ''
validation: pass
approved_by: andrei-shtanakov
approved_at: '2026-09-04T06:17:57Z'
owner_role: stream-owner
---

## Milestone 1: Red-pass robustness: авто-fix тривиального линта red-файла перед заморозкой (spec-runner#341) + ws-scoped имена red-файлов между workstream'ами (spec-runner#334)

Сгенерировано task_bridge из behaviour-spec бандла WS-spec-runner-341 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Draft: исполнение только после человеческого approve.

### Решения открытых вопросов (уровень design, зафиксированы до исполнения)

Профиль конвейера стадии design не содержит; чтобы задачи не решали
архитектурные вопросы каждая по-своему, ответы фиксируются здесь — в границах
продуктовых рамок requirements (правка решений = правка этой спеки, не
молчаливый выбор исполнителя):

- **Q-03 — форма fix-инвокации:** отдельная объявленная `commands.lint_fix`
  с собственным битом declared-ности (наличие ключа в конфиге проекта =
  объявление, симметрично `lint_command_declared`); python-дефолт
  `lint_fix_command` объявлением не является (FR-05). Применяется суженной до
  claim-путей правилом `_lint_claimed` (FR-01); при композитной команде не
  применяется (FR-09).
- **Q-04 — форма починки в истории:** subject-сохраняющий amend
  коммита-кандидата — единственная форма, совместимая с усыновляемостью
  остатка (#261, NFR-08) и предъявимостью дифа (BEH-25): SHA чекпойнта
  указывает на исправленную версию, subject остаётся распознаваемым
  `_unregistered_red`.
- **Q-06 — где живёт правило имени:** в адаптере (`evidential_file`), как
  требуют FR-11/FR-12; промпт и харнесс потребляют имя из адаптера, не
  дублируя формулу; харнесс-проверка #252 D по-прежнему проверяет фактически
  заявленный путь (`claim_paths(selector)`).

### TASK-001: Устранимая линт-находка доводит попытку до чекпойнта
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-01

**Checklist:**
- [x] реализовать BEH-01: Устранимая линт-находка доводит попытку до чекпойнта
- [x] проверка группы: tests/test_red_lint_autofix.py (kind: integration) зелёные на BEH-01

**Traces to:** [FR-01], [FR-03]

### TASK-002: Замороженные байты — ровно те, на которых выполнен реплей (+1 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-02, BEH-25.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-02 (—BEH-25)
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-02: Замороженные байты — ровно те, на которых выполнен реплей
- [x] реализовать BEH-25: Диф починки предъявим в истории
- [x] проверка группы: tests/test_frozen_bytes_are_the_replayed_bytes.py (kind: contract), tests/test_frozen_bytes_are_the_replayed_bytes.py (kind: integration) зелёные на BEH-02, BEH-25

**Traces to:** [FR-03], [FR-01], [FR-02]

### TASK-003: Починка, вышедшая за границы заявляемого файла, — отказ
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-03.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-03
**Depends on:** [TASK-002]

**Checklist:**
- [x] реализовать BEH-03: Починка, вышедшая за границы заявляемого файла, — отказ
- [x] проверка группы: tests/test_red_autofix_scope.py (kind: integration) зелёные на BEH-03

**Traces to:** [FR-02]

### TASK-004: Неустранимая находка сохраняет сегодняшний класс отказа (+2 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-04, BEH-05, BEH-11.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-04 (—BEH-11)
**Depends on:** [TASK-003]

**Checklist:**
- [x] реализовать BEH-04: Неустранимая находка сохраняет сегодняшний класс отказа
- [x] реализовать BEH-05: Потолок заходов починки объявлен и соблюдается
- [x] реализовать BEH-11: Оператор отличает «красного нет» от «сортировки импортов» по одному сообщению
- [x] проверка группы: tests/test_red_lint_autofix_refusal.py (kind: integration) зелёные на BEH-04, BEH-05, BEH-11

**Traces to:** [FR-04], [FR-06], [FR-01], [FR-09]

### TASK-005: Проект без объявленного линтера ведёт себя ровно как сегодня (+1 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-06, BEH-29.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-06 (—BEH-29)
**Depends on:** [TASK-004]

**Checklist:**
- [x] реализовать BEH-06: Проект без объявленного линтера ведёт себя ровно как сегодня
- [x] реализовать BEH-29: Объявленный линтер без объявленной fix-инвокации не запускает дефолтную
- [x] проверка группы: tests/test_red_lint_scope.py (kind: integration) зелёные на BEH-06, BEH-29

**Traces to:** [FR-05], [FR-01]

### TASK-006: Остаток находок возвращается агенту одним заходом в той же сессии
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-07.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-07
**Depends on:** [TASK-005]

**Checklist:**
- [x] реализовать BEH-07: Остаток находок возвращается агенту одним заходом в той же сессии
- [x] проверка группы: tests/test_red_autofix_agent_round.py (kind: integration) зелёные на BEH-07

**Traces to:** [FR-07], [FR-06]

### TASK-007: Починка, сделавшая тест зелёным, не даёт чекпойнта (+1 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-08, BEH-09.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-08 (—BEH-09)
**Depends on:** [TASK-006]
Закрыто tdd-waiver'ом (red-unverifiable: поведение — эмерджентное свойство
TASK-001–006; spec/.tdd-evidence/waivers/c560727b864370c7/TASK-007.json,
санкция владельца 2026-09-04) + зелёной регрессией по цели checklist'а.

**Checklist:**
- [x] реализовать BEH-08: Починка, сделавшая тест зелёным, не даёт чекпойнта
- [x] реализовать BEH-09: Починка, сломавшая сборку, не даёт чекпойнта
- [x] проверка группы: tests/test_autofix_does_not_whiten_red.py (kind: integration) зелёные на BEH-08, BEH-09

**Traces to:** [FR-08], [FR-03]

### TASK-008: Композитная команда линта имеет объявленное, а не выведенное поведение
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-10.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-10
**Depends on:** [TASK-007]

**Checklist:**
- [x] реализовать BEH-10: Композитная команда линта имеет объявленное, а не выведенное поведение
- [x] проверка группы: tests/test_red_lint_autofix_composite.py (kind: contract) зелёные на BEH-10

**Traces to:** [FR-09], [FR-05]

### TASK-009: Два workstream'а с одинаковым task-id получают непересекающиеся пути (+4 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-12, BEH-13, BEH-20, BEH-21, BEH-22.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-12 (—BEH-22)

**Checklist:**
- [x] реализовать BEH-12: Два workstream'а с одинаковым task-id получают непересекающиеся пути
- [x] реализовать BEH-13: Namespace-сегмент присутствует всегда — слаг при объявленном, дайджест при вычисленном
- [x] реализовать BEH-20: Путь детерминирован и стабилен между попытками и машинами
- [x] реализовать BEH-21: Нормализация не склеивает два разных namespace
- [x] реализовать BEH-22: Путь остаётся читаемым и ограниченной длины
- [x] проверка группы: tests/test_ws_scoped_red_names.py (kind: integration), tests/test_ws_scoped_red_names.py (kind: contract) зелёные на BEH-12, BEH-13, BEH-20, BEH-21, BEH-22

**Traces to:** [FR-10], [FR-14]

### TASK-010: Названный путь проходит discovery того же адаптера (+1 смежных BEH)
P2 | 🔄 IN_PROGRESS   Est: 0.5d

Реализовать сценарии BEH-14, BEH-15.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-14 (—BEH-15)
**Depends on:** [TASK-009]

**Checklist:**
- [ ] реализовать BEH-14: Названный путь проходит discovery того же адаптера
- [ ] реализовать BEH-15: Конвенции pytest и ExUnit сохраняются вместе с сегментом
- [ ] проверка группы: tests/test_tdd_runners.py (kind: contract) зелёные на BEH-14, BEH-15

**Traces to:** [FR-11], [FR-15]

### TASK-011: RED-промпт и харнесс называют один и тот же путь (+1 смежных BEH)
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-16, BEH-19.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-16 (—BEH-19)
**Depends on:** [TASK-010]

**Checklist:**
- [ ] реализовать BEH-16: RED-промпт и харнесс называют один и тот же путь
- [ ] реализовать BEH-19: Отказ писать red в существующий файл не ослаблен
- [ ] проверка группы: tests/test_evidential_file_is_new.py (kind: contract), tests/test_evidential_file_is_new.py (kind: integration) зелёные на BEH-16, BEH-19

**Traces to:** [FR-12], [FR-16]

### TASK-012: Ранее записанные чекпойнты и claim'ы работают без миграции
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-17.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-17
**Depends on:** [TASK-011]

**Checklist:**
- [ ] реализовать BEH-17: Ранее записанные чекпойнты и claim'ы работают без миграции
- [ ] проверка группы: tests/test_ws_scoped_red_names_backcompat.py (kind: integration) зелёные на BEH-17

**Traces to:** [FR-13]

### TASK-013: Уцелевший файл соседнего workstream не блокирует первую задачу
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-18.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-18
**Depends on:** [TASK-012]

**Checklist:**
- [ ] реализовать BEH-18: Уцелевший файл соседнего workstream не блокирует первую задачу
- [ ] проверка группы: tests/test_neighbour_ws_file_does_not_block.py (kind: e2e) зелёные на BEH-18

**Traces to:** [FR-16], [FR-10]

### TASK-014: Стоимость сценария #341 измерена под pytest и сопоставлена с базовой точкой
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-23.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-23
**Depends on:** [TASK-008]

**Checklist:**
- [ ] реализовать BEH-23: Стоимость сценария #341 измерена под pytest и сопоставлена с базовой точкой
- [ ] проверка группы: tests/test_red_autofix_cost.py (kind: e2e) зелёные на BEH-23

**Traces to:** [FR-01], [FR-06]

### TASK-015: Регрессии обоих боевых сценариев не вызывают реального агента
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-24.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-24
**Depends on:** [TASK-014], [TASK-013]

**Checklist:**
- [ ] реализовать BEH-24: Регрессии обоих боевых сценариев не вызывают реального агента
- [ ] проверка группы: tests/test_harness_guards.py (kind: contract) зелёные на BEH-24

**Traces to:** [FR-16], [FR-01]

### TASK-016: Документация и CHANGELOG объявляют новое имя и новое поведение
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-26.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-26
**Depends on:** [TASK-015]

**Checklist:**
- [ ] реализовать BEH-26: Документация и CHANGELOG объявляют новое имя и новое поведение
- [ ] проверка группы: docs/architecture.md (kind: manual) зелёные на BEH-26

**Traces to:** [FR-09], [FR-10]

### TASK-017: Границы существующих гарантий не сдвигаются
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-27.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-27
**Depends on:** [TASK-016]

**Checklist:**
- [ ] реализовать BEH-27: Границы существующих гарантий не сдвигаются
- [ ] проверка группы: tests/test_red_gate.py (kind: contract) зелёные на BEH-27

**Traces to:** [FR-05], [FR-08], [FR-13]

### TASK-018: Отвергнутый после починки red не голодает следующую попытку
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-28.
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-28
**Depends on:** [TASK-008]

**Checklist:**
- [ ] реализовать BEH-28: Отвергнутый после починки red не голодает следующую попытку
- [ ] проверка группы: tests/test_rejected_red_remainder_is_adopted.py (kind: integration) зелёные на BEH-28

**Traces to:** [FR-03], [FR-01]

