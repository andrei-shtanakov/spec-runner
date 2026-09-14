---
schema: discovery-brief
schema_version: 1
spec_stage: discovery
status: approved
approved_by: andrei-shtanakov
approved_at: '2026-09-14'
approver: andrei-shtanakov
owner_role: product
generated_by: discovery-runtime
generated_at: '2026-09-14T04:39:31Z'
validation: pass
interview:
  frame: customer
  sessions:
  - participant_role: product
coverage:
  goals: covered
  personas: covered
  jobs: covered
  functions: covered
  nfr: covered
  constraints: covered
  success_metrics: covered
  out_of_scope: covered
  risks: covered
  gate_passed: true
open_questions: 2
blocking_open_questions: 0
conflicts: 0
traces_to: []
---

# Discovery Brief — andrei-shtanakov/spec-runner (customer-фрейм)

## Goals

- **G-01** MCP-сервер, запущенный для одного scope (project root + change либо spec prefix), читает, запускает и останавливает исключительно этот scope; ни один tool не пересобирает плоский config из CWD.
- **G-02** Запрос stop, поданный после ответа started, никогда не теряется: оператор может положиться на то, что остановка вступит в силу.
- **G-03** MCP как канал управления executor-ом заслуживает доверия: ни один tool не сообщает started или stop_requested без наблюдаемого эффекта в заявленном scope.

## Personas

- **P-01** Оператор spec-runner в Claude Code (владелец репо) — primary; хочет, чтобы каждый MCP tool действовал ровно в том scope, с которым сервер запущен.
- **P-02** Программный вызывающий `mcp_run_server` (fleet-агент devtools, spec-runner-vscode) — вторичная персона; хочет неизменный публичный programmatic-контракт и lazy import.
- **P-03** Соседний репозиторий или namespace на той же машине — страдающая сторона: неверно адресованный executor может править его файлы и тратить бюджет. Спонсор и решатель — владелец репо andrei-shtanakov.

## Jobs-to-be-done

- **J-01** Когда я запускаю MCP-сервер для внешнего проекта с `--change`, я хочу вызвать status/tasks/next_tasks/task_detail/costs/logs и видеть именно этот change, чтобы не рассуждать о чужой спеке.
  traces: [G-01]
- **J-02** Когда я вызываю run_task на этом scope, я хочу, чтобы дочерний executor работал в том же project root и namespace с теми же настройками безопасности, чтобы бюджет и правки легли туда, где я запустил сервер.
  traces: [G-01, G-03]
- **J-03** Когда я вызываю stop сразу после того, как run_task ответил started, я хочу, чтобы работающий executor завершил текущую задачу и вышел, чтобы остановить прогон, запущенный по ошибке.
  traces: [G-02, G-03]

## Functional Requirements

- **FR-01** Все MCP tools (status, tasks, costs, logs, next_tasks, task_detail, run_task, stop) получают config из launch scope сервера (project_root + ровно один namespace: change_id либо spec_prefix), переданного `spec-runner mcp`.
  **Priority**: Must
  **Acceptance**: Сервер запущен с `--project-root <external> --change add-x`: status/tasks читают `<external>/spec/changes/add-x/tasks.md`; в плоском `<external>/spec/` и в CWD сервера не появляется state, lock или stop-файлов.
  traces: [G-01, J-01]
- **FR-02** Tool-level `spec_prefix`, противоречащий launch scope (запуск с `--change` или с другим prefix), отклоняется явной ошибкой, а не переключает namespace.
  **Priority**: Must
  **Acceptance**: `run_task("TASK-001", spec_prefix="other-")` на сервере с `--change add-x` возвращает status error с именем конфликта; дочерний процесс не запускается.
  traces: [G-01, J-01]
- **FR-03** run_task передаёт дочернему executor точный scope: project_root, ровно один namespace (change_id либо spec_prefix), effective safety-настройки (governance strictness, branch/commit/review, tests/lint, integration PR, HITL) и лимиты исполнения (retries, timeout, бюджеты); дочерний процесс запускается с cwd = project_root.
  **Priority**: Must
  **Acceptance**: В E2E effective config дочернего executor совпадает с родительским по всем перечисленным полям; state/lock/log дочернего процесса лежат под `<external>/spec/changes/add-x/`.
  traces: [G-01, J-02]
- **FR-04** Если resolved parent-config невозможно воспроизвести для child, run_task отказывает до платного вызова — никаких молчаливых defaults.
  **Priority**: Must
  **Acceptance**: Поле effective config без представления в команде child даёт status error и ни одного запущенного subprocess.
  traces: [G-01, G-03, J-02]
