---
traces_to:
- decomposition
upstream_hashes:
  decomposition: 8c3f26c25a1a8c6bac39ee838e86920c9df5d85c
spec_stage: tasks
status: approved
version: 2
generated_by: fleet-agent
generated_at: '2026-09-14T11:23:20+04:00'
source_prompt_version: ''
validation: pass
approved_by: andrei-shtanakov
approved_at: '2026-09-14T07:33:04Z'
owner_role: stream-owner
---

## Milestone 1: MCP launch scope (spec-runner#485)

Сгенерировано task_bridge из behaviour-spec бандла mcp-launch-scope-spec-runner-485-20260914 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Draft: исполнение только после человеческого approve.

## Решения открытых вопросов (уровень design)

**Q-02 — resolved:**

**Канал ready — отдельный файл `<spec_dir>/.executor-ready`, не поле lock.
Child, не опубликовавший ready до таймаута, завершается родителем.**

Файл, а не lock, по трём причинам, каждая из которых лежит в существующем коде.
(1) Lock-файл `ExecutorLock` — не JSON, как предполагал upstream, а две
текстовые строки `PID:`/`Started:`, которые держатель пишет в открытый под
`flock` дескриптор (`config.py:188–192`); добавить туда «поле ready» — значит
переписывать файл под блокировкой из процесса-держателя и учить родителя
парсить файл, который держатель вправе усечь. (2) Lock берётся в `cmd_run`
(`cli.py:214`) **до** governance-гейта, dirty-spec-гейта, гейта tracked-state и
до `clear_stop_file` (`cli.py:831`) — «lock существует» не означает «старый
marker стёрт», а ready по определению §3 означает именно это; отдельный файл,
записанный последним шагом старта, несёт ровно нужный смысл. (3) Файл лежит
рядом со `.executor-stop`, наблюдается теми же средствами (BEH-18, третий And),
уже покрыт правилом `spec/` корневого `.gitignore` репо и glob-ом
`.executor-*` из `RUNTIME_GITIGNORE_ENTRIES` (`git_ops.py:193`), которым
spec-runner дополняет ignore-файлы spec-директорий (tracked `spec/.gitignore`
в дереве нет) и входит в тот же инвентарь runtime-файлов (NFR-01). Это
«существующий механизм», а не новый IPC — граница OUT (requirements §7).

Судьба child при таймауте — **завершение родителем** (SIGTERM, ограниченное
ожидание, затем SIGKILL), с `pid` и `terminated: true` в ответе. Требования
допускали оба варианта (FR-06, BEH-22); выбор диктует G-03: tool, ответивший
`status: error`, но оставивший живой executor тратить бюджет в namespace —
тот же класс дефекта, что потерянный stop. SIGTERM уже означает для executor-а
graceful shutdown (`executor._signal_handler`), так что завершение не вводит
новой семантики остановки (OUT-01). Побочная выгода — lock освобождается, и
следующий `run_task` не упирается в призрака.

**Q-03 — resolved:**

**Обратной сверки effective config child родителем в runtime нет; её место
занимает проверка воспроизводимости до `Popen`, а доказательство совпадения —
E2E BEH-09.**

FR-04 требует отказа **до** запуска child, если parent-config невоспроизводим.
Единственный способ ответить на этот вопрос честно — построить в процессе
родителя тот config, который child получит из тех же входов (YAML по
`project_root` + argv serializer-а через тот же `_build_parser()` и
`build_config`), и сравнить с родительским по всем полям (§ Механика 2.2).
После такой симуляции runtime-эхо от child проверяло бы уже проверенное: те же
YAML, тот же парсер, тот же интерпретатор (CON-05). Что симуляция *не* видит —
изменение YAML между проверкой и стартом child и расхождение окружений — E2E
BEH-09 покрывает против настоящего child, а первое к тому же лежит в поле
допущений любого CLI-запуска. Цена эха — ready-файл превращается в дамп config
с собственным форматом сравнения (Path, dict, personas), плюс ещё одна
error-ветка FR-06 «config mismatch», которую требования вводили условно. Scope
при этом доказывается позиционно: ready-файл читается по пути `spec_dir`
launch namespace и принимается только с `PID`, равным `proc.pid` — child,
взявший lock в чужом namespace, ready в нужном месте не оставит.


