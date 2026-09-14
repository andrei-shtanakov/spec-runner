---
spec_stage: behaviour-spec
status: approved
owner_role: product
traces_to:
- requirements
upstream_hashes:
  requirements: 1c93693e948d5c19d5da488f578a5968e76a4cd1
version: 2
approved_content_hash: 89f46153aac93203485de23fbe84b4f209d7b790
approved_by: andrei-shtanakov
approved_at: '2026-09-14T06:40:50Z'
---

# Behaviour spec — MCP launch scope (spec-runner#485)

## Область поведения

Спецификация описывает наблюдаемое поведение `spec-runner mcp` на отрезке
«сервер запущен для одного scope (`project_root` + ровно один namespace) → любой
из восьми tools вызван с tool-level аргументами → config взят из launch scope
либо вызов отклонён → `run_task` формирует и запускает child → child берёт lock,
стирает старый stop-marker, публикует ready → родитель отвечает `started` →
`stop` пишет marker → child его не стирает (в single-task режиме `run --task`
потребление возможно только до задачи)», а также поведение
поверхностей, на которых этот путь предъявляется: JSON-ответы tools, файлы в
spec-директории namespace, лог child, programmatic-вход `mcp_run_server()`,
README/CHANGELOG.

Наблюдаемыми считаются: какой `tasks.md` прочитан и какой YAML применён; текст и
класс ответа tool (`started` / `stop_requested` / `status: error` с причиной);
был ли вызван `Popen` и сколько раз; argv и `cwd` child; resolved `ExecutorConfig`
child в сравнении с родительским; набор файлов `.executor-*` в spec-директории
namespace, в плоском `spec/` и в CWD сервера; наличие lock-файла с pid child в
момент ответа `started`; наличие stop-marker до и после `started`; сколько задач
child начал до выхода; код выхода child и хвост его лога; число платных
агентских вызовов; время прогона базового E2E.

Внутренние решения design-стадии здесь не фиксируются: канал ready — файл
`<spec_dir>/.executor-ready` или поле в текстовом lock-файле `ExecutorLock` (Q-02);
судьба child при таймауте ready — завершение родителем или pid в ответе (Q-02);
обратная сверка effective config child родителем (Q-03); точная форма поля
`namespace` в ответах `status`/`task_detail` (FR-09). Сценарии написаны так,
чтобы оставаться верными при любом допустимом ответе: они проверяют предъявленный
факт (lock существует, marker на месте, задача не начата), а не механизм, которым
он достигнут.

Термины употребляются в значении upstream'а (§3 требований): **launch scope**,
**плоский запуск**, **противоречащий `spec_prefix`**, **effective config**,
**представимое / непредставимое поле**, **ready**, **stop-marker**.

## Сценарии

### A. Один launch scope для всех восьми tools

#### BEH-01: Все восемь tools обслуживают launch scope, а не CWD сервера
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** внешний проект `<external>` с `spec/changes/add-x/tasks.md`
  (две задачи) и плоским `spec/tasks.md` с другим составом задач; сервер
  запущен командой `spec-runner mcp --project-root <external> --change add-x`
  из директории, не являющейся `<external>` и содержащей свой собственный
  `spec/tasks.md`.
- **When** вызываются `status`, `tasks`, `next_tasks`, `task_detail`, `costs`,
  `logs` без tool-level `spec_prefix`.
- **Then** каждый ответ отражает `<external>/spec/changes/add-x/tasks.md`:
  `tasks` перечисляет ровно две задачи change, `task_detail` находит задачу
  change и не находит задачу плоского `spec/`, `status.total_tasks == 2`.
- **And** ни один ответ не содержит задач из `spec/tasks.md` CWD сервера и из
  плоского `<external>/spec/tasks.md`.
- **And** `stop` на том же сервере пишет marker в
  `<external>/spec/changes/add-x/` — поведение #484 сохранено, но теперь оно
  общее для всех восьми tools, а не исключение для одного.

#### BEH-02: YAML ищется по `project_root` launch scope, а не по CWD процесса
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** `<external>/spec-runner.config.yaml` объявляет
  `spec_governance: strict`, `tasks.md` change — managed со `status: draft`;
  в CWD сервера YAML либо отсутствует, либо объявляет `spec_governance: off`.