- **FR-05** run_task возвращает started только после того, как child получил executor lock, очистил только старый stop-marker и опубликовал ready; stop, вызванный после ответа started, не может быть стёрт startup-кодом.
  **Priority**: Must
  **Acceptance**: E2E на настоящем дочернем CLI: run_task → started → stop → child видит тот же `<external>/spec/changes/add-x/.executor-stop` и завершается, не начиная следующую задачу; немедленный stop не теряется.
  traces: [G-02, G-03, J-03]
- **FR-06** Если child завершился, не получил lock или не достиг ready, run_task возвращает error, а не ложный started; занятый lock даёт ошибку запуска.
  **Priority**: Must
  **Acceptance**: При lock, удерживаемом другим процессом, run_task возвращает status error и второй executor не запускается.
  traces: [G-02, G-03, J-03]
- **FR-07** `mcp_run_server()` без аргументов продолжает работать по прежнему programmatic-контракту (config из текущей директории), lazy import `mcp_server` сохраняется.
  **Priority**: Should
  **Acceptance**: Существующие тесты `test_lazy_mcp_import` и programmatic-вызов без аргументов проходят без изменений.
  traces: [G-01, J-02]
- **FR-08** Контракт синхронизирован: README описывает привязку MCP-сервера к launch scope, CHANGELOG содержит запись, issue #485 закрыт с evidence.
  **Priority**: Should
  **Acceptance**: В PR присутствуют раздел README и строка CHANGELOG; #485 закрыт ссылкой на PR.
  traces: [G-03, J-01]
- **FR-09** status и task_detail показывают launch scope сервера (project_root, namespace), чтобы оператор мог проверить, какой scope обслуживается.
  **Priority**: Could
  **Acceptance**: JSON-ответ status содержит поля project_root и namespace, совпадающие с параметрами запуска.
  traces: [G-03, J-01]

## Non-Functional

- **NFR-01** Ни один tool не пишет state, lock или stop-файлы вне spec-директории launch scope.
  **Category**: safety
  **Acceptance**: После E2E-прогона в плоском `<external>/spec/` и в CWD сервера нет ни одного файла `.executor-*`.
  traces: [G-01]
- **NFR-02** E2E и unit-тесты не вызывают платного агента и укладываются в CI-бюджет времени.
  **Category**: cost
  **Target**: Дочерний executor в тестах использует детерминированный fake command; E2E-сценарий завершается быстрее 60 секунд.
  traces: [G-03]
- **NFR-03** Stop после started не теряется даже при немедленном вызове.
  **Category**: reliability
  **Target**: Отдельный soak-тест (маркер slow) повторяет E2E run_task → started → stop 20 раз подряд без единого потерянного stop-запроса; базовый E2E в CI — одна итерация.
  traces: [G-02]

## Constraints

- **CON-01** Публичный `mcp_run_server` и lazy import `mcp_server` из `spec_runner` обязаны сохраниться.
- **CON-02** Работа доставляется через governance репо: `spec_governance: strict`, `execution_mode: tdd`, `review_policy: required`, integration PR — как задано в `spec-runner.config.yaml`.
- **CON-03** `--change` и `--spec-prefix` взаимоисключающие (существующий CLI-контракт); child получает ровно один namespace.
- **CON-04** Форма решения задана владельцем: единый launch-scope holder для всех tools; один типизированный serializer/command-builder команды child; startup-handshake (lock → очистка только старого stop-marker → ready) до ответа started.

## Success Metrics

- **M-01** E2E-сценарий #485 run_task → started → stop на внешнем project root с `--change` в CI.
  **Target**: baseline: stop теряется / executor в чужом namespace → target: зелёный E2E в CI на каждом PR
  traces: [G-01, G-02]
- **M-02** Число MCP tools, пересобирающих плоский config из CWD.
  **Target**: baseline: 7 из 8 → target: 0 из 8
  traces: [G-01]

## Out of Scope

- **OUT-01** Семантика остановки не меняется за пределами стартового handshake: stop — файл-маркер, executor не убивается.
- **OUT-02** Сетевой транспорт и аутентификация MCP не вводятся; stdio-only остаётся.
- **OUT-03** Argv-контракт spec-runner-vscode и жизненный цикл команды `change` не меняются.
- **OUT-04** Новые write-tools MCP (кроме существующих run_task и stop) в эту итерацию не входят.

## Risks

- **RK-01** Окно ожидания ready в handshake может давать флаки на медленном CI.
- **RK-02** Сериализация effective config в команду child может быть неполной; тогда отказ FR-04 обязан ловить каждое непредставимое поле.

## Open Questions

- **Q-01** Каким механизмом child публикует ready (файл-маркер, pipe, метаданные lock)?
  owner_role: architect
  blocking: false
- **Q-02** Нужна ли обратная сверка effective config child родителем (чтение записанного child-ом конфига), а не только доверие сформированной команде?
  owner_role: architect
  blocking: false