## Критерии приёмки (уровень acceptance)

- **AC-01** (test): Все восемь tools обслуживают launch scope, а не CWD сервера
- **AC-02** (test): Чтение не оставляет следов ни в scope, ни вне его
- **AC-03** (test): Противоречащий prefix отклоняется по имени и ничего не запускает
- **AC-04** (test): Совпадающий, пустой и уточняющий prefix принимаются
- **AC-05** (test): Child живёт в `project_root`, в одном namespace, с тем же effective config
- **AC-06** (test): Перечень serializer-а и парсера `common` не расходятся
- **AC-07** (test): Child запускается entry point-ом текущего окружения
- **AC-08** (test): Вывод child уходит в лог namespace, многословный child не зависает
- **AC-09** (test): Невоспроизводимый config — отказ до `Popen`, воспроизводимый — ровно один запуск
- **AC-10** (test): `started` приходит не раньше lock и ready; старый marker стёрт, новый — нет
- **AC-11** (test): Stop сразу после `started` не теряется
- **AC-12** (test): Таймаут ожидания ready объявлен и конфигурируем
- **AC-13** (test): Гарантия «stop после started» держится статистически
- **AC-14** (test): Занятый lock, ранний выход и молчащий child дают `status: error`, не `started`
- **AC-15** (test): Ни один tool не пишет вне spec-директории launch scope, включая отказные ветки
- **AC-16** (test): Тесты не вызывают платного агента и базовый E2E входит в `not slow`
- **AC-17** (metric): Базовый E2E укладывается в порог NFR-02
- **AC-18** (metric): Число tools, пересобирающих config из CWD, достигло target M-02
- **AC-19** (test): `mcp_run_server()` без аргументов и lazy import работают как прежде
- **AC-20** (test): `status` и `task_detail` называют обслуживаемый scope
- **AC-21** (test): Существующие MCP-тесты меняют ожидания только по контракту
- **AC-22** (manual): README, CHANGELOG и #485 говорят то же, что код

### TASK-001: Launch scope: holder, предикат, YAML по `project_root`, родительская сторона handshake
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01, BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07, BEH-18, BEH-20, BEH-21, BEH-22, BEH-25, BEH-28 (DT-01, группа core).
Source: workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/30-decomposition.md#DT-01

**Checklist:**
- [x] реализовать BEH-01: Все восемь tools обслуживают launch scope, а не CWD сервера
- [x] реализовать BEH-02: YAML ищется по `project_root` launch scope, а не по CWD процесса
- [x] реализовать BEH-03: Чтение не оставляет следов ни в scope, ни вне его
- [x] реализовать BEH-04: Ни один tool не пересобирает config из CWD (M-02: 7/8 → 0/8)
- [x] реализовать BEH-05: `run_task` с противоречащим prefix отказывает по имени и ничего не запускает
- [x] реализовать BEH-06: Предикат противоречия один и тот же для всех восьми tools
- [x] реализовать BEH-07: Совпадающий, пустой и уточняющий prefix — не противоречие
- [x] реализовать BEH-18: Таймаут ожидания ready объявлен и конфигурируем
- [x] реализовать BEH-20: Занятый lock — ошибка запуска, второй executor не запущен
- [x] реализовать BEH-21: Child, умерший до ready, — ошибка с кодом выхода и хвостом лога
- [x] реализовать BEH-22: Child, не опубликовавший ready, — `timeout`, и ready-файла после ошибки нет
- [x] реализовать BEH-25: `status` и `task_detail` называют обслуживаемый scope
- [x] реализовать BEH-28: Существующие MCP-тесты меняют ожидания только там, где этого требует контракт
- [x] проверка группы: tests/test_mcp_launch_scope.py (kind: integration), tests/test_mcp_launch_scope.py (kind: contract), tests/test_mcp.py (kind: contract) зелёные на BEH-01, BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07, BEH-18, BEH-20, BEH-21, BEH-22, BEH-25, BEH-28