- **When** сервер запущен из этой CWD с `--project-root <external> --change
  add-x` и вызван `run_task("TASK-001")`.
- **Then** ответ — `status: error` с текстом governance-гейта (`⛔ spec
  governance: …`), как в существующем
  `TestMCPRunTask::test_strict_governance_blocks_spawn`; `Popen` не вызван.
- **And** тот же сценарий с legacy-расположением
  `<external>/spec/executor.config.yaml` даёт тот же отказ: обе формы YAML
  находятся по `project_root`.
- **And** зеркальный случай — `<external>` объявляет `off`, CWD сервера
  объявляет `strict` — запускает child: YAML CWD не читается вовсе.

#### BEH-03: Чтение не оставляет следов ни в scope, ни вне его
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** сервер BEH-01; state DB change ещё не существует.
- **When** вызваны все шесть read-tools (`status`, `tasks`, `next_tasks`,
  `task_detail`, `costs`, `logs`), включая вызовы по несуществующей задаче.
- **Then** `rglob(".executor-*")` по плоскому `<external>/spec/` и по CWD
  сервера возвращает пустой список.
- **And** в `<external>/spec/changes/add-x/` не появляется state DB, lock,
  stop- или ready-файла: чтение идёт через `ExecutorState.for_read` (#337) и
  ничего не создаёт даже в собственном namespace.

#### BEH-04: Ни один tool не пересобирает config из CWD (M-02: 7/8 → 0/8)
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** сервер с заполненным launch scope; функция `_build_config` (или её
  преемник, строящий config из CWD) подменена двойником, который поднимает
  исключение при любом вызове.
- **When** вызываются все восемь tools с пустым tool-level `spec_prefix`.
- **Then** ни один вызов не поднимает исключение двойника: все восемь проходят
  через один helper holder-а, ни один не обращается к CWD-сборке.
- **And** модульная глобаль `_launch_stop_config` отсутствует — на её месте
  один типизированный holder, через который `run_server(config)` публикует
  scope; статическая проверка (`grep _build_config` по tool-обработчикам
  `mcp_server.py`) даёт ноль вызовов, минующих holder.
- **And** число tools, пересобирающих config из CWD, предъявляется как метрика:
  было 7/8 (все, кроме `stop`), стало 0/8.

### B. Противоречащий tool-level `spec_prefix`

#### BEH-05: `run_task` с противоречащим prefix отказывает по имени и ничего не запускает
`traces: [FR-02]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** сервер запущен с `--change add-x`; `subprocess.Popen` подменён
  считающим двойником (как в `TestMCPRunTask`).
- **When** вызван `run_task("TASK-001", spec_prefix="other-")`.
- **Then** ответ — JSON со `status: error`, текст которого называет и launch
  namespace (`add-x`), и запрошенный prefix (`other-`).
- **And** двойник `Popen` не вызван ни разу; namespace сервера не изменился —
  следующий `status()` без prefix по-прежнему отвечает за `add-x`.
- **And** тот же отказ при запуске сервера с `--spec-prefix p-` и вызове
  `run_task("TASK-001", spec_prefix="other-")`: текст называет `p-` и `other-`.
- **And** отказ не заканчивается трейсбеком.

#### BEH-06: Предикат противоречия один и тот же для всех восьми tools
`traces: [FR-02]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** сервер с `--change add-x`.
- **When** каждый из семи остальных tools (`status`, `tasks`, `costs`, `logs`,
  `next_tasks`, `task_detail`, `stop`) вызван с `spec_prefix="other-"`.
- **Then** каждый отвечает тем же классом ошибки, что BEH-05, с тем же
  называнием обеих сторон конфликта.
- **And** state DB namespace `other-` не создаётся ни в `<external>/spec/`, ни
  где-либо ещё; `stop` не пишет marker ни в `other-`, ни в `add-x`.
- **And** формулировка ошибки во всех восьми ответах порождена одним
  предикатом: ни один tool не имеет собственной редакции «противоречия».

#### BEH-07: Совпадающий, пустой и уточняющий prefix — не противоречие
`traces: [FR-02]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** три сервера: (а) `--spec-prefix p-`; (б) `--change add-x`;
  (в) плоский запуск без namespace из `<external>`.
- **When** на (а) вызван `status(spec_prefix="p-")`; на (а) и (б) —
  `status(spec_prefix="")`; на (в) — `status(spec_prefix="p-")`.
- **Then** (а) с совпадающим prefix отвечает успехом в namespace `p-`.
- **And** пустой tool-level prefix на любом сервере отвечает успехом в launch
  namespace — это дефолт, а не выбор.
- **And** (в) с непустым prefix отвечает успехом в namespace `p-` внутри
  `<external>` — рабочее допущение Q-01: при плоском запуске tool-level prefix
  уточняет scope, а не противоречит ему; `project_root` при этом остаётся
  `<external>` — это утверждает собственный contract-тест сценария в
  `tests/test_mcp_launch_scope.py` (существующий
  `TestMCPStop::test_explicit_prefix_keeps_launch_project_root` закрепляет
  другой случай — (а), совпадающий prefix при launch `--spec-prefix`, и
  свидетелем плоского запуска не является).
- **And** если владелец решит Q-01 иначе, меняется ровно этот последний пункт —
  остальные три остаются верны.

### C. `run_task` передаёт child точный scope и effective config

#### BEH-08: Child живёт в `project_root` и ровно в одном namespace
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** сервер `--project-root <external> --change add-x`, запущенный из
  чужой директории; в конфиге `<external>` — детерминированный fake command,
  завершающий задачу успешно.
- **When** вызван `run_task("TASK-001")` и child доработал.
- **Then** argv child содержит `--project-root <external>` и `--change add-x`,
  не содержит `--spec-prefix`; `cwd` child равен `<external>`.
- **And** state DB, lock-файл и лог child лежат под
  `<external>/spec/changes/add-x/`; в плоском `<external>/spec/` и в CWD
  сервера файлов `.executor-*` нет.
- **And** тот же сценарий с сервером `--spec-prefix p-` даёт argv с
  `--spec-prefix p-` и без `--change`: child получает ровно один namespace —
  тот, с которым запущен сервер.

#### BEH-09: Effective config child равен родительскому по всем представимым полям
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** сервер запущен с набором представимых override, отличающихся от
  дефолтов и от YAML: `--max-retries`, `--timeout`, `--no-tests`, `--no-branch`,
  `--no-commit`, `--no-review`, `--integration-pr`, `--hitl-review`, `--budget`,
  `--task-budget`, `--callback-url`, `--log-level`, `--log-json`; YAML
  `<external>` объявляет непредставимые поля (`review_policy`,
  `execution_mode`, `harness_guard`, `commands`), отличные от дефолтов
  dataclass; child запускается тестовым entry point, который записывает свой
  resolved `ExecutorConfig` в файл и выходит.
- **When** вызван `run_task("TASK-001")` и child записал свой config.
- **Then** для каждого представимого поля значение в записанном config child
  равно значению в родительском launch scope.
- **And** для каждого непредставимого поля значение child равно родительскому,
  потому что оба прочитали один YAML по `<external>` (BEH-02).
- **And** сравниваются два resolved `ExecutorConfig`, а не строка команды:
  тест не знает и не проверяет, каким флагом какое поле доехало.

#### BEH-10: Перечень serializer-а и `common`-парсера не расходятся
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_serializer.py`
- **Given** serializer `ExecutorConfig → argv`, словарь `_COMMON_DEFAULTS`
  (`cli.py`) — единственный источник флагов парсера `common` — и действия
  subparser-а `run` из `_build_parser()` (run-only флаги `--strict`/`--no-strict`).
- **When** сравниваются множество полей, которые serializer объявляет
  представимыми через `common`, и множество ключей `_COMMON_DEFAULTS`; отдельно
  — run-only перечень serializer-а и `dest`-ы действий `run`.
- **Then** каждое представимое `common`-поле serializer-а имеет ключ в
  `_COMMON_DEFAULTS`, а каждое run-only поле (`spec_governance`) — действие у
  `run` (serializer не выдумывает флагов, которых парсер не примет).
- **And** каждый ключ `_COMMON_DEFAULTS` либо объявлен serializer-ом
  представимым, либо явно перечислен как намеренно непередаваемый с причиной
  (например, `spec_prefix` и `change` передаются как единый namespace, а не как
  два поля) — молчаливого пропуска нет.
- **And** добавление флага в `common` без правки serializer-а (или наоборот)
  делает этот тест красным: рассинхрон (RK-05) ловится до мержа, а не в runtime.

#### BEH-11: Child запускается entry point-ом текущего окружения, а не `spec-runner` из PATH
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** тестовое окружение, в `PATH` которого нет исполняемого
  `spec-runner` (PATH сужен до системного минимума либо console-script
  переименован на время теста).
- **When** вызван `run_task("TASK-001")`.
- **Then** child запущен (`started` получен, lock с pid child существует) через
  `sys.executable -m spec_runner` либо console-script того же venv — argv[0]
  child указывает внутрь текущего интерпретатора/venv.
- **And** установленный в другом venv или системный `spec-runner` не вызывается:
  версия и модули child совпадают с родительскими.

#### BEH-12: Вывод child уходит в лог namespace, и многословный child не зависает
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** fake command, печатающий в stdout и stderr объём, заведомо больший
  размера pipe-буфера ОС (≥ 1 MiB), а затем завершающий задачу успешно.
- **When** вызван `run_task("TASK-001")`, родитель после `started` больше
  ничего у child не читает.
- **Then** child завершается в пределах таймаута теста, не блокируясь на
  записи: `stdout`/`stderr` направлены в файл под `config.logs_dir` launch
  namespace (или в `DEVNULL`), а не в `subprocess.PIPE`.
- **And** если лог-файл используется, он лежит под
  `<external>/spec/changes/add-x/`, и `logs` tool умеет его показать; вне
  namespace лог не появляется.

### D. Невоспроизводимый parent-config

#### BEH-13: Override непредставимого поля — отказ до `Popen`, называющий поле
`traces: [FR-04]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_serializer.py`
- **Given** родительский `ExecutorConfig`, у которого относительно YAML
  `<external>` переопределено непредставимое поле (например,
  `review_policy: required` при YAML `advisory`, или `execution_mode: tdd` при
  YAML `standard`) — programmatic-config, переданный в `run_server(config)`;
  `Popen` подменён считающим двойником.
- **When** вызван `run_task("TASK-001")`.
- **Then** ответ — `status: error`, текст называет поле, которое невозможно
  передать child, и говорит, почему (нет CLI-флага; YAML по `project_root` его
  не объявляет).
- **And** двойник `Popen` не вызван; в namespace не создано ни state, ни lock,
  ни stop-, ни ready-файла.
- **And** тот же отказ, когда YAML по `project_root` не найден там, где
  родитель его прочитал (файл удалён между запуском сервера и `run_task`):
  child прочитал бы дефолты, а не родительский config — молчаливого расхождения
  не допускается.

#### BEH-14: Полностью воспроизводимый config запускает child ровно один раз
`traces: [FR-04]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_serializer.py`
- **Given** родительский config, все override которого — представимые поля
  (BEH-09), YAML на месте; `Popen` подменён двойником, который имитирует
  успешный handshake.
- **When** вызван `run_task("TASK-001")`.
- **Then** serializer формирует argv без отказа, `Popen` вызван ровно один раз,
  ответ — `started`.
- **And** отказ BEH-13 и успех здесь решаются одной и той же проверкой
  воспроизводимости: нет второго места, где решается «можно ли передать».

### E. Startup-handshake: `started` означает «lock взят, marker стёрт, ready опубликован»

#### BEH-15: `started` приходит не раньше lock и ready
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** сервер `--project-root <external> --change add-x`, настоящий CLI
  entry point тестового окружения, fake command.
- **When** вызван `run_task("TASK-001")` и получен ответ `started` с `pid`.
- **Then** в момент получения ответа lock-файл
  `config.state_file.with_suffix(".lock")` — под change
  `<external>/spec/changes/add-x/.executor-state.lock` (`cli.py:181`,
  `config.py:546`) — уже существует и содержит pid, равный `pid` из ответа.
- **And** ready уже опубликован (файл в namespace или поле lock — по Q-02);
  тест проверяет факт публикации через тот интерфейс, который зафиксирует
  design, а не конкретный путь.
- **And** до появления lock и ready ответ не приходит: двойник child, который
  берёт lock через 2 с, даёт `started` не раньше чем через 2 с.

#### BEH-16: Stop сразу после `started` не теряется
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** `tasks.md` change содержит ready-задачу `TASK-001`; fake command
  при вызове создаёт файл-сигнал и ждёт файла-освобождения; сервер как в
  BEH-15; child — `run --task TASK-001`: ровно одна задача
  (`cli.py:945`–`:951`), точки «между задачами» нет, единственная проверка
  marker-а стоит до задачи (`cli.py:1273`).
- **When** вызван `run_task("TASK-001")`, получен `started`, тест дождался
  файла-сигнала (задача уже идёт — проверка `:1273` пройдена), вызван
  `stop()`, затем fake освобождён.
- **Then** child завершает `TASK-001` одной успешной попыткой и выходит с
  кодом 0; после выхода `<external>/spec/changes/add-x/.executor-stop`
  **существует** — ни одна ветка child после `started` его не стёрла.
- **And** ответ `stop` — `stop_requested` с `stop_file`, лежащим под
  `<external>/spec/changes/add-x/`.
- **And** немедленный вариант — `stop()` сразу после `started`, без ожидания
  сигнала — даёт после выхода child ровно один из двух легальных исходов:
  (i) marker потреблён проверкой до задачи — файла нет, в логе child «Graceful
  shutdown requested», у `TASK-001` ни одной попытки; (ii) marker лёг после
  проверки — `TASK-001` выполнена, файл на месте. Исход «файла нет ∧ попытка
  есть» — потерянный stop, дефект #485 — делает тест красным.
- **And** семантика stop не изменилась: child не убит, три точки потребления
  не тронуты, marker после выхода child стирает только следующий старт
  (OUT-01).

#### BEH-17: Marker до `run_task` стирается, marker после `started` — нет
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** сервер как в BEH-15.
- **When** (а) `stop()` вызван **до** `run_task("TASK-001")`, затем `run_task`;
  (б) в отдельном прогоне `stop()` вызван **после** `started`.
- **Then** в (а) стартовый `clear_stop_file` child стирает старый marker, и child
  выполняет задачу как обычно — прежнее поведение сохранено; ready публикуется
  после этого стирания.
- **And** в (б) marker остаётся на месте до выхода child: ни одна ветка child
  после ready не вызывает `clear_stop_file` (в `run --task` точки потребления
  после задачи нет — файл стирает только следующий старт executor-а).
- **And** три точки потребления marker-а (`run`, `watch`, `retry`) не
  изменились: существующие тесты stop-семантики проходят без правок.

#### BEH-18: Таймаут ожидания ready объявлен и конфигурируем
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** родитель ждёт ready с таймаутом, значение которого объявлено
  upstream'ом/design (конфиг-ключ или константа с дефолтом, достаточным для
  медленного CI).
