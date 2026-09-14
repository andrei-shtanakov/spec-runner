---
spec_stage: charter
status: draft
owner_role: product
traces_to:
- discovery-brief
- discovery-customer
upstream_hashes:
  discovery-brief: 39e8c625f65812f9530876e5d52695015d90d0ae
  discovery-customer: 4d7900d7dc9f3271326594647f21a7b7d54ab676
---

# Charter — MCP launch scope (spec-runner#485): один scope на сервер, один config на всех tools

`spec-runner mcp` запускается для одного scope — project root плюс ровно один
namespace (`--change <id>` либо `--spec-prefix <p>`) — и обязан **читать,
запускать и останавливать только его**. Сегодня это верно для одного tool из
восьми. После узкого исправления #481/#484 `stop` видит resolved launch config
через модульную глобаль `_launch_stop_config` (`mcp_server.py:19`, `:230`);
остальные семь tools вызывают `_build_config(spec_prefix)` (`:22`), который
читает YAML **относительно CWD сервера** и собирает `Namespace(project_root="")`
— то есть плоский config чужой директории. `run_task` при этом спавнит
`["spec-runner", "run", "--task", id]` без `cwd`, без `--project-root`, без
`--change` (`:206`) и отвечает `started` в момент `Popen`, до того как child
взял lock и очистил старый stop-marker.

Наблюдаемый сценарий из #485: `spec-runner mcp --change add-x` →
`run_task("TASK-001")` запускает executor в **default namespace** →
`stop()` пишет per-change marker → запущенный executor его не видит.
Немедленный `stop` после `started` теряется и в правильном namespace: child
стартует с `clear_stop_file` (`cli.py:831`) и стирает marker, записанный между
`Popen` и своим стартом.

Workstream закрывает разрыв целиком: **один типизированный launch-scope holder
для всех восьми tools, один serializer effective config в команду child,
startup-handshake до ответа `started`** — форма решения задана владельцем
(customer CON-04).

## Контекст и проблема

### Как это устроено сегодня

- **Launch-контекст есть только у `stop`.** `run_server(config)` выставляет
  `_launch_stop_config` (`mcp_server.py:284`–`:293`); её читает единственный
  tool. `status`, `tasks`, `costs`, `logs`, `next_tasks`, `task_detail`,
  `run_task` пересобирают config через `_build_config` — 7 из 8 (customer
  M-02 baseline).
- **YAML ищется по CWD, не по `project_root`.** `_resolve_config_path`
  (`config.py:836`) проверяет модульные константы `CONFIG_FILE` /
  `LEGACY_CONFIG_FILE` (`:241`–`:242`) относительно текущей директории
  процесса. Родитель, запущенный с `--project-root` из чужой директории,
  читает чужой YAML или дефолты — в том числе `spec_governance: off` там, где
  целевой репо strict, и `spec_run_gate_ok` пропускает (engineer CON-03,
  RK-02). Это tacit-ловушка, а не документированное поведение.
- **Child наследует не scope, а PATH и CWD родителя.** Spawn в `run_task`
  переносит только `--spec-prefix`; `project_root`, `change_id`, поле
  `spec_governance` (у него есть только run-only флаги `--strict`/`--no-strict`
  subparser-а `run`, `cli.py:1934`–`:1942`, которых `run_task` не передаёт) и
  все поля без CLI-флага вовсе (`review_policy`, `execution_mode`,
  `harness_guard`, `commands`) child получает из того YAML, который найдёт по
  своему CWD — то есть по CWD сервера (engineer IF-02, IF-05).
- **`started` — до lock и до ready.** Порядок в `cmd_run`: `_acquire_run_lock`
  → гейты → `clear_stop_file` (`cli.py:831`) → открытие `ExecutorState`.
  Родитель отвечает `started` сразу после `Popen`, и stop, поданный в это
  окно, стирается стартовым `clear_stop_file` (engineer S-05). Занятый lock
  завершает child с exit 1 — а родитель уже сообщил `started` (customer
  FR-06).
- **`Popen(stdout=PIPE, stderr=PIPE)` никто не читает.** Разговорчивый child
  блокируется на записи после заполнения буфера (engineer RK-01). Нигде не
  зафиксировано; E2E с многословным fake это вскроет.

### Что уже есть и не нужно изобретать

- **Namespace-инфраструктура готова.** `ExecutorConfig.__post_init__`
  резолвит `project_root` и namespace-ит `state_file`/`logs_dir`/`stop_file`
  по `spec_prefix` либо `change_id`; взаимоисключение проверяется дважды
  (`SystemExit` в CLI, `ConfigError` в dataclass) — child по построению
  получает ровно один namespace (engineer S-02, CON-04).