**Traces to:** [FR-01], [FR-02], [FR-05], [FR-06], [FR-09]

### TASK-002: Serializer effective config и проверка воспроизводимости до `Popen`
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-10, BEH-13, BEH-14 (DT-02, группа core).
Source: workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/30-decomposition.md#DT-02
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-10: Перечень serializer-а и `common`-парсера не расходятся
- [x] реализовать BEH-13: Override непредставимого поля — отказ до `Popen`, называющий поле
- [x] реализовать BEH-14: Полностью воспроизводимый config запускает child ровно один раз
- [x] проверка группы: tests/test_mcp_serializer.py (kind: contract) зелёные на BEH-10, BEH-13, BEH-14

**Traces to:** [FR-03], [FR-04]

### TASK-003: Programmatic-контракт `mcp_run_server()` и lazy import не сдвинулись
P2 | TODO   Est: 0.5d

Проверить сценарии BEH-23 (DT-03, группа solo).
Source: workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/30-decomposition.md#DT-03
**Mode:** verify_first
**Verifies:** tests/test_lazy_mcp_import.py, tests/test_mcp_launch_scope.py, tests/test_mcp.py
**Depends on:** [TASK-001]

**Checklist:**
- [ ] проверить BEH-23: `mcp_run_server()` без аргументов работает как прежде
- [ ] проверка группы: tests/test_lazy_mcp_import.py (kind: contract) зелёные на BEH-23

**Traces to:** [FR-07]

### TASK-004: Child публикует ready; E2E scope, effective config, handshake и стоп на живом child; soak; документация
P2 | 🔍 REVIEW   Est: 0.5d

Реализовать сценарии BEH-08, BEH-09, BEH-11, BEH-12, BEH-15, BEH-16, BEH-17, BEH-19, BEH-24, BEH-26, BEH-27 (DT-04, группа core).
Source: workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/30-decomposition.md#DT-04
**Depends on:** [TASK-002]

**Checklist:**
- [x] реализовать BEH-08: Child живёт в `project_root` и ровно в одном namespace
- [x] реализовать BEH-09: Effective config child равен родительскому по всем представимым полям
- [x] реализовать BEH-11: Child запускается entry point-ом текущего окружения, а не `spec-runner` из PATH
- [x] реализовать BEH-12: Вывод child уходит в лог namespace, и многословный child не зависает
- [x] реализовать BEH-15: `started` приходит не раньше lock и ready
- [x] реализовать BEH-16: Stop сразу после `started` не теряется
- [x] реализовать BEH-17: Marker до `run_task` стирается, marker после `started` — нет
- [x] реализовать BEH-19: Гарантия «stop после started» держится статистически (soak ×20)
- [x] реализовать BEH-24: README, CHANGELOG и #485 говорят то же, что код
- [x] реализовать BEH-26: Ни один tool не пишет вне spec-директории launch scope, включая ошибочные ветки
- [x] реализовать BEH-27: Тесты не вызывают платного агента и укладываются в CI-бюджет
- [x] проверка группы: tests/test_mcp_e2e_485.py (kind: e2e), README.md (kind: manual) зелёные на BEH-08, BEH-09, BEH-11, BEH-12, BEH-15, BEH-16, BEH-17, BEH-19, BEH-24, BEH-26, BEH-27

**Traces to:** [FR-03], [FR-05], [FR-08], [FR-01], [FR-04], [FR-06]