- **When** таймаут переопределён на малое значение и child (двойник) публикует
  ready позже него.
- **Then** родитель отвечает `status: error` с причиной `timeout` по истечении
  именно переопределённого значения, а не дефолта.
- **And** с дефолтным таймаутом тот же двойник, публикующий ready быстро,
  получает `started`: дефолт не мешает штатному пути.
- **And** handshake не вводит нового IPC: канал ready — существующий механизм
  (файл в namespace или поле lock), наблюдаемый теми же средствами, что и
  остальные `.executor-*`.

#### BEH-19: Гарантия «stop после started» держится статистически (soak ×20)
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** немедленный вариант BEH-16, обёрнутый в цикл из 20 итераций под
  `@pytest.mark.slow`; между итерациями namespace очищается.
- **When** цикл прогнан.
- **Then** во всех 20 итерациях исход — (i) либо (ii) BEH-16; запрещённый исход
  «файла нет ∧ попытка есть» не встретился ни разу — ноль потерянных
  stop-запросов; распределение (i)/(ii) печатается в отчёт как evidence, не
  утверждается.
- **And** soak не входит в обязательный CI-прогон (`-m "not slow"`): базовый E2E
  BEH-16 в CI — одна итерация; soak запускается вручную и в ночном профиле,
  если он есть.