- **Общий парсер `common` с гвардом дрейфа.** `_COMMON_DEFAULTS` + assert
  при импорте (engineer S-04) — готовое зеркало для serializer: перечень
  представимых флагов уже существует и уже охраняется.
- **Lock per-namespace с диагностикой.** `ExecutorLock` пишет две текстовые
  строки `PID:`/`Started:` под `flock` (`config.py:188`–`:192`) в
  `state_file.with_suffix(".lock")` (под change —
  `spec/changes/<id>/.executor-state.lock`), `--force` обходит, занятый lock → exit 1
  с известным сообщением (engineer IF-04).
- **Stop-marker — устоявшийся контракт.** `<spec_dir>/.executor-stop`,
  писатели `cmd_stop`/MCP `stop`/`touch`, читатель — цикл `run` между
  задачами (engineer IF-03). Handshake должен встать **рядом** с ним, не
  вместо него.
- **Тестовый пояс против платного агента.** conftest поднимает
  `PaidBinaryReached` на argv известного агента; fake CLI
  `tests/fixtures/fake_claude.sh`; `tests/test_mcp.py::TestMCPStop` уже несёт
  launch-namespace тесты из #484 (engineer CON-01, S-07).
- **Programmatic-контракт под охраной.** `spec_runner.mcp_run_server`
  экспортируется лениво через `__getattr__`, `test_lazy_mcp_import` ловит
  регресс (engineer IF-06, S-08).

## Цель

Сделать launch scope MCP-сервера **единственным источником config для всех
tools и для child-executor**, а ответ `started` — **утверждением о
наблюдаемом эффекте**: child взял lock в заявленном namespace, очистил только
старый stop-marker и опубликовал ready. Всё, что нельзя воспроизвести
(противоречащий `spec_prefix`, непредставимое поле effective config, занятый
lock, child, умерший до ready) — явный отказ до платного вызова, не молчаливый
дефолт.

## Пользователи и ожидаемая ценность

- **P-01 Оператор spec-runner в Claude Code** (владелец репо) — primary.
  Каждый MCP tool действует ровно в том scope, с которым сервер запущен;
  `status` показывает, какой scope обслуживается; `stop` после `started`
  гарантированно доходит.
- **P-02 Программный вызывающий `mcp_run_server`** (fleet-агент devtools,
  spec-runner-vscode). Публичный контракт `mcp_run_server(config=None)` и lazy
  import не меняются; вызов без аргументов работает как прежде.
- **P-03 Соседний репозиторий или namespace на той же машине** — страдающая
  сторона. Неверно адресованный executor сегодня может править чужие файлы и
  тратить чужой бюджет; после workstream ни один tool не пишет `.executor-*`
  вне spec-директории launch scope.

## Требования (из customer-брифа, ID неизменны)

### Функциональные

- **FR-01** · Must — Все MCP tools (status, tasks, costs, logs, next_tasks,
  task_detail, run_task, stop) получают config из launch scope сервера
  (project_root + ровно один namespace: change_id либо spec_prefix),
  переданного `spec-runner mcp`. *Приёмка:* сервер с `--project-root
  <external> --change add-x`: status/tasks читают
  `<external>/spec/changes/add-x/tasks.md`; в плоском `<external>/spec/` и в
  CWD сервера не появляется state, lock или stop-файлов.
- **FR-02** · Must — Tool-level `spec_prefix`, противоречащий launch scope
  (запуск с `--change` или с другим prefix), отклоняется явной ошибкой, а не
  переключает namespace. *Приёмка:* `run_task("TASK-001",
  spec_prefix="other-")` на сервере с `--change add-x` → status error с именем
  конфликта; дочерний процесс не запускается.
- **FR-03** · Must — run_task передаёт child точный scope: project_root, ровно
  один namespace, effective safety-настройки (governance strictness,
  branch/commit/review, tests/lint, integration PR, HITL) и лимиты (retries,
  timeout, бюджеты); child запускается с `cwd = project_root`. *Приёмка:* в
  E2E effective config child совпадает с родительским по всем перечисленным
  полям; state/lock/log child лежат под `<external>/spec/changes/add-x/`.
- **FR-04** · Must — Если resolved parent-config невозможно воспроизвести для
  child, run_task отказывает до платного вызова — никаких молчаливых defaults.
  *Приёмка:* поле effective config без представления в команде child → status
  error и ни одного запущенного subprocess.