### F. Child без lock или без ready — error, не ложный `started`

#### BEH-20: Занятый lock — ошибка запуска, второй executor не запущен
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** lock namespace `add-x` удерживается другим процессом с живым pid
  (тестовый процесс-держатель).
- **When** вызван `run_task("TASK-001")`.
- **Then** ответ — `status: error`, текст называет занятый lock (pid держателя
  или путь lock-файла); `started` не возвращён.
- **And** lock-файл после ответа содержит прежний pid держателя: второй executor
  в namespace не запущен и lock не перехвачен.
- **And** ответ приходит детерминированно раньше полного таймаута ready: child,
  вышедший с exit 1 на занятом lock, замечен через `proc.poll()`.

#### BEH-21: Child, умерший до ready, — ошибка с кодом выхода и хвостом лога
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** child (fake command или двойник entry point), выходящий с exit 1
  сразу на старте, до lock и ready, с диагностикой в stderr.
- **When** вызван `run_task("TASK-001")`.
- **Then** ответ — `status: error`, содержащий код выхода child и хвост его лога
  из `logs_dir` namespace (диагностика stderr видна оператору без поиска
  файла).
- **And** ответ приходит без ожидания полного таймаута: `proc.poll()`
  наблюдается параллельно с ожиданием ready.
- **And** тот же класс ответа для отказа governance-гейта внутри child и для
  `ConfigError` при старте child — оператор видит причину, а не `started`.