- **FR-05** · Must — run_task возвращает `started` только после того, как child
  получил executor lock, очистил только старый stop-marker и опубликовал ready;
  stop, вызванный после `started`, не может быть стёрт startup-кодом.
  *Приёмка:* E2E на настоящем дочернем CLI: run_task → started → stop → child
  видит тот же `<external>/spec/changes/add-x/.executor-stop`, и marker,
  записанный после `started`, не стёрт ни одной веткой child (child `run
  --task` исполняет ровно одну задачу — «следующей» у него нет по построению,
  единственная проверка marker-а стоит до задачи, `cli.py:1273`; наблюдаемое
  уточняют requirements FR-05); немедленный stop не теряется.
- **FR-06** · Must — Если child завершился, не получил lock или не достиг
  ready, run_task возвращает error, а не ложный `started`; занятый lock даёт
  ошибку запуска. *Приёмка:* при lock, удерживаемом другим процессом, run_task
  → status error и второй executor не запускается.
- **FR-07** · Should — `mcp_run_server()` без аргументов работает по прежнему
  programmatic-контракту (config из текущей директории), lazy import
  `mcp_server` сохраняется. *Приёмка:* `test_lazy_mcp_import` и
  programmatic-вызов без аргументов проходят без изменений.
- **FR-08** · Should — Контракт синхронизирован: README описывает привязку
  MCP-сервера к launch scope, CHANGELOG содержит запись, issue #485 закрыт с
  evidence. *Приёмка:* в PR есть раздел README и строка CHANGELOG; #485 закрыт
  ссылкой на PR.
- **FR-09** · Could — status и task_detail показывают launch scope сервера
  (project_root, namespace). *Приёмка:* JSON-ответ status содержит поля
  `project_root` и `namespace`, совпадающие с параметрами запуска.

### Нефункциональные

- **NFR-01** · safety — Ни один tool не пишет state, lock или stop-файлы вне
  spec-директории launch scope. *Приёмка:* после E2E в плоском
  `<external>/spec/` и в CWD сервера нет ни одного файла `.executor-*`.
- **NFR-02** · cost — E2E и unit-тесты не вызывают платного агента и
  укладываются в CI-бюджет времени. *Цель:* child в тестах использует
  детерминированный fake command; E2E-сценарий завершается быстрее 60 секунд.
- **NFR-03** · reliability — Stop после `started` не теряется даже при
  немедленном вызове. *Цель:* отдельный soak-тест (маркер slow) повторяет
  run_task → started → stop 20 раз подряд без единого потерянного stop; базовый
  E2E в CI — одна итерация.

## Объём

- **Единый launch-scope holder** (engineer AP-01): типизированный объект с
  resolved `ExecutorConfig` вместо `_launch_stop_config`; все 8 tools берут
  config через один helper с одним предикатом противоречия; `run_server(None)`
  строит config как прежде. Покрывает FR-01, FR-02, FR-07.
- **Serializer `ExecutorConfig → argv`** для child (engineer AP-02):
  перечисляет представимые поля (`--project-root`, `--change` |
  `--spec-prefix`, `--max-retries`, `--timeout`, `--no-*`,
  `--integration-pr`, `--hitl-review`, `--budget`, `--task-budget`,
  `--callback-url`, `--log-level`); поля без представления воспроизводятся
  тем, что child читает **тот же YAML** с `cwd=project_root`; override
  непредставимого поля у родителя → отказ до spawn. Покрывает FR-03, FR-04.
- **Поиск YAML по `project_root`** — входит в объём как условие AP-02
  (engineer CON-03): родитель обязан читать тот же файл, что и child, иначе
  «тот же YAML» недоказуем.
- **Startup-handshake** (engineer AP-03): child после `_acquire_run_lock` и
  `clear_stop_file` публикует ready; родитель ждёт ready с таймаутом,
  параллельно наблюдая `proc.poll()`, и только тогда отвечает `started`;
  child, вышедший до ready (в т.ч. exit 1 на занятом lock), → error. Покрывает
  FR-05, FR-06.
- **stdout/stderr child** — в лог-файл под `config.logs_dir` (или DEVNULL),
  не `PIPE` (engineer AP-04).
- **Видимость scope**: поля `project_root`/`namespace` в ответах status и
  task_detail (FR-09).
- **Документация и закрытие**: README §MCP Server (привязка к launch scope,
  отказ на противоречащем prefix — видимое изменение контракта IF-01),
  CHANGELOG, #485 закрыт с evidence (FR-08).
- **Тесты**: E2E на настоящем CLI entry point из тестового окружения
  (`python -m spec_runner` либо console-script uv-venv, не `spec-runner` из
  PATH оператора — engineer CON-05) с fake command; soak-тест `slow` ×20
  (NFR-03); unit на holder, serializer, handshake.

## Вне объёма

- **OUT-01** Семантика остановки за пределами стартового handshake: stop —
  файл-маркер, executor не убивается; три точки потребления marker-а в
  `run`/`watch`/`retry` (`cli.py:831`, `:1055`, `:1274`) не переписываются
  (engineer RK-04).
- **OUT-02** Сетевой транспорт и аутентификация MCP; stdio-only остаётся.
- **OUT-03** Argv-контракт spec-runner-vscode и жизненный цикл команды
  `change`.
- **OUT-04** Новые write-tools MCP помимо существующих run_task и stop.
- **Новый IPC.** Ready публикуется существующими механизмами (файл в
  namespace или поле lock) — не сокет, не pipe-протокол.
- **Правка соседних репо** (devtools, spec-runner-vscode): всё, что им
  требуется от нового контракта, уходит issue/handoff'ом.

## Продуктовые инварианты

1. **Один scope на сервер, один источник config.** Ни один tool не
   пересобирает config из CWD; число таких tools — 0 из 8 (M-02).
2. **Противоречие — отказ, не переключение.** Tool-level `spec_prefix`,
   расходящийся с launch scope, называется по имени и отклоняется; namespace
   никогда не меняется молча.
3. **`started` — утверждение о факте.** Ответ `started` означает: child
   держит lock в заявленном namespace, старый stop-marker стёрт, ready
   опубликован. Ответ `stop_requested` означает: marker лежит в spec-директории
   launch scope. Ни один tool не сообщает об эффекте, которого нет в заявленном
   scope (G-03).
4. **Stop после `started` не теряется.** Стартовый код child не может стереть
   marker, записанный после ответа `started` (G-02).