#### BEH-22: Child, не опубликовавший ready, — `timeout`, и ready-файла после ошибки нет
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** двойник child, который берёт lock и зависает, не публикуя ready;
  таймаут ready переопределён на малое значение (BEH-18).
- **When** вызван `run_task("TASK-001")`.
- **Then** по истечении таймаута ответ — `status: error` с причиной `timeout`.
- **And** судьба child — по решению design (Q-02): либо родитель завершил его и
  lock освобождён, либо child оставлен, и ответ содержит его `pid` явно;
  третьего варианта (child жив, а ответ о нём молчит) нет.
- **And** после любой ошибочной ветки этого раздела (BEH-20, BEH-21, BEH-22) в
  namespace не остаётся ready-файла: следующий `run_task` не примет чужой ready
  за свой.

### G. Programmatic-контракт

#### BEH-23: `mcp_run_server()` без аргументов работает как прежде
`traces: [FR-07]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_lazy_mcp_import.py`
- **Given** вызывающий (fleet-агент, spec-runner-vscode) в project root
  плоского проекта.
- **When** вызван `spec_runner.mcp_run_server()` без аргументов.
- **Then** holder заполнен config-ом, построенным из текущей директории, как
  строит его `run_server(None)` сегодня; все восемь tools работают в плоском
  namespace.
- **And** `tests/test_lazy_mcp_import.py` проходит без правок: lazy import
  `mcp_server` через `__getattr__` пакета сохранён.
- **And** `import spec_runner` в окружении без установленного `mcp` не падает:
  `mcp` остаётся опциональной зависимостью.
- **And** плоский запуск `spec-runner mcp` из project root без namespace
  обслуживается тем же holder-ом без особой ветки.

### H. Контракт синхронизирован

#### BEH-24: README, CHANGELOG и #485 говорят то же, что код
`traces: [FR-08]`

- **checked_by**: `status: planned` `kind: manual` `owner: qa` `target: README.md`
- **Given** PR, реализующий FR-01…FR-06.
- **When** ревьюер читает README §MCP Server и CHANGELOG в том же PR.
- **Then** README описывает: привязку сервера к launch scope и требование
  запуска (`--project-root` + один namespace); отказ на противоречащем
  tool-level `spec_prefix` как видимое изменение контракта; значение `started`
  (child держит lock, ready опубликован, stop после него не теряется).
- **And** CHANGELOG содержит запись под Unreleased с ссылкой на #485.
- **And** #485 закрыт ссылкой на PR, в закрывающем комментарии — ссылка на E2E
  BEH-16.
- **And** соседям (devtools, spec-runner-vscode) новый контракт объявлен
  issue с `slug:` + `from:` (ADR-ECO-006) либо записью в
  `../prograph-vault/authored/notes/` — без правки их файлов.