5. **Fail-closed вместо fallback** (engineer AP-05, прецеденты #64, #129,
   #337, #484): непредставимое поле, ненайденный YAML по `project_root`, child
   без ready, занятый lock — явная ошибка до платного вызова, не дефолт.
6. **Child — зеркало родителя, а не догадка о нём.** Effective config child
   совпадает с родительским по перечисленным serializer-ом полям; сравнивать в
   приёмке resolved `ExecutorConfig`, а не строку команды (engineer RK-03).
7. **Programmatic-контракт неизменен.** `spec_runner.mcp_run_server(config=None)`
   и lazy import сохраняются байт-в-байт по поведению (CON-01).
8. **Обратная совместимость для плоского запуска.** Сервер, запущенный из
   project root без namespace, обслуживает его как сегодня.

## Критерии приёмки

- E2E #485 в CI: сервер с `--project-root <external> --change add-x` →
  `run_task("TASK-001")` → `started` → `stop()` → child видит
  `<external>/spec/changes/add-x/.executor-stop`, и marker после `started`
  не стёрт стартовым кодом (single-task режим, FR-05); state/lock/log child — под `<external>/spec/changes/add-x/`;
  ни одного `.executor-*` в плоском `<external>/spec/` и в CWD сервера (M-01,
  FR-01, FR-03, FR-05, NFR-01).
- `run_task("TASK-001", spec_prefix="other-")` на сервере с `--change add-x`
  → status error, называющий конфликт; `Popen` не вызван (FR-02).
- Тест-двойник поля effective config без argv-представления → status error и
  ни одного subprocess (FR-04).
- Lock, удерживаемый другим процессом, → status error; второй executor не
  запущен; `started` не возвращён (FR-06).
- `grep _build_config` по tools даёт 0 вызовов, минующих holder; статический
  или тестовый факт, что все 8 tools проходят через один helper (M-02).
- `test_lazy_mcp_import` и вызов `mcp_run_server()` без аргументов проходят
  без правок тестов (FR-07).
- Soak ×20 run_task → started → stop под маркером `slow` — ноль потерянных
  stop (NFR-03); базовый E2E < 60 с, без `PaidBinaryReached` (NFR-02).
- Ответ `status` содержит `project_root` и `namespace`, равные параметрам
  запуска (FR-09).
- README §MCP Server описывает привязку к launch scope и отказ на
  противоречащем prefix; CHANGELOG содержит запись; #485 закрыт ссылкой на PR
  (FR-08).
- Существующие `TestMCPStop` (#484), `TestMCPRunTask`, `test_mcp_v2_wire`
  проходят без изменения ожиданий, кроме тех, что прямо следуют из FR-02/FR-05.

## Риски и меры

- **Флаки окна ready на медленном CI** (customer RK-01). *Мера:* родитель
  ждёт ready **и** `proc.poll()` одновременно — детерминированный исход на
  смерти child; таймаут конфигурируем; soak-тест — `slow`, базовый E2E — одна
  итерация.
- **Неполная сериализация effective config** (customer RK-02, engineer RK-03).
  *Мера:* serializer перечисляет поля явно и зеркалит `_COMMON_DEFAULTS`;
  приёмка FR-03 сравнивает resolved `ExecutorConfig` child и parent, а не
  argv; непредставимый override — отказ (FR-04).
- **Родитель читает чужой YAML** (engineer CON-03, RK-02). *Мера:* поиск YAML
  по `project_root` — в объёме; без него «тот же YAML у child» не доказуем.
- **Недренируемый PIPE блокирует child** (engineer RK-01). *Мера:* AP-04 —
  stdout/stderr в лог под `logs_dir`; E2E с многословным fake.
- **Расползание объёма на семантику stop** (engineer RK-04). *Мера:* OUT-01 —
  handshake встаёт после существующего `clear_stop_file`, три точки
  потребления не трогаются.
- **Рассинхрон флага serializer с `common`** (engineer RK-05). *Мера:*
  serializer строится от того же перечня, что `_COMMON_DEFAULTS`; новый флаг
  без парсера ловится гвардом при импорте — тест на паритет перечней.
- **Видимое изменение контракта IF-01** (отказ на противоречащем prefix).
  *Мера:* README до мержа (FR-08); поведение при плоском запуске сохраняется
  (инвариант 8).

## Открытые вопросы

- **Q-A · owner_role: product · blocking: false** (engineer Q-01). Сервер
  запущен без namespace (плоский `spec/`): остаётся ли tool-level
  `spec_prefix` разрешённым выбором prefix-namespace внутри launch
  project_root (противоречия нет) или отклоняется тоже? *Предложение:*
  разрешён, через тот же holder — это не противоречие scope, а его уточнение.
- **Q-B · owner_role: architect · blocking: false** (customer Q-01, engineer
  Q-02). Канал ready: файл `<spec_dir>/.executor-ready` или поле в текстовом
  lock-файле `ExecutorLock`? *Предложение:* файл в namespace — наблюдаем в E2E, не
  меняет формат lock.
- **Q-C · owner_role: architect · blocking: false** (customer Q-02, engineer
  Q-03). Обратная сверка effective config child родителем: child записывает
  resolved поля в ready-marker, родитель сравнивает перечисленные serializer-ом
  поля и отказывает при расхождении — или доверие сформированной команде
  достаточно? *Предложение:* сверка — это и есть тест FR-03, вынесенный в
  runtime.

## Условие завершения

Workstream закончен, когда E2E #485 (внешний `--project-root` + `--change`,
run_task → started → stop, настоящий CLI entry point, fake command) зелёный в
CI на каждом PR; число tools, пересобирающих config из CWD, равно нулю;
инварианты 1–8 подтверждены тестами из критериев приёмки; README и CHANGELOG
описывают привязку к launch scope; #485 закрыт ссылкой на PR с evidence;
контракт объявлен соседям (devtools, spec-runner-vscode) issue/handoff'ом без
правки их файлов.

## Источники

- `workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/00-discovery/discovery-brief-customer.md`
  (approved 2026-09-14, blob `4d7900d7…`) — G/P/J/FR/NFR/CON/M/OUT/RK/Q
  customer-фрейма; источник требований.
- `workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/00-discovery/brief.md`
  (engineer-фрейм, blob `39e8c625…`) — S/IF/CON/AP/RK/Q; оценка систем и
  архитектурные предпочтения.
- `src/spec_runner/mcp_server.py:19` — `_launch_stop_config`; `:22` —
  `_build_config`; `:206` — `Popen` без `cwd`/`--project-root`/`--change`;
  `:230` — единственный читатель launch config; `:284` — `run_server`.
- `src/spec_runner/config.py:241`–`:242`, `:836` — CWD-относительный поиск
  YAML.
- `src/spec_runner/cli.py:179` — `_acquire_run_lock`; `:831`, `:1055`,
  `:1274` — точки `clear_stop_file`.
- `tests/test_mcp.py` (`TestMCPStop`, `TestMCPRunTask`),
  `tests/test_lazy_mcp_import.py`, `tests/test_mcp_v2_wire.py`,
  `tests/fixtures/fake_claude.sh`, conftest-пояс `PaidBinaryReached`.
- Issue #485 «MCP: унифицировать launch scope для run_task, stop и read
  tools» — источник; неблокирующая находка формального review PR #484;
  прецедент #481 (узкое исправление `stop`).