### I. Видимость scope

#### BEH-25: `status` и `task_detail` называют обслуживаемый scope
`traces: [FR-09]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp_launch_scope.py`
- **Given** три сервера: `--project-root <external> --change add-x`;
  `--project-root <external> --spec-prefix p-`; плоский запуск из `<external>`.
- **When** на каждом вызваны `status()` и `task_detail("TASK-001")`.
- **Then** оба ответа содержат `project_root`, равный resolved абсолютному пути
  `<external>`, и `namespace`, различающий три случая (change `add-x`, prefix
  `p-`, плоский `spec/`) — точная форма поля по design.
- **And** все существующие ключи ответов `status` и `task_detail` сохранены с
  прежними значениями: изменение аддитивно, `TestMCPStatus` и
  `TestMCPTaskDetail` проходят без правок.
- **And** если владелец вырезает FR-09 при сокращении объёма, этот сценарий
  снимается целиком с записью в CHANGELOG-заметке PR; ни один другой сценарий
  на него не опирается.

### J. Сквозные гарантии

#### BEH-26: Ни один tool не пишет вне spec-директории launch scope, включая ошибочные ветки
`traces: [FR-01, FR-03, FR-04, FR-06]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** полный прогон сценариев BEH-01…BEH-22 на одном `<external>` с
  `--change add-x`, включая отказные ветки (противоречащий prefix,
  невоспроизводимый config, занятый lock, ранний выход, таймаут ready).
- **When** после прогона выполнен `rglob(".executor-*")` по плоскому
  `<external>/spec/` и по CWD сервера.
- **Then** оба списка пусты.
- **And** все runtime-файлы child и сервера (state, lock, stop, ready, лог)
  перечислены под `<external>/spec/changes/add-x/` — это единственная защита
  P-03, соседнего namespace на той же машине.

#### BEH-27: Тесты не вызывают платного агента и укладываются в CI-бюджет
`traces: [FR-05, FR-06]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_mcp_e2e_485.py`
- **Given** conftest-пояс `PaidBinaryReached` активен; child в тестах использует
  детерминированный fake command (`tests/fixtures/fake_claude.sh` или
  специализированный двойник).
- **When** прогнан `uv run pytest tests/ -q -m "not slow"`.
- **Then** ни один тест не поднимает `PaidBinaryReached`.
- **And** базовый E2E #485 (BEH-15, BEH-16, BEH-17) завершается быстрее 60 с и
  входит в `-m "not slow"`; замер приводится из CI, а не оценкой.
- **And** fake command умеет три режима, которых требуют сценарии: успешно
  завершить задачу (BEH-16); напечатать многословный вывод (BEH-12); выйти с
  exit 1 на старте (BEH-21).

#### BEH-28: Существующие MCP-тесты меняют ожидания только там, где этого требует контракт
`traces: [FR-02, FR-05]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_mcp.py`
- **Given** `tests/test_mcp.py` (`TestMCPStatus`, `TestMCPTasks`,
  `TestMCPCosts`, `TestMCPLogs`, `TestMCPStop`, `TestMCPNextTasks`,
  `TestMCPTaskDetail`, `TestMCPRunTask`) и `tests/test_mcp_v2_wire.py` на
  момент до PR.
- **When** PR реализован и суита прогнана.
- **Then** все перечисленные тесты зелёные.
- **And** изменённые ожидания ограничены двумя прямыми следствиями контракта:
  `run_task` отвечает `started` только после ready (двойники `Popen` в
  `TestMCPRunTask` имитируют handshake), и противоречащий tool-level prefix
  отклоняется (FR-02); любое иное изменение ожидания существующего теста —
  находка ревью, а не побочный эффект.

## Матрица трассируемости

| Behaviour | Functional requirements |
|---|---|
| BEH-01 | FR-01 |
| BEH-02 | FR-01 |
| BEH-03 | FR-01 |
| BEH-04 | FR-01 |
| BEH-05 | FR-02 |
| BEH-06 | FR-02 |
| BEH-07 | FR-02 |
| BEH-08 | FR-03 |
| BEH-09 | FR-03 |
| BEH-10 | FR-03 |
| BEH-11 | FR-03 |
| BEH-12 | FR-03 |
| BEH-13 | FR-04 |
| BEH-14 | FR-04 |
| BEH-15 | FR-05 |
| BEH-16 | FR-05 |
| BEH-17 | FR-05 |
| BEH-18 | FR-05 |
| BEH-19 | FR-05 |
| BEH-20 | FR-06 |
| BEH-21 | FR-06 |
| BEH-22 | FR-06 |
| BEH-23 | FR-07 |
| BEH-24 | FR-08 |
| BEH-25 | FR-09 |
| BEH-26 | FR-01, FR-03, FR-04, FR-06 |
| BEH-27 | FR-05, FR-06 |
| BEH-28 | FR-02, FR-05 |

Обратная трассировка по функциональным требованиям:

| Requirement | Behaviours |
|---|---|
| FR-01 | BEH-01, BEH-02, BEH-03, BEH-04, BEH-26 |
| FR-02 | BEH-05, BEH-06, BEH-07, BEH-28 |
| FR-03 | BEH-08, BEH-09, BEH-10, BEH-11, BEH-12, BEH-26 |
| FR-04 | BEH-13, BEH-14, BEH-26 |
| FR-05 | BEH-15, BEH-16, BEH-17, BEH-18, BEH-19, BEH-27, BEH-28 |
| FR-06 | BEH-20, BEH-21, BEH-22, BEH-26, BEH-27 |
| FR-07 | BEH-23 |
| FR-08 | BEH-24 |
| FR-09 | BEH-25 |

Все FR-01–FR-09 покрыты хотя бы одним сценарием; сценариев без FR нет.

Нефункциональные требования закреплены наблюдаемыми результатами сценариев без
введения дополнительных идентификаторов трассировки: NFR-01 (ни одной записи
вне spec-директории launch scope) — BEH-03, BEH-06, BEH-13, BEH-22 и сводно
BEH-26; NFR-02 (без платного агента, базовый E2E < 60 с в `not slow`) — BEH-27;
NFR-03 (stop после `started` не теряется статистически) — BEH-16 и soak BEH-19.

Метрики требований предъявляются сценариями: M-01 (зелёный E2E
`run_task → started → stop` на внешнем project root с `--change`) — BEH-16;
M-02 (число tools, пересобирающих config из CWD, 7/8 → 0/8) — BEH-04.

## Границы спецификации

Сценарии не предписывают: канал ready — файл `<spec_dir>/.executor-ready` или
поле в JSON lock (Q-02) — BEH-15, BEH-17, BEH-18 и BEH-22 проверяют факт
публикации через интерфейс, который зафиксирует design; судьбу child при
таймауте ready (Q-02) — BEH-22 допускает оба решённых варианта и исключает
только молчание; обратную сверку effective config child родителем (Q-03) — если
design её принимает, FR-06 получает ещё одну error-ветку («config mismatch»),
и её сценарий добавляется на стадии design/acceptance, а BEH-09 остаётся
тестовой формой той же проверки; точную форму поля `namespace` (FR-09) — BEH-25
требует лишь различимости трёх случаев; имена тестовых файлов — цели
`checked_by` взяты из §9 требований как ожидание, design вправе переименовать
при сохранении покрытия.

Численные значения объявляются upstream'ом и design: таймаут ready (BEH-18,
BEH-22) проверяется против объявленного значения, а не константы, зашитой здесь;
порог 60 с (BEH-27) и объём ≥ 1 MiB (BEH-12) взяты из NFR-02 и AP-04/RK-01
требований; число итераций soak (BEH-19) — из NFR-03.

Вне спецификации остаются границы, подтверждённые upstream'ом: семантика
остановки за пределами стартового handshake — stop остаётся файл-маркером,
executor не убивается, три точки потребления в `run`/`watch`/`retry` не
переписываются (OUT-01); сетевой транспорт и аутентификация MCP (OUT-02);
argv-контракт spec-runner-vscode и жизненный цикл команды `change` (OUT-03);
новые write-tools помимо `run_task` и `stop` (OUT-04); новый IPC для ready
(сокет, pipe-протокол); правка соседних репозиториев — только issue/handoff
(BEH-24).
