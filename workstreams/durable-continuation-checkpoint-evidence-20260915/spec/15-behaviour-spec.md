---
spec_stage: behaviour-spec
status: draft
owner_role: product
traces_to:
- requirements
upstream_hashes:
  requirements: ffbd991ff6f3fa6d2fe0297307c7ee1cbb8af041
---

# Behaviour spec — Durable continuation checkpoint и evidence для run/call/attempt (spec-runner#480)

## Область поведения

Спецификация описывает наблюдаемое поведение spec-runner на отрезке «CLI
invocation получил один `run_id` → run-start записан → перед каждым платным
subprocess подтверждён call-start → subprocess вернулся, call-result и
attempt записаны → после каждой continuation-relevant mutation опубликован
checkpoint → прогон закрыт closure либо оборвался без неё → оператор с другой
машины читает прогон по `run_id` (`evidence`) или восстанавливает его в новом
каталоге (`restore`) и получает следующий безопасный шаг либо `needs-human`».
Поверхности, на которых этот путь предъявляется: ledger-таблицы
`attempts` / `agent_calls` / `pr_agent_calls`, prompt-артефакты `prompts_log`,
OTel JSONL и compliance audit-log, файлы checkpoint-а (DB snapshot, manifest,
WIP artifact, spool), записи evidence bundle (run-start, call-start,
call-result, attempt export, run-closure), stdout/stderr и exit code команд
`run` / `retry` / `watch` / `plan` / `review-pr` / `doctor` / `restore` /
`evidence` / `status` / `costs`, JSON-контракты `--json-result` и
`status --json`.

Наблюдаемыми считаются: значение `run_id` и `pipeline_id` в каждом канале;
был ли вызван `Popen` и сколько раз, и предшествовала ли ему запись
call-start (порядок фиксируется двойником store); число строк call-start и
call-result на `call_id`; outcome и стоимость (число или `null`) каждой
записи; `sequence` и `checkpoint_id` последнего checkpoint-а, полученного
двойником store; содержимое manifest-а и отсутствие в нём локальных фактов;
байты файлов в восстановленном каталоге (SHA-256) и наличие/отсутствие lock,
stop-marker и временных worktrees; kind, reason и `last_checkpoint_id`
closure; наличие closure после `kill -9`; текст и exit code отказов;
`git status --porcelain` продуктового репо после E2E; число исключений
`PaidBinaryReached`.

Внутренние решения design-стадии здесь не фиксируются: какой канал вправе
подтверждать call-start — внешний store или локальный spool (Q-02); формат
WIP artifact (Q-03); синхронная или асинхронная доставка checkpoint-а и
что значит «доступен» (Q-05) — с одной оговоркой: **один** факт порядка
требования фиксируют сами (FR-03: подкоманда без платного вызова не
завершается успешно до ack своего mutation-checkpoint-а), и BEH-48
предъявляет ровно его — порядок двух наблюдаемых событий, а не устройство
доставки, очередь или число ожиданий; где живёт единый seam
call-start/call-result
(Q-06); точный состав policy identity (Q-07); движок redaction (Q-08);
исполнитель retention (Q-11); откуда `run` читает open calls при старте
(Q-12). Сценарии проверяют предъявленный факт — запись существует раньше
процесса, файл восстановлен байт в байт, отказ произошёл до `Popen` — а не
механизм, которым он достигнут. Там, где имя команды ещё подтверждается
(Q-04), сценарии используют рабочие имена требований: `spec-runner restore
<run_id> --into <dir>`, `spec-runner evidence <run_id> [--json]`,
`spec-runner evidence close-call <run_id> --call <call_id> --reason …`.

Термины употребляются в значении upstream'а (§3 требований): **`run_id`**,
**`pipeline_id`**, **`call_id`**, **платный subprocess**, **provenance**,
**policy identity**, **call-start** / **call-result** / **open call**,
**ack**, **run-start** / **run-closure**, **continuation-relevant
mutation**, **continuation checkpoint**, **manifest**, **WIP artifact**,
**evidence bundle**, **emergency spool**, **artifact store**, **durable
boundary**, **legacy run**, **restore**. «Двойник store» — тестовая
реализация публичного контракта store (put/get/list/delete + acl), которая
записывает порядок вызовов и умеет отказывать по команде теста; «двойник
`Popen`» — подмена `subprocess.Popen`, считающая вызовы и не запускающая
ничего (как в существующих E2E с `tests/fixtures/fake_claude.sh`).

## Сценарии

### A. Один `run_id` во всех каналах

#### BEH-01: Все каналы прогона несут один и тот же full UUIDv4 `run_id`, а `pipeline_id` лежит рядом
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_run_identity.py`
- **Given** окружение с `ORCHESTRA_PIPELINE_ID=<pid>`, проект с одной ready
  задачей, fake CLI для GREEN и review, включённые OTel (`obs.init_logging`)
  и compliance `AuditLogger`, двойник store.
- **When** выполнен `spec-runner run --task TASK-001` и задача прошла GREEN и
  review.
- **Then** OTel JSONL, audit-log, строки `agent_calls` этой задачи (GREEN и
  review), заголовки prompt-артефактов, checkpoint manifest и run-closure
  содержат одно и то же значение `run_id`, и это full UUIDv4 (36 символов,
  не восьмисимвольное сокращение).
- **And** в каждом из тех же каналов `pipeline_id == <pid>` хранится
  отдельным полем; ни один канал не подставляет `pipeline_id` на место
  `run_id` и наоборот.
- **And** без `ORCHESTRA_PIPELINE_ID` `run_id` по-прежнему один во всех
  каналах, а `pipeline_id` — если он присутствует хотя бы в одном канале —
  присутствует одним и тем же значением в каждом канале, который его несёт
  (сегодня это ULID, который чеканит `obs.init_logging`, #482), и это
  значение никогда не равно `run_id`; канал, где `pipeline_id` нет, несёт
  только `run_id`, а не `run_id` под именем `pipeline_id`.
- **And** prompt-артефакт несёт `run_id` и `call_id` в заголовке файла
  (сегодня — первая строка `=== <SLUG> PROMPT ===`, `prompts_log.log_prompt`),
  а тело между заголовком и терминальной секцией остаётся «prompt как
  отправлен» (#282): для prompt-а в пределах `bound` оно байт в байт равно
  prompt-у, переданному двойнику `Popen`; в тело ни один id не вписан.

#### BEH-02: Каждый invocation чеканит новый `run_id`, `watch` — один на весь цикл
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_run_identity.py`
- **Given** тот же проект и окружение, что в BEH-01, две ready задачи.
- **When** подряд выполнены два invocation `spec-runner run --task TASK-001`
  и `spec-runner run --task TASK-002`.
- **Then** run-start, closure и строки `agent_calls` первого invocation несут
  `run_id` A, второго — `run_id` B, `A != B`; `pipeline_id` в обоих равен
  `<pid>`.
- **And** один invocation `spec-runner watch` (fake CLI, две задачи, стоп по
  stop-marker после второй) оставляет ровно один run-start, одну closure и
  строки обеих задач с одним `run_id`.
- **And** статический тест: в `src/spec_runner/cli.py` нет `uuid4().hex[:8]`
  как источника `run_id`, в `src/spec_runner/audit_log.py` нет собственного
  `uuid.uuid4()` для `run_id`; `AuditLogger` принимает `run_id` параметром.

#### BEH-03: Старая DB мигрирует аддитивно, старые строки читаются с `run_id IS NULL`
`traces: [FR-01]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_state.py`
- **Given** `spec/.executor-state.db`, созданная версией до контракта:
  таблицы `attempts`, `agent_calls`, `pr_agent_calls` без столбцов `run_id`
  и `call_id`, с несколькими строками и известной суммой стоимости.
- **When** DB открыта новой версией через `ExecutorState`.
- **Then** миграция добавляет столбцы, не удаляя и не переписывая строки;
  старые строки читаются с `run_id IS NULL` (и `call_id IS NULL` для
  call-ledger-ов); `spec-runner costs` показывает прежнюю сумму.
- **And** `schemas/executor-state.schema.json` и `docs/state-schema.md`
  описывают новые столбцы; версия контракта состояния — minor bump, а не
  major: существующие schema-тесты проходят без изменения ожиданий.
- **And** `--json-result` и `status --json` содержат аддитивные ключи
  `run_id` (и `pipeline_id`, когда он есть); все ранее пинованные ключи
  сохранены с прежней семантикой — `tests/test_json_result_contract.py`
  зелёный, golden-фикстуры изменены только добавлением ключей.

#### BEH-04: Read-only команды получают `run_id` для логов, но run-start не пишут
`traces: [FR-01, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_closure_every_exit.py`
- **Given** проект с DB и двойником store, записывающим каждую публикацию.
- **When** выполнены `spec-runner status`, `costs`, `validate`, `report`,
  `evidence <run_id>` (по прогону из BEH-01). Подкоманда `evidence` здесь
  read-only ровно в форме `evidence <run_id>`: её формы `close-call` и
  `purge` — писатели и предъявляются ниже.
- **Then** structlog каждой команды содержит `run_id` (full UUIDv4), и он у
  каждой команды свой.
- **And** двойник store не получил ни одного run-start, checkpoint-а или
  closure от этих команд.
- **And** каждая из команд `run`, `retry`, `watch`, `plan`, `review-pr`,
  `doctor`, `tdd abandon/repair/resume/release`, `budget authorize`, `restore`
  (fake CLI, где нужен) оставляет ровно один run-start и ровно одну closure.
- **And** формы `evidence close-call` и `evidence purge` платного вызова не
  делают, но меняют continuation-state и потому тоже пишут свою пару
  run-start + closure; здесь это не наблюдается, а предъявляется там, где
  живут сами команды, — BEH-11 и BEH-42. Read-only в этом сценарии
  наблюдается ровно для формы `evidence <run_id>`: run-start у неё — красный
  тест.
- **And** три платящих пути, не берущие executor lock, — `retry TASK-001`,
  `watch` (один круг до stop-marker) и `run --all --force` — оставляют ту же
  пару run-start + closure, что и обычный `run`: двойник store получает по
  одному run-start на invocation, и ни один call-start этих прогонов не
  ссылается на `run_id` без run-start. Отсутствие run-start у любого из трёх —
  красный тест; тест не вправе доказывать run-start вызовом
  `_acquire_run_lock`, потому что ни один из трёх путей его не проходит.

### B. Запись раньше траты

#### BEH-05: На каждом сайте платного вызова call-start подтверждён до `Popen`
`traces: [FR-02]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_call_start_before_spawn.py`
- **Given** двойник store, записывающий в общий журнал событие
  `call_start(call_id)` с временем ack; двойник `Popen`, записывающий в тот же
  журнал событие `spawn(argv)` и отдающий ответ fake CLI; конфигурация,
  доводящая прогон до каждого сайта из §3 требований.
- **When** по очереди прогнаны сайты: RED authoring, RED agent round после
  lint-findings (#220), GREEN, review, `review:<role>`
  (параллельный и последовательный режим), `plan --full` (три стадии),
  `plan --gated`, интерактивный `plan "<описание>"` (один круг цикла: fake CLI
  отвечает `PLAN_READY`, ответ на приглашение — отказ от записи задач, так что
  цикл завершается после одного платного вызова),
  `review-pr` verify, `review-pr` fix, оба сайта пробы `doctor --with-review`
  (исполнение канонной задачи и её review).
- **Then** для каждого `spawn` в журнале непосредственно раньше есть
  `call_start` с ack, и у пары один `call_id`; число `spawn` равно числу
  call-start-ов с ack.
- **And** call-start каждого сайта содержит `run_id`, `call_id`, provenance
  из одного словаря (`red`, `red:fix`, `green`, `review`, `review:<role>`,
  `plan:<stage>`, `plan:interactive`, `review-pr:verify`, `review-pr:fix`,
  `doctor:execute`, `doctor:review`), policy identity, digest redacted
  prompt-а, timestamp, а для task-сайтов — `task_id` и номер attempt.
  Исключение — оба сайта **эфемерной пробы**: по функции это те же GREEN и
  review, но их опубликованные записи задачи не несут, потому что канонная
  задача пробы адресуема только внутри её scratch (BEH-47); требование
  `task_id` от них этот And не выполняет. Ни
  одно значение словаря не остаётся без сайта в этом журнале: значение,
  которого не производит ни один прогон матрицы, — красный результат этого
  And.
- **And** тот же `call_id` записан рядом с `provenance` в строке ledger-а
  **своего семейства** — `agent_calls` для сайтов задачи, `pr_agent_calls`
  для `review-pr`, `plan_agent_calls` для трёх сайтов планирования, — так
  что ledger стоимости и evidence соединяются одним ключом на каждом сайте.
  Требование строки в `agent_calls` от сайта планирования этот And не
  выполняет: у той таблицы `task_id` — `NOT NULL`, задачи у планирования
  нет, и BEH-24 предъявляет ровно обратное.
- **And** матрица гоняется с настоящим именем `claude` в `claude_command`,
  и это исполнимо только потому, что autouse-гвард `_no_real_agent_calls`
  переключён на одно имя `paid_call._spawn`, а два его патча швов
  (`tdd._run_agent`, `execution._run_agent_process`) сняты тем же коммитом:
  до перевода они поднимают отказ на внешнем шве раньше, чем управление
  дошло бы до `_spawn`, и рецепт не доходит до журнала. Двойник `_spawn`
  самого сценария заменяет собой патч гварда — документированное свойство
  гварда, а не обход; пояс `PaidBinaryReached` остаётся линией под ним и
  красит любой путь к бинарю мимо `_spawn`. `tests/test_harness_guards.py`
  предъявляет гвард на новом имени в том же коммите: окна, в котором швы уже
  не патчатся, а `_spawn` ещё не проверен, быть не должно.
- **And** отказ гварда на `_spawn` не глотает ни один `except Exception` на
  платных путях. Предъявлено там, где гвард не подменён двойником
  (`tests/test_harness_guards.py`), и **вне** `execute_task` — на review и на
  интерактивном `plan`, потому что именно они ловят `Exception` сами и
  превращают отказ в `ReviewVerdict.ERROR` и в выход с кодом 0: наблюдаемое —
  падение теста с вердиктом гварда. Зелёный прогон, «успешно» получивший
  verdict `error` или exit 0, — красный результат этого And. Пояс
  `PaidBinaryReached` этот случай не подстраховывает по построению: он
  цепляется за создание процесса, а отказ происходит раньше, — поэтому тип
  отказа обязан быть от `BaseException`, как сам пояс.
- **And** статический тест по образцу пояса `PaidBinaryReached`
  (`tests/test_harness_guards.py`): любой путь к бинарю провайдера проходит
  через один seam call-start; обходной вызов `subprocess.run`/`Popen` с
  argv провайдера — красный тест. В `cli_plan.py` таких путей **три**, и все
  три прогоняются выше: gated (`_generate_stage_draft`), `--full`
  (трёхстадийный цикл) и интерактивный цикл `cmd_plan`; тест, доказавший seam
  на двух из трёх, оставляет достижимый из CLI платный вызов без call-start.

#### BEH-06: Без acknowledgement процесс не стартует; отказ — до траты, exit 2, closure с причиной
`traces: [FR-02, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_call_start_before_spawn.py`
- **Given** двойник store, отказывающий в ack call-start (ошибка или
  превышение объявленного таймаута ack); двойник `Popen`; одна ready задача
  под `execution_mode: tdd`.
- **When** выполнен `spec-runner run --task TASK-001`.
- **Then** двойник `Popen` вызван 0 раз; задача остановлена на текущей
  безопасной границе (статус не `done`, attempt с `error_code =
  INFRASTRUCTURE`), процесс завершён с exit 2.
- **And** stderr называет отказ типизированно (`Refusal(kind="instrument")`,
  #230) — не «tests/lint check», не трейсбек.
- **And** closure прогона записана (в store, если он принимает closure; иначе
  stderr называет и это) с kind `failed` и exit code 2: `error_kind` attempt-а
  — `instrument`, а он не входит в отказное подмножество `ERROR_KINDS`,
  поэтому правило даёт `failed`, а не `refused` (design § 6.3).
- **And** таймаут ack конфигурируем; при превышении поведение то же, что при
  явном отказе: ожидание без предела невозможно.

#### BEH-07: Budget-отказ происходит до call-start — отказанный вызов не оставляет строки
`traces: [FR-02]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_call_start_before_spawn.py`
- **Given** `task_budget`, исчерпанный записанной стоимостью предыдущих
  вызовов; двойник store; двойник `Popen`.
- **When** выполнен `spec-runner run --task TASK-001`, и budget guard (#213)
  отказывает перед GREEN.
- **Then** двойник store не получил call-start для этого вызова; двойник
  `Popen` не вызван; в `agent_calls` строки нет («a refusal is no row»).
- **And** prompt-артефакт GREEN заканчивается `=== NOT STARTED: … ===`
  (#296) — как сегодня.
- **And** отказ виден в attempt (`error_code = BUDGET_EXCEEDED`,
  `error_kind = budget`) и в closure прогона: `budget` входит в отказное
  подмножество `ERROR_KINDS`, поэтому kind — `refused`.

#### BEH-08: Timeout, пустой ответ, `is_error` при exit 0 и crash провайдера — это call-result, не open call
`traces: [FR-02, FR-06]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_evidence_every_outcome.py`
- **Given** fake CLI в четырёх режимах: превышает таймаут; выходит с exit 0
  и пустым stdout; отдаёт JSON с `is_error: true` при exit 0; падает с
  exit 1 без маркера. Двойник store.
- **When** каждый режим прогнан через GREEN одной задачи.
- **Then** у каждого вызова есть call-start и ровно один call-result;
  outcome соответствует классификации `runner.classify_agent_answer`
  (#241): `timeout`, `failed` (empty), `failed` (crashed при exit 0),
  `failed` (crashed); стоимость — число, если провайдер её сообщил, иначе
  `null`.
- **And** ни один из четырёх вызовов не числится open call: `evidence
  <run_id>` показывает 0 open calls, следующий `run` в том же namespace
  не ставит задачу в `needs-human` по этой причине.
- **And** open call возникает только если процесс spec-runner умер между ack
  и записью результата (BEH-09).

#### BEH-09: Open call блокирует продолжение: `run --all` пропускает, `run --task` отказывает, `restore` даёт `needs-human` до subprocess
`traces: [FR-02, FR-05]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_open_call_door.py`
- **Given** прогон, в котором двойник seam вызывает `os._exit` сразу после
  ack call-start (граница «после call-start до spawn») либо сразу после
  возврата `Popen` до записи call-result (граница «после spawn до
  результата»); в store остался call-start без call-result.
- **When** в том же namespace выполнены `spec-runner run --all`, затем
  `spec-runner run --task TASK-001`, затем `spec-runner retry TASK-001`,
  `spec-runner watch`, `spec-runner run --all --force`, затем `spec-runner
  restore <run_id> --into <new-dir>`.
- **Then** `run --all` пропускает задачу с причиной, называющей `call_id`,
  provenance и «open call», и выполняет остальные ready задачи; `run --task
  TASK-001` отказывает с той же причиной, exit 1; двойник `Popen` для этой
  задачи вызван 0 раз в обоих случаях.
- **And** `restore` печатает `needs-human: open call <call_id> (<provenance>,
  TASK-001)` и завершается exit 1 **до** любого `Popen`.
- **And** обнаружение выполнено по evidence bundle/checkpoint, а не по памяти
  процесса: сценарий проходит в новом процессе и в новом каталоге.
- **And** ни один из трёх шагов не создал второй call-start для того же
  attempt: молчаливого повтора нет.
- **And** тот же результат — когда локальная state DB между crash-ом и
  следующим `run` уничтожена: после `spec-runner reset` (штатная подкоманда,
  удаляющая файл DB) и, отдельным прогоном, после удаления файла DB вручную
  `run --all` по-прежнему пропускает задачу с той же причиной, а `run --task
  TASK-001` отказывает с exit 1 и 0 вызовов двойника `Popen`. Пустой
  `agent_calls` свежей DB «отсутствием open call» не считается: свидетельство
  берётся из evidence bundle, и единственный способ закрыть open call
  остаётся аудируемым (`evidence close-call --reason`, BEH-11).
- **And** тот же прогон в **клоне репозитория на другом пути**, где локальной
  DB не было вовсе, даёт то же самое: open call предъявлен, `Popen` не
  вызван. Прогон в каталоге без единого прошлого прогона при этом стартует
  как обычно — пустая история не отказ.
- **And** решение принимается по namespace целиком, а не по одному `run_id`,
  и это предъявлено конфигурацией, где эти два ответа расходятся: в том же
  namespace прогон A закрыт штатно, более поздний прогон C — **`spec-runner
  plan` в интерактивной форме** (без `--full` и без `--gated`), убитый
  двойником seam сразу после ack call-start, — оставил open call X в
  `plan_agent_calls`, и оператор выполняет `restore <run_id-A> --into <dir>
  --experimental`.
  Restore отказывает `needs-human` с `run_id` прогона C, `call_id` X и его
  provenance — а не применяет snapshot A на том основании, что у самого A
  open call нет; 0 `Popen`, `--into` остаётся пустым. Применённый snapshot A
  здесь — красный тест.
- **And** первый `run` в восстановленном каталоге спрашивает заново, а не
  доверяет индексу из snapshot-а: в том же сценарии с закрытым X (после
  `evidence close-call`) restore применяется, и следующий `run --all`
  предъявляет open call, оставленный ещё более поздним прогоном D, вместо
  того чтобы пройти по `open`-строкам snapshot-а мимо него. Наблюдаемый
  признак — обращение к индексу workstream-а на этом старте. Условие, при
  котором «restore применяется» вообще достижимо, названо прямо, а не
  подразумевается, и посылок **две**, по числу шагов, которые пришлось бы
  снять. Шаг 4 проверки (6) — namespace-wide и стоит ДО шага 5: на момент
  `restore` в workstream-е не должно быть ни одного call-start без
  call-result. Поэтому прогон D начинается **после** применения
  snapshot-а; начнись он раньше, его собственный open call отказал бы
  восстановлению шагом 4, и ветка осталась бы недостижимой — ровно так,
  как утверждала исходная находка. Этот же порядок и делает знак
  наблюдаемым: D лежит в индексе workstream-а и не лежит в snapshot-е,
  так что пройти по `open`-строкам snapshot-а мимо него — красный тест.
  Шаг 5 снимается вторым и только прогоном C — и снимается **выбором
  подкоманды и её формы**, а не границей краша: интерактивный `plan` до
  подтверждения оператором не пишет ничего **continuation-relevant** (свой
  лог-файл он открывает до первого вызова, `cli_plan.py:782`, но лог в
  перечне §3 не состоит), а задачи дописываются в `tasks.md` уже после
  последнего платного вызова (`cli_plan.py:878`),
  поэтому ни один его call-start не идёт после continuation-relevant
  mutation — сколько бы кругов Q&A он ни сделал, — и acknowledged
  checkpoint-а под его `run_id` нет ни одного. Форма пинуется не для
  красоты: `plan --full` пишет артефакт стадии сразу после её вызова
  (`cli_plan.py:694`), и call-start следующей стадии там уже идёт после
  записи. При этом `plan` — из **блокирующей** половины перечня, то есть
  снимается именно второе условие ключа, а не первое: конфигурация
  проверяет ключ, а не обходит его.
- **And** подкоманда здесь — не деталь, и обратный случай предъявлен
  рядом: тот же сценарий с C = `run`, убитым на той же границе, обязан
  дать **отказ** шагом 5. Флип `tasks.md` в `in_progress` —
  continuation-relevant mutation по §3 (решение владельца 2026-09-20,
  широкое прочтение), он стоит до первого платного вызова
  (`execution.py:646` против `:690-801`), а ожидание publisher-а — перед
  call-start, поэтому любой `run`/`retry`/`watch`, дошедший до ack
  call-start, к этому моменту уже несёт acknowledged checkpoint.
  Прогона этих трёх подкоманд с open call и без checkpoint-а не бывает,
  **когда флип состоялся**; единственная дыра в этом названа здесь же:
  `update_task_status` возвращает False, если у задачи нет meta-строки, —
  флипа тогда нет, mutation нет, и такой прогон дошёл бы до call-start без
  checkpoint-а. Сценарий берёт задачу с нормальной meta-строкой, и в этой
  конфигурации красным тестом будет **применённый** snapshot.
  Дверь на оба ответа не влияет: она закрывает **call**, не прогон.
- **And** та же конфигурация с одним изменённым фактом даёт противоположный
  ответ, и это предъявлено: если C успел записать continuation-relevant
  mutation и доставить её checkpoint (acknowledged), то после закрытия X
  дверью restore прогона A всё равно отказывает `needs-human` — теперь уже
  шагом 5, с `run_id` прогона C и указанием на его checkpoint, а выходом
  называется C. Применённый snapshot A здесь — красный тест: изменения C в
  него не попали, и продолжение с A потеряло бы их молча. Два ответа на
  одной и той же последовательности команд — ровно то, ради чего ключ
  составной: перечень отвечает, какая работа считается, checkpoint — была
  ли она вообще.
- **And** тот же знак предъявлен на путях, которые не проходят через
  исполнение `run`: при том же open call `spec-runner retry TASK-001`
  отказывает с той же причиной и exit 1; `spec-runner watch` (остановленный stop-файлом
  после первого круга) предъявляет тот же open call на старте invocation и
  задачу к исполнению не берёт; `run --all --force` пропускает её с той же причиной. Двойник
  `Popen` для этой задачи вызван 0 раз во всех трёх. Ни один из трёх не
  берёт executor lock, поэтому доказательство детекции вызовом
  `_acquire_run_lock` этот And не выполняет.
- **And** `restore` более раннего `run_id` workstream-а, у которого есть
  более поздний **закрытый** прогон из блокирующей половины перечня,
  **несущий хотя бы один acknowledged checkpoint**, — отказывает `needs-human`:
  изменения такого прогона в snapshot A не попали, и отказ называет как выход
  прогон, чей `restore` исполним: последний с acknowledged checkpoint-ом,
  после которого нет ни одного блокирующего прогона. Знак проверяется
  **исполнением**, а не чтением строки: тест берёт напечатанный `run_id` и
  выполняет его `restore` в новый пустой `--into` — тот обязан применить
  snapshot. Отказ на напечатанном выходе — красный тест. Предъявлено на
  конфигурации, где «последний с checkpoint-ом» и «исполнимый» расходятся:
  A → B (`plan`, закрыт, checkpoint есть) → C (`review-pr`, закрыт, раунд
  записан, checkpoint есть). Выходом обязан быть C; напечатанный B — красный
  тест, потому что `restore <B>` упрётся в ту же C. Предъявлено как минимум на `review-pr`
  (его раунд пишет `pr_review_comments`) и на `plan` (дописывает задачи в
  `tasks.md` после платного вызова): неотказ на любом из двух — красный
  тест, потому что «платит, но задач не выбирает» не то же самое, что
  «ничего не оставил».
- **And** более поздний **холостой** прогон из той же блокирующей половины
  восстановлению не мешает, и это второе условие ключа: в том же namespace
  после закрытия A выполняется `run --all`, которому нечего делать (готовых
  задач нет), и, отдельным прогоном, `run --all`, чей старт отказан занятым
  executor lock-ом уже после run-start. Оба закрылись, оба лежат в индексе
  позже A, acknowledged checkpoint-а нет ни у одного: `restore <run_id-A>` применяет
  snapshot. Отказ здесь — красный тест, и не только по букве: он оставляет
  оператора без единого пути, потому что восстановить названный выходом
  холостой прогон нельзя — digests берутся из последнего acknowledged
  checkpoint-а, которого у него нет.
- **And** более поздний прогон из неблокирующей половины восстановлению не
  мешает, и это предъявлено на трёх, каждый из которых оставляет в индексе
  workstream-а свою строку: дверь `evidence close-call`, закрывшая X в And
  про первый `run` в восстановленном каталоге (её запись — шаг close уже
  существующего вызова, и неотказ предъявлен в обеих конфигурациях — где
  вызов жил в `agent_calls` и где в `pr_agent_calls`); `evidence purge`,
  отработавший между A и восстановлением (open call он не закрывает вовсе, а
  его `deletions/<ts>.json` — аудит собственного удаления); и **чужой**
  `restore`, более ранний, — наблюдается, когда A восстанавливают второй раз
  в новый пустой `--into` (повторный запуск в уже занятый каталог отказывает
  раньше, на проверке каталога, и шага 5 не наблюдает). Во всех трёх
  snapshot A применяется; отказ на любом из трёх — красный тест.
- **And** четвёртый неблокирующий — `doctor`, и его знак другой, чем у
  первых трёх: он не «в перечне», а не имеет второго условия ключа вовсе.
  `doctor --with-review --yes` с fake CLI, отработавший между A и
  восстановлением, оставляет в индексе workstream-а свою строку и **ни
  одного** acknowledged checkpoint-а (BEH-47), поэтому snapshot A
  применяется; и он не называется выходом в отказе шага 5 ни в одной
  конфигурации — выход обязан нести acknowledged checkpoint, а у `doctor`
  его нет. Напечатанный как выход `doctor` — красный тест: его `restore`
  упёрся бы в отсутствие digests.

#### BEH-10: Один `call_id` — ровно один call-start и не более одного call-result
`traces: [FR-02, FR-06]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_open_call_door.py`
- **Given** seam call-start/call-result с двойником store.
- **When** тест записывает call-start для `call_id` X, затем call-result для
  X, затем повторно пытается записать call-result для X с другим outcome, а
  затем второй call-start с тем же X.
- **Then** первая запись результата принята; вторая отвергнута ошибкой,
  называющей `call_id`; чтение по X возвращает первый результат неизменным.
- **And** второй call-start с тем же `call_id` отвергнут; `call_id` двух
  разных вызовов в одном прогоне и в двух прогонах различны (full UUIDv4).
- **And** отказ записи не «улучшает» исход задачи и не удаляет первую запись.

#### BEH-11: Open call закрывается только аудируемой операторской командой
`traces: [FR-02]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_open_call_door.py`
- **Given** состояние BEH-09 (open call `call_id` X у TASK-001).
- **When** выполнены по очереди: `spec-runner evidence close-call <run_id>
  --call X` без `--reason`; та же команда с `--reason "provider dashboard
  shows no charge"`; та же команда с `--reason` повторно; та же команда с
  `SPEC_RUNNER_AGENT=1`.
- **Then** первая попытка отказана до записи (нет `--reason`); вторая пишет
  новый call-result с outcome `resolved_unknown`, ссылкой на open call X,
  записанным actor и reason; третья идемпотентна — отвечает «уже закрыт»,
  второй записи нет; четвёртая отказана guardrail-ом агента (по образцу
  `tdd abandon`).
- **And** после второй попытки TASK-001 снова выбираема: `run --task
  TASK-001` доходит до нового call-start (новый `call_id`), а `evidence`
  показывает 0 open calls и запись `resolved_unknown` с actor.
- **And** команда не запускает платный вызов и не решает, повторять ли его:
  повтор — отдельный `run`, инициированный оператором.
- **And** платного вызова нет, но continuation-state меняется — задача снова
  выбираема, — поэтому команда пишет свою пару run-start + closure. Первая
  попытка отказана argparse-ом (`--reason` объявлен `required=True` по
  образцу `tdd abandon`, `cli.py:2215`), то есть **до** вызова handler-а и до
  `start()`: у неё нет ни run-start, ни closure, и это BEH-04, а не пробел.
  У трёх остальных invocation-ов двойник store получает ровно один run-start
  и ровно одну closure: `completed` на записи `resolved_unknown` и на
  идемпотентном повторе (код 0, невыполненной работы нет), `failed` на
  отказе guardrail-а `SPEC_RUNNER_AGENT` (код 1); при недоступном store —
  `failed` и exit 2. Kind выводит диспетчер по коду выхода, своих сайтов
  closure у команды нет (design § 6.3). Закрытие строки ledger-а — mutation,
  и её checkpoint опубликован под тем же `run_id`, у которого run-start
  есть; checkpoint под `run_id` без run-start — красный тест.

### C. Checkpoint после каждой continuation-relevant mutation

#### BEH-12: Checkpoint — snapshot с WAL-only страницами, а не копия файла
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_checkpoint_is_a_snapshot.py`
- **Given** открытое соединение к `spec/.executor-state.db` с
  `PRAGMA wal_autocheckpoint=0`, так что записанный attempt лежит только в
  `-wal`.
- **When** записан attempt; снят checkpoint через seam «после mutation»;
  DB snapshot из checkpoint-а восстановлен в новом каталоге и открыт.
- **Then** attempt виден в восстановленной DB.
- **And** контрольный вариант того же теста, где вместо snapshot-а взят
  `shutil.copy` main-файла `.db`, **не** видит attempt — тест обязан
  различать backup API и копирование файла (инвариант 6).

#### BEH-13: Каждая mutation из перечня публикует новый checkpoint с большим `sequence`; read-only команды — нет
`traces: [FR-03]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_checkpoint_after_every_mutation.py`
- **Given** двойник store, записывающий каждый полученный checkpoint
  (`checkpoint_id`, `sequence`, `supersedes`); проект под `execution_mode:
  tdd` с fake CLI.
- **When** по очереди совершены mutation: запись attempt; запись RED
  checkpoint; запись claims и их release; запись `tdd_phases`;
  `verify_evidence`; gate verdict; waiver (`phase_waivers` /
  `waivers_applied`); remedy (`tdd abandon`); budget authorization
  (`budget authorize` отдельным invocation, без attempt); строка `pr_*`
  (раунд `review-pr` с fake gh); harness status flip — **обе его формы**:
  коммитящая (`bookkeeping.commit_status_flip`, #192) и та, что пишет флип
  в `tasks.md` без коммита (`task.update_task_status`), причём вторая
  предъявлена на **двух** статусах набора `BOOKKEEPING_STATUSES`, которые
  harness пишет о собственном процессе до платного вызова: `in_progress`
  из `execute_task` и `review` из `post_done_hook` перед вызовом ревью. Вторая — решение
  владельца 2026-09-20 о широком прочтении §3: перечень сайтов §3.1
  описывал лишь часть доставленных путей и требование §3 не сужал.
- **Then** после каждой mutation двойник получил ровно один новый checkpoint;
  `sequence` строго возрастает внутри `run_id`; `checkpoint_id` — UUIDv4;
  manifest каждого следующего указывает предыдущий в `supersedes`.
- **And** флип `in_progress`, записанный `execute_task` до первого платного
  вызова, публикует checkpoint **до** call-start, а не после: двойник
  получает его раньше, чем двойник провайдера — первый вызов. То же для
  флипа `review`: его checkpoint доставлен раньше, чем стартовал платный
  вызов ревью. Пропуск любого из двух — красный тест: они одного класса по
  §3 требований, и покрытие одного не доказывает покрытия другого. Это и есть
  наблюдаемое следствие широкого прочтения, на которое опирается BEH-09
  («прогона `run`/`retry`/`watch` с open call и без acknowledged
  checkpoint-а не существует»).
- **And** после `status`, `costs`, `validate`, `report`, `evidence <run_id>`
  двойник не получил ничего. Перечень read-only именует форму `evidence
  <run_id>`, а не подкоманду `evidence` целиком: её формы `close-call` и
  `purge` — mutation, и checkpoint первой предъявляет BEH-11.
- **And** checkpoint после `budget authorize` содержит эту authorization в
  DB snapshot — authority mutations публикуются даже без attempt.
- **And** все точки публикации вызывают одну функцию «после mutation» (по
  образцу `run_plugin_hooks_for`, #307): статический тест находит ровно один
  seam, через который проходят перечисленные сайты записи.
- **And** mutation эфемерной пробы через этот seam checkpoint-а не даёт:
  `doctor` записывает attempt в state DB своего scratch-каталога, и двойник
  не получает ни одного checkpoint-а (полный знак — BEH-47).

#### BEH-47: Проба `doctor` не оставляет следа в учёте проекта, а её платные вызовы оставляют полный
`traces: [FR-02, FR-03, FR-06]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_doctor_probe_scope.py`
- **Given** проект под контрактом с двойником store, собственной задачей
  `TASK-001` в `tasks.md` и **относительным** путём в настройках адаптера
  store; fake CLI, отвечающий и на исполнение, и на review.
- **When** выполнен `spec-runner doctor --with-review --yes`, а затем в том же
  каталоге `spec-runner run --all`.
- **Then** двойник получил от invocation-а `doctor`: run-start, две пары
  call-start/call-result с provenance `doctor:execute` и `doctor:review`,
  одну closure — и **ни одного** checkpoint-а, ни одного attempt-экспорта.
  Любой checkpoint под этим `run_id` — красный тест: его DB snapshot был бы
  снимком удалённого каталога, а manifest нёс бы identity scratch-а.
- **And** ни одна опубликованная запись пробы не несёт `task_id`:
  восстановить из неё `open`-строку на какую бы то ни было задачу проекта
  нечем, и столбец `agent_calls.task_id` при этом остаётся `NOT NULL` —
  строку самой пробы приняла scratch-DB, где канонная задача настоящая.
- **And** последующий `run --all` берёт **собственную** `TASK-001` проекта и
  доходит до её платного вызова. Задача проекта, одноимённая канонной задаче
  пробы, заблокированной не оказывается — блокировка здесь красный тест, в
  том числе после `spec-runner reset` и в свежем клоне (ветка процедуры
  старта, восстанавливающая `open`-строки из store, BEH-09).
- **And** записи легли в store **вызывающего**, а не внутрь удалённого
  scratch: относительный путь адаптера разрешён в абсолютный до того, как
  проба сменила рабочий каталог, и после `doctor` все перечисленные ключи
  читаются на месте. Прогон, оставивший 0 ключей (адрес уехал вместе со
  scratch и удалён с ним), — красный тест, и это единственный наблюдаемый
  признак: платный вызов при этом состоялся и деньги потрачены.
- **And** платных вызовов ровно два и у проекта под `execution_mode: tdd`:
  проба идёт под `standard`, третьего вызова (RED authoring) нет, и число
  совпадает с тем, что cost gate объявил оператору перед подтверждением.
- **And** опубликованные срезы прогона `doctor` следов пробы не содержат, и
  предъявлено это на конфигурации, где ошибиться легче всего, — включённый
  аудит с **абсолютным** `audit_log_path`: срез `runs/<run_id>/audit-log.jsonl`
  (если он есть вовсе) не называет ни канонной задачи пробы, ни её
  namespace, а хвост task-history за прогон пуст. Строка пробы в срезе
  вызывающего — красный тест.
- **And** ни один из этих знаков не зависит от вердикта `doctor`: тот же
  набор ключей предъявлен и когда fake CLI отвечает маркером провала, то
  есть на verdict `broken` (BROKEN — исход пробы, а не отказ записи).

#### BEH-48: Подкоманда без платного вызова не завершается успешно, пока её checkpoint не acknowledged
`traces: [FR-03, FR-05, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_mutation_checkpoint_ack.py`
- **Given** двойник store, умеющий по команде теста задержать или отклонить
  ack checkpoint-а; проект с записанной задачей, активным бюджетом,
  завершённой задачей под claims и активным RED-checkpoint-ом под claims.
- **When** выполнены `spec-runner budget authorize … --reason …` (одна
  mutation), `spec-runner tdd release TASK-001 --reason …` (две) и
  `spec-runner tdd abandon TASK-002 --checkpoint <id> --reason …`
  (многошаговая) — каждая сначала с исправным двойником, затем с
  отклоняющим ack.
- **Then** с исправным двойником ack mutation-checkpoint-а в общем журнале
  стоит **раньше** выхода с кодом 0 у каждой из трёх.
- **And** с отклоняющим все три завершаются exit 2, stderr называет
  недоставленный `sequence` и `checkpoint_id`; нулевого кода не получает ни
  одна — ложный успех есть красный тест. Строка успеха, напечатанная
  handler-ом раньше отказа, успехом не считается: авторитетны код выхода и
  closure, и stderr дополнительно говорит, что решение записано локально, но
  не доставлено.
- **And** отказ ack **не рвёт** многошаговую подкоманду посередине: у `tdd
  abandon` под отклоняющим двойником в DB есть и retirement claims, и строка
  `tdd_remedies` с actor и reason — то есть все её записи, а не первые из
  них. Состояние «red помечен abandoned, claims сняты, audit-строки нет» —
  красный тест: ожидание живёт на выходе invocation-а, а не между записями
  handler-а.
- **And** mutation при этом не откатывается и не теряется: строки в DB есть,
  потому что были закоммичены раньше. Наблюдаемое здесь — сами mutation и
  порядок ack относительно выхода; **что** остаётся не закрытым этим
  сценарием, названо прямо: убитый в окне между записью mutation и ack
  прогон правку теряет, и это принятый пробел (FR-05), а не дефект —
  транзакционное обязательство публикации бандл не вводит (spec-runner#528).
- **And** горячий путь `run` не получает ни одного нового синхронного
  ожидания: прогон `run --task` ведёт себя как до бандла по числу и месту
  обращений к двойнику store, и ожидание на каждую mutation задачи —
  красный тест. Цену гейта перед closure у подкоманд без платного вызова
  меряет бенчмарк (BEH-41), а не этот сценарий (RK-01): ожиданий у
  invocation-а по-прежнему столько, сколько точек drain, и число mutation
  подкоманды его не увеличивает.

#### BEH-14: Manifest валиден, полон и не содержит локальных фактов
`traces: [FR-03, FR-04]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_checkpoint_after_every_mutation.py`
- **Given** любой checkpoint, полученный двойником в BEH-13, снятый в проекте
  с известным абсолютным путём, PID, активным `spec-runner-red-*` worktree и
  `tdd_namespace`, не объявленным в config.
- **When** manifest провалидирован по `schemas/checkpoint-manifest.schema.json`
  и все файлы checkpoint-а прочитаны.
- **Then** manifest содержит `contract_version`, `run_id`, `pipeline_id`,
  repository identity (нормализованный remote URL, root commit), workstream
  (namespace, `spec_prefix` / `change_id`), `head`, `published_ref` с
  reachability, `config_hash`, effective TDD namespace с
  `namespace_source: computed`, join keys (последние ids attempt/call/
  closure), `digests` для каждого файла и `excluded`.
- **And** manifest хранит **значение** effective namespace, а не seed
  для пересчёта; повторный запуск того же прогона с объявленным
  `tdd_namespace` даёт `namespace_source: declared`.
- **And** `grep` по manifest и всем файлам checkpoint-а не находит ни
  абсолютного пути исходного каталога, ни PID, ни имени временного
  worktree; `excluded` перечисляет lock, `.<prefix>spec.lock`,
  `.executor-stop`, временные worktrees, `.executor-progress.txt`.
- **And** manifest несёт digest верхнего уровня над своим содержимым
  (BEH-40 проверяет его действие).

#### BEH-15: Checkpoint в degraded mode включает spool и помечен `degraded: true`
`traces: [FR-03, FR-08]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_emergency_spool.py`
- **Given** двойник SQLite, роняющий запись attempt (DB переходит в
  degraded mode), spool исправен, двойник store исправен.
- **When** attempt записан (в spool), сработал seam «после mutation».
- **Then** двойник store получил новый checkpoint, manifest которого несёт
  `degraded: true`; в checkpoint входит spool-файл с этой mutation; DB
  snapshot взят из последнего успешного состояния файла.
- **And** `state_degraded` уведомление отправлено один раз, как сегодня.
- **And** restore из этого checkpoint-а (BEH-33) доигрывает spool в DB и
  показывает attempt.

### D. Git-материал и незавершённая работа

#### BEH-16: Local-only commit, dirty, untracked и rescue stash восстанавливаются байт в байт
`traces: [FR-04]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_restore_drill.py`
- **Given** прогон под git automation с активной task-веткой, на которой
  есть один local-only commit (не в published ref), один изменённый tracked
  файл, один untracked файл и rescue stash с меткой `spec-runner rescue:
  TASK-001 …` (`hooks.rescue_uncommitted`, #231); published base ref
  доступен на fake forge (bare-репо). Записаны SHA-256 dirty/untracked
  байтов и SHA local-only commit.
- **When** последний checkpoint acknowledged; исходный каталог удалён
  целиком; выполнен `spec-runner restore <run_id> --into <other-abs-path>
  --experimental`.
- **Then** в новом каталоге `git log` task-ветки содержит local-only commit
  с тем же SHA; dirty и untracked файлы присутствуют с теми же SHA-256;
  `git stash list` содержит stash с той же меткой и тем же содержимым.
- **And** manifest прогона содержит для каждого ref, на который ссылается
  continuation (base, task-ветка, integration branch #254), имя, SHA и факт
  достижимости; байты pushed-объектов в checkpoint не входят (размер WIP
  artifact не зависит от объёма published-истории).
- **And** dirty/untracked работа отобрана по семантике
  `git_ops.uncommitted_work_paths` (#229): runtime-state (`.executor-*`)
  в WIP не попадает.

#### BEH-17: Lock, stop-marker, временные worktrees и process identity не восстанавливаются и не становятся authority
`traces: [FR-04, FR-05]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_restore_drill.py`
- **Given** прогон из BEH-16, в котором на момент checkpoint-а существовали
  executor lock `spec/.executor-state.lock` (`config.state_file` с суффиксом
  `.lock`, `_acquire_run_lock`; с `--spec-prefix` —
  `.executor-<prefix>state.lock`) с PID живого процесса, `.executor-stop`,
  `.<prefix>spec.lock`, worktree `spec-runner-red-*` и
  `.executor-progress.txt`.
- **When** выполнен restore в новый каталог.
- **Then** в новом каталоге нет ни одного из перечисленных файлов и
  worktrees; `git worktree list` показывает только основной.
- **And** `spec-runner status` в новом каталоге не сообщает о stale lock от
  чужого PID; следующий `run` создаёт lock заново живым процессом.
- **And** virtualenv и tool caches не восстановлены и не перечислены в
  manifest как обязательные (OUT-05).

#### BEH-18: Пустой WIP — явная запись, недостижимый published ref — `needs-human` с именем ref и SHA
`traces: [FR-04]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_restore_drill.py`
- **Given** два прогона: (а) чистое дерево, все commits в published ref;
  (б) manifest ссылается на published ref, который на fake forge удалён.
- **When** для каждого выполнен restore.
- **Then** (а) manifest несёт `wip: none` (запись, а не отсутствие файла),
  restore проходит, exit 0, `--json` отчёт отмечает `wip: none`.
- **And** (б) restore останавливается `needs-human`, exit 1, называя имя ref
  и ожидаемый SHA; реконструкция ref из локального материала не
  предпринята (OUT-09); в каталог ничего не записано.
- **And** WIP artifact подчиняется тем же digests, redaction и retention, что
  остальной checkpoint: его digest есть в `digests` manifest-а (BEH-40
  включает его в параметризацию).

### E. Restore по `run_id` в новом каталоге

#### BEH-19: Restore возвращает claims, budget authority, waiver и namespace и печатает следующий шаг без оплаты
`traces: [FR-05, FR-04]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_restore_drill.py`
- **Given** прогон под `execution_mode: tdd`, остановленный на durable
  boundary после подтверждённого RED (checkpoint, claim на файл теста),
  с `budget authorize --task-limit` для TASK-001, применённым waiver и
  Git-материалом из BEH-16; исходный каталог удалён; двойник `Popen`.
- **When** выполнен `spec-runner restore <run_id> --into <other-abs-path>
  --experimental`, затем `spec-runner tdd status TASK-001`, `spec-runner
  costs`, `spec-runner run --task TASK-001` (fake CLI).
- **Then** restore завершается exit 0 и печатает следующий шаг
  `spec-runner run --task TASK-001`; двойник `Popen` за время restore
  вызван 0 раз.
- **And** `tdd status` показывает active claim с тем же blob SHA и тот же
  RED checkpoint; `costs` показывает authorization; effective namespace
  равен значению из manifest; `budget.effective_limits` отвечает поднятым
  потолком.
- **And** claims gate видит старый claim: если тест до `run` изменяет
  замороженный файл, gate отказывает — правила claims не пересочинены
  (OUT-06).
- **And** следующий `run` начинает с GREEN — нового call-start для RED нет,
  оплаченный RED не повторён.
- **And** восстановленная DB получена из snapshot-а (+ replay spool), а не
  реконструирована из Git: строки `tdd_claims`, `red_checkpoints`,
  `budget_authorizations`, `phase_waivers` совпадают с экспортом terminal
  attempt-а исходного прогона.

#### BEH-20: Все проверки restore выполняются до записи в каталог, в объявленном порядке, и любое несовпадение — отказ
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_restore_refusals.py`
- **Given** валидный checkpoint прогона из BEH-19 и набор подменённых
  условий: (1) manifest с `contract_version` из будущего; (2) изменённый
  байт в DB snapshot; (3) целевой репозиторий с другим root commit; (4)
  активный config с другим `review_policy` (другой `config_hash` над
  `gates.POLICY_KEYS`); (5) активный config с `tdd_namespace`, отличным от
  записанного; (6) open call в bundle; (7) повреждённая строка spool.
- **When** для каждого условия выполнен restore в пустой `--into`.
- **Then** каждый вариант отказывает, `--into` остаётся пустым, exit 1
  (`needs-human`) для (3)–(6) и exit 2 (instrument) для (1), (2), (7);
  двойник `Popen` вызван 0 раз; `claims.check_claims` не вызывался.
- **And** сообщение (5) содержит оба значения namespace и оба способа
  получения (`declared`/`computed`) — отказ, не предупреждение (RK-06);
  сообщение (4) называет ключ и оба значения; сообщение (3) называет оба
  root commit.
- **And** при нескольких одновременных несовпадениях отказ называет первое
  по порядку contract version → digests → repository identity → policy
  identity → namespace → open calls → spool; порядок предъявлен тестом с
  двумя подменами сразу.
- **And** проверка (6) читает open calls всего workstream-а, а не только
  ключи восстанавливаемого `run_id`: условие (6) предъявляется в двух формах
  — open call внутри самого bundle и open call более позднего прогона того
  же workstream-а (BEH-09), — и обе дают `needs-human` на одном и том же
  месте порядка.
- **And** перечень, по которому шаг 5 решает, полон, и это проверяется не
  чтением: тест требует `set(PAYING_SUBCOMMANDS) == BLOCKING | NON_BLOCKING`
  при пустом пересечении. Подсадка — добавить в `PAYING_SUBCOMMANDS`
  подкоманду и не отнести её ни к одной половине: тест обязан покраснеть.
  Зелёный тест на такой подсадке означает, что полноту перечня не проверяет
  никто, и следующая забытая подкоманда молча получит поведение по
  умолчанию. And живёт здесь, а не в BEH-09, потому что предмет у него —
  машинерия проверки (6), и файл `checked_by` этого сценария принадлежит той
  же задаче, что и сама проверка.
- **And** если активный config `tdd_namespace` не объявляет, restore
  записывает восстановленное effective value как `tdd_namespace` в config
  нового каталога и печатает diff этой правки (рабочее допущение Q-09);
  файл после этого коммитуем и виден в `git status`.

#### BEH-21: Legacy run, отсутствие `--experimental` и непустой `--into` — отказы с точной причиной
`traces: [FR-05]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_restore_refusals.py`
- **Given** (а) `run_id` прогона, начатого версией до контракта: есть DB и
  логи, нет run-start, manifest и call records; (б) валидный checkpoint
  BEH-19; (в) тот же checkpoint и `--into`, в котором уже лежит файл.
- **When** выполнены `restore <run_id-a> --into <dir> --experimental`;
  `restore <run_id-b> --into <dir>` без флага; `restore <run_id-b> --into
  <non-empty>` с флагом.
- **Then** (а) fail-closed, exit 1: сообщение перечисляет «нет run-start»,
  «нет manifest», «нет call records» и ссылается на ручной путь
  `docs/architecture.md` («operational minimum»); реконструкции нет
  (CON-07).
- **And** (б) отказ с текстом CON-01 (experimental до приёмки FR-01–FR-08),
  каталог не тронут; после снятия статуса в CHANGELOG флаг не требуется —
  сценарий фиксирует оба состояния как параметр.
- **And** (в) отказ без перезаписи: содержимое `--into` байт в байт прежнее.
- **And** `restore --json` во всех исходах валиден по
  `schemas/restore-result.schema.json` и несёт `next_step` либо `reason`;
  exit code 0 / 1 / 2 однозначно соответствует `ok` / `needs-human` /
  `instrument`.

### F. Evidence на каждом платном вызове и terminal attempt

#### BEH-22: Матрица outcome × site — каждая клетка оставляет адресуемый call record
`traces: [FR-06, FR-02]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_evidence_every_outcome.py`
- **Given** fake CLI, параметризованный по исходу {success, `TASK_FAILED`,
  blocked (`TASK_BLOCKED`), timeout, infrastructure error}, и конфигурации,
  доводящие прогон до сайтов {GREEN, review, `review:<role>`, `plan
  --full`, `plan --gated`, `plan` интерактивный, `review-pr fix`, `doctor`
  (исполнение пробы, provenance `doctor:execute`)}; двойник store.
- **When** прогнана каждая клетка матрицы.
- **Then** в store есть call record, адресуемый `run_id/call_id`, с
  provenance сайта, outcome клетки, стоимостью (число или `null`, никогда
  `0.0` за неизвестную), bounded/redacted prompt и result, SHA-256 и
  размером полного содержимого каждого.
- **And** клетки `TASK_BLOCKED`, timeout, `INFRASTRUCTURE` и `is_error` при
  exit 0 оставляют record так же, как success: `post_review` не является
  каналом evidence, и отсутствие успешного completion-path не отменяет
  публикацию.
- **And** число call records равно числу `spawn` двойника `Popen` во всей
  матрице.
- **And** сайты RED authoring и RED agent round (#220) в матрицу FR-06 не
  входят, но не остаются без record: их call-start и `call_id` в
  `agent_calls` предъявляет BEH-05, исход каждого вызова — BEH-08 и BEH-10
  (ровно один call-result на `call_id`), а результат RED как факт задачи —
  строки `red_checkpoints` в экспорте BEH-23.

#### BEH-23: Terminal attempt экспортирует свои строки JSONL, и только свои
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_evidence_every_outcome.py`
- **Given** две задачи в одном namespace под `execution_mode: tdd`, у каждой
  RED checkpoint, claims, gate verdicts, verify evidence, waiver и
  authorization; TASK-001 завершается `done`, TASK-002 — `failed`, TASK-003
  — `blocked`.
- **When** каждая достигла terminal attempt.
- **Then** для каждой опубликован JSONL-экспорт, валидный по
  `schemas/evidence-record.schema.json`, со строками этой задачи/attempt-а
  из `attempts`, `red_checkpoints`, `tdd_claims`, `tdd_phases`,
  `tdd_remedies`, `phase_waivers`/`waivers_applied`,
  `budget_authorizations`, `gate_verdicts`, `verify_evidence`,
  `agent_calls`; каждая строка несёт namespace, task, attempt,
  `config_hash`, actor, reason, referenced SHA/blob, timestamp,
  supersession status.
- **And** в экспорте TASK-001 нет ни одной строки TASK-002 и TASK-003.
- **And** для `review-pr` экспорт строк `pr_*` происходит при завершении
  раунда, а не на каждом comment.
- **And** с bundle того же `run_id` уезжают две корроборирующие записи,
  которых требует инвентарь `docs/architecture.md`: срез task change history
  за этот прогон (строки, дописанные в `.task-history.log` между стартом и
  закрытием прогона) и срез compliance audit-trail (строки с этим `run_id`),
  когда источники есть; при выключенном аудите и отсутствующей истории
  bundle обходится без них и прогон не отказывает. Ни одна из двух записей не
  содержит строк чужого прогона, обе проходят redaction и границы размера,
  и ни `evidence`, ни `restore` не выводят из них статус задачи — они
  корроборируют ledger, а не заменяют его.

#### BEH-24: Планирование получает ledger-identity и отдельную строку `planning` в `costs`
`traces: [FR-06, FR-01]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_planning_has_ledger_identity.py`
- **Given** проект с одной выполненной задачей известной стоимости; fake
  CLI, сообщающий стоимость.
- **When** выполнены `spec-runner plan --full "…"` и `spec-runner plan
  --gated --stage requirements "…"`, затем `spec-runner costs` и `costs
  --json`.
- **Then** `plan --full` оставил три call records с provenance
  `plan:requirements`, `plan:design`, `plan:tasks` и **без задачи**;
  `plan --gated` — один с `plan:requirements`; у каждого `run_id` своего
  invocation и свой `call_id`.
- **And** интерактивный `spec-runner plan "…"`, прогнанный тем же fake CLI на
  один круг, оставил свой call record с provenance `plan:interactive` и
  тоже без задачи: третий платный путь `cli_plan.py` получает ledger-identity
  наравне с двумя флаговыми, а не остаётся вне ledger-а.
- **And** «без задачи» наблюдается как **отсутствие задачи у строки**, а не
  как `NULL` в `agent_calls`: ни одна строка `agent_calls` после этих
  прогонов не добавилась, `task_cost` любой задачи не изменился, а строки
  планирования читаются своим ledger-ом (как `pr_agent_calls` у `review-pr`).
  Прогон, потребовавший `NULL` в `agent_calls.task_id`, — красный: столбец
  `NOT NULL`, и вставка исчезла бы warning-ом, оставив планирование вообще
  без записи.
- **And** `costs` показывает их суммой отдельной строкой «planning», по
  образцу `pr_cost_rows` (#218); `task_cost` выполненной задачи не
  изменился; `repo_total_cost` включает planning.
- **And** до PR `cli_plan.py` не писал ни одной строки ledger-а — тест
  фиксирует, что теперь путь планирования проходит через тот же seam, что
  и task-сайты (BEH-05).

#### BEH-25: Опубликованная запись не переписывается; исправление — новая запись со ссылкой
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_evidence_every_outcome.py`
- **Given** двойник store, реализующий контракт immutability (версия/ETag
  или отказ overwrite), и опубликованный call record.
- **When** тест пытается опубликовать запись с тем же ключом и другим
  содержимым; затем публикует «исправление» как новую запись с полем
  `supersedes` = ключ первой.
- **Then** первая попытка отвергнута; чтение по ключу возвращает исходную
  запись байт в байт.
- **And** вторая принята под новым ключом; `evidence <run_id>` показывает обе
  и помечает первую как superseded, не удаляя её.
- **And** контракт store-адаптера объявляет запрет overwrite; адаптер без
  него отклонён при загрузке config (совместно с BEH-28).

#### BEH-26: Bounded prompt и result сохраняют доказательство исходных байтов
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_bounded_evidence_logs.py`
- **Given** GREEN-вызов с prompt-ом 3 MiB (frozen-files block добавлен
  последним, #214) и fake CLI, отдающим result 10 MiB; контрольный вызов с
  prompt-ом и result-ом в бюджете.
- **When** call record опубликован.
- **Then** bounded prompt ≤ 1 MiB и содержит head **и** tail исходника (в
  tail — frozen-files block), marker `truncated`, `full_sha256` и
  `full_size`, равные SHA-256 и размеру исходного prompt-а; bounded result
  ≤ 4 MiB аналогично.
- **And** у контрольного вызова marker-а нет, `full_sha256` совпадает с
  digest bounded-копии, `full_size` равен её размеру.
- **And** усечение выполнено по образцу `prompts_log.bound`; полный текст
  остаётся в локальном prompt-артефакте, как сегодня.

#### BEH-27: Секреты не публикуются; единственный путь публикации проходит через redactor
`traces: [FR-06, FR-09]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_redaction_corpus.py`
- **Given** фикстура `tests/fixtures/secrets-corpus/` из 100 синтетических
  секретов известной формы (ключи провайдеров, токены forge, приватные
  ключи, `.env`-строки); prompt содержит первые 50, fake CLI печатает
  остальные 50 в result и stderr.
- **When** GREEN-вызов выполнен, call record и attempt export опубликованы.
- **Then** ни одно из 100 значений не встречается ни в одном файле,
  полученном двойником store (bundle, checkpoint, WIP artifact, spool);
  `full_sha256` и `full_size` соответствуют **исходному** (нередактированному)
  содержимому.
- **And** статический тест по образцу `PaidBinaryReached`: каждый вызов
  put store-адаптера проходит через redactor; прямой вызов put минуя его —
  красный тест.
- **And** `evidence <run_id>` показывает redacted копии; локальный
  prompt-артефакт (не публикуемый) содержит полный текст, как сегодня.

#### BEH-28: Адаптер store без TLS или шифрования в покое отклоняется при загрузке config
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_config.py`
- **Given** config с адаптером store, объявившим `tls: false` либо не
  объявившим шифрование в покое; второй config с адаптером, объявившим
  `tls: true` и managed encryption.
- **When** config загружен (`build_config`) и выполнен `spec-runner
  validate`.
- **Then** первый даёт `ConfigError` при загрузке и ошибку в `validate` с
  именем адаптера и недостающего свойства; `run` с ним не доходит до
  run-start.
- **And** второй загружается; шифрование и IAM исполняет store (OUT-03),
  spec-runner проверяет только объявление контракта.
- **And** `retention_days` вне 7–365 в том же config даёт отдельную
  `ConfigError` (BEH-42).

### G. Closure на каждом штатном завершении

#### BEH-29: Каждый orderly exit `run`, включая пути до attempt, оставляет ровно одну closure, и её kind выведен из кода выхода и исхода работы
`traces: [FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_closure_every_exit.py`
- **Given** конфигурации `run`, покрывающие каждую строку правила вывода
  (design § 6.3): все задачи `done`; нет ready-задач; `--dry-run`; `--task` с
  именем, которого нет в tasks.md; `validate` красный при старте;
  governance-гейт (`_enforce_spec_governance`); dirty-spec guard
  (`_enforce_clean_spec`); tracked-state-DB guard (#273); занятый lock;
  budget guard перед вызовом; неудовлетворённый gate под `review_policy:
  required`; отказ ack call-start двойником store; `session_timeout_minutes`,
  истёкший в цикле при невыполненных задачах; SIGTERM в цикле; исключение,
  поднятое двойником и не перехваченное handler-ом. Те же конфигурации «все
  задачи `done`», «занятый lock» и «SIGTERM» повторяются для `run --all
  --force`, где executor lock не берётся. Двойник store.
- **When** каждая конфигурация прогнана в отдельном invocation.
- **Then** у каждого `run_id` ровно одна closure, kind — один из пяти
  словаря `schemas/run-closure.schema.json` (`completed`, `refused`,
  `failed`, `interrupted`, `crashed`), `last_checkpoint_id` равен id
  последнего acknowledged checkpoint-а (или `null`, если mutation не было),
  exit code записан и равен фактическому.
- **And** kind соответствует правилу: `completed` — на конфигурациях с кодом
  0 и без невыполненной работы (все задачи `done`, нет ready-задач,
  `--dry-run`, несуществующий `--task`); `refused` — там, где ненулевой код
  сопровождается отказом правила в последнем неуспешном attempt-е
  (неудовлетворённый gate даёт `error_kind` `policy` — типизированный
  `Refusal` гейта, `gates.py:695`, `execution.py:289-290`; budget guard —
  `budget`); `failed` — на красном `validate`, четырёх гардах старта, отказе
  ack (`error_kind` `instrument`, exit 2) и на истёкшем session-таймере,
  оставившем выбранную задачу невыполненной; `interrupted` — на SIGTERM,
  который диспетчер видит по флагу `executor._shutdown_requested` (design
  § 6.3), хотя цикл `run` на нём делает `break` и выходит штатным кодом;
  `crashed` — на неперехваченном исключении.
- **And** `completed` не выдан ни одному прогону с невыполненной работой:
  конфигурация «неудовлетворённый gate под `review_policy: required`»
  предъявляется прямо — `run` выходит с кодом 1, потому что считает свой
  исход сам (`cli.py:1344-1371`), хотя персистированный
  `last_run_stop_reason` остался дефолтным `completed`; closure `completed`
  на ней — красный тест. Closure `last_run_stop_reason` не читает, и это
  наблюдается: `status` на этой конфигурации показывает `completed`, closure
  — нет.
- **And** для путей до attempt (no-ready, task-not-found, `--dry-run`,
  validation failure, governance-гейт, dirty-spec, tracked-state, lock занят)
  closure существует, хотя attempt не создан; run-start у них записан
  диспетчером `main()` **до** вызова handler-а и потому раньше любого из этих
  гардов. Для «lock занят» это означает обычную пару run-start + closure с
  одним `run_id` у двойника store; одиночный run-start без closure и, равно,
  отсутствие run-start для этого пути — красный тест.
- **And** `run --all --force`, который executor lock не берёт вовсе, даёт ту
  же пару run-start + closure на каждой из своих конфигураций. Конфигурация
  «занятый lock» под `--force` closure не даёт вовсе — lock не проверяется,
  прогон идёт, — и это наблюдается: `--force` остаётся обычным платящим
  прогоном с run-start, а не путём в обход контракта.
- **And** closure несёт `run_id`, `pipeline_id`, подкоманду, kind, reason,
  exit code, число open calls, `degraded`/spool status, timestamps start/end,
  `last_call_ids`/`attempt_ids`. `reason` не сверяется ни с каким словарём:
  проверяется только, что поле есть и что пустое значение допустимо.
- **And** сценарий покрывает `run`; десять платящих подкоманд вне его —
  BEH-46 (восемь самостоятельных) и BEH-11/BEH-42 (две формы `evidence`), и
  утверждение «каждый orderly exit» верно только вместе с ними: `run` — одна
  подкоманда из одиннадцати, а run-start и closure диспетчер пишет всем
  одиннадцати одинаково.

#### BEH-30: `kill -9` не оставляет closure, и читатели классифицируют прогон как crash/unknown
`traces: [FR-07, FR-09]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_closure_every_exit.py`
- **Given** прогон с fake CLI, которому после run-start и первого
  acknowledged checkpoint-а послан SIGKILL (или `os._exit` в seam-е).
- **When** выполнены `spec-runner evidence <run_id>` и `spec-runner restore
  <run_id> --into <dir> --experimental`.
- **Then** closure для `run_id` отсутствует; `evidence` показывает статус
  `crash/unknown`, никогда «пустой успех» или `completed`.
- **And** `restore` читает прогон как `crash/unknown`, восстанавливает с
  последнего acknowledged checkpoint-а и печатает следующий шаг только если
  open calls нет; иначе — `needs-human` (BEH-09).
- **And** `spec-runner status` в исходном каталоге (если он жив) не считает
  прогон завершённым и показывает его `run_id`.

#### BEH-31: Closure — последняя запись; отказ ack последнего checkpoint-а и повторная closure обрабатываются fail-closed
`traces: [FR-07, FR-03]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_closure_every_exit.py`
- **Given** двойник store, подтверждающий все checkpoints кроме последнего;
  второй двойник, отказывающий в записи closure; третий — исправный.
- **When** прогон завершён штатно с каждым двойником; с третьим тест затем
  пытается записать вторую closure для того же `run_id`.
- **Then** с первым closure записана с kind `failed`, `last_checkpoint_id`
  предыдущего acknowledged и exit code прогона 2; reason называет
  неподтверждённый checkpoint, но ни с каким словарём не сверяется.
- **And** со вторым stderr называет причину отказа записи closure; exit code
  прогона не улучшается из-за неё (остаётся кодом фактического исхода или
  становится 2, но не 0 при незаписанной closure).
- **And** с третьим повторная closure отвергнута, первая неизменна.
- **And** closure по времени позже последнего checkpoint-ack: журнал
  двойника не содержит публикации checkpoint-а после closure.

#### BEH-32: Kind closure выводит одна функция из кода выхода и исхода работы; ни один сайт выхода kind не выбирает
`traces: [FR-07]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_closure_every_exit.py`
- **Given** двойник handler-а, завершающийся каждым из шести способов
  правила вывода (design § 6.3): необработанным исключением; поднятым
  флагом сигнала `executor._shutdown_requested` (так его ставит
  `_signal_handler` на SIGINT/SIGTERM) и `KeyboardInterrupt`; ненулевым
  кодом при последнем неуспешном attempt-е с `error_kind` из отказного
  подмножества `ERROR_KINDS` (`policy`, `budget`, `blocked`, `hook_failure`,
  `harness_guard`); ненулевым
  кодом без такого attempt-а и с `error_kind` инструментального класса
  (`instrument`, `timeout`, `network`, `rate_limit`, `auth`, `api_error`,
  `cli_error`, `internal_error`, `interrupted`, `unknown`); кодом 0 при
  задаче этого invocation не в статусе `success` либо оставшемся open call;
  кодом 0 без невыполненной работы.
- **When** каждый способ прогнан в отдельном invocation.
- **Then** kinds — `crashed`, `interrupted`, `refused`, `failed`, `failed`,
  `completed` соответственно; строки правила применяются сверху вниз, первая
  подошедшая выигрывает, и `completed` не выдан ни на одной из первых пяти —
  красный тест на любой из них.
- **And** словарь kinds закрыт пятью значениями и пинован
  `schemas/run-closure.schema.json`: шестое значение не сериализуется.
- **And** правило живёт в одной функции, и ни один сайт выхода kind не
  выбирает: статический тест утверждает, что в `src/spec_runner/` нет
  `note_stop`, нет таблиц «подкоманда/сайт → причина closure», а
  `RUN_STOP_REASONS` (`cli.py:536-541`) содержит те же семь значений, что и
  до бандла.
- **And** `reason` словарём не является: closure с `reason`, которого нет ни
  в одном перечне, и closure с пустым `reason` обе валидны по схеме — отказ
  сериализации по reason есть красный тест.
- **And** exit code closure совпадает с фактическим кодом процесса; kind с
  ним не отождествляется. Различитель «работа плоха» (код 1) и «инструмент
  не смог» (код 2) остаётся в поле `exit_code`: оба дают kind `failed`, и
  требование разных kind-ов для них — красный тест.

#### BEH-46: Каждая платящая подкоманда вне `run` закрывается closure своего исхода, и ни один её нулевой код не выдаёт невыполненную работу за успех
`traces: [FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_closure_every_exit.py`
- **Given** восемь платящих подкоманд вне `run`, доставляемых как
  самостоятельные команды, — `retry`, `watch`, `doctor`, `plan`,
  `review-pr`, `tdd abandon/repair/resume/release`, `budget authorize`,
  `restore` — и по одной конфигурации на каждую строку правила вывода,
  достижимую у этой подкоманды: `retry` с задачей, закончившей `done`
  (код 0, работа выполнена), и `retry` с задачей, закончившей `blocked`
  (код 0, работа не выполнена); `watch`, остановленный
  `max_consecutive_failures` (код 0, работа не выполнена), и `watch` с
  красной pre-run validation (код 1, работа не выполнена); `doctor` с
  verdict `ready` (код 0) и с verdict
  `broken` (код 1); `plan` с usage-ошибкой (код 1), `plan --gated` с
  неодобренным upstream-ом (код 2, вызовов CLI ноль) и `plan`, у которого
  двойник провайдера поднял исключение сквозь handler; `review-pr` на draft
  PR (код 1) и с полным успехом (код 0); `tdd repair` без переустановленного
  red (код 2) и `tdd release` на повторе (код 0); `budget authorize` с
  `AuthorizationError` (код 1) и с записанным решением (код 0); `restore` на
  отказе проверок (код 1 и код 2) и на успешном применении (код 0); плюс
  SIGTERM, посланный `watch` в цикле. Двойник store и двойник провайдера.
- **When** каждая конфигурация прогнана в отдельном invocation.
- **Then** у каждого `run_id` ровно один run-start и ровно одна closure с
  kind по правилу, фактическим exit code, `run_id`, `pipeline_id` и
  подкомандой.
- **And** `completed` не выдан ни одному исходу с невыполненной работой, и
  это наблюдается прямо на двух конфигурациях, где код процесса лжёт:
  `retry` с задачей `blocked` (код 0) и `watch`, остановленный
  `max_consecutive_failures` (код 0), дают `failed`, потому что задача, по
  которой invocation записал attempt, не в статусе `success`. Closure
  `completed` на любой из двух — красный тест.
- **And** `completed` выдан ровно тем конфигурациям, у которых код 0 и
  невыполненной работы нет (`retry` с задачей `done`, `doctor` с verdict
  `ready`, `review-pr` с полным успехом, `tdd release` на повторе, `budget
  authorize` с записанным решением, `restore` на успешном применении).
- **And** `watch` с красной pre-run validation закрывается `failed` с кодом
  1 — тем же ответом, что `run` на том же дефекте спеки, и по той же
  причине: H-1 (`cli.py:905-919`) — «a silent `return` here exited 0 and
  orchestrators (Maestro) read that as workstream success». Прежняя
  редакция этого сценария классифицировала конфигурацию как `completed`,
  опираясь на код 0, который `cmd_watch` тогда и возвращал (`return` до
  цикла, attempt-ов нет): признак «делать было нечего» выдавался за
  прогон, который даже не начинался, и сценарий узаконивал в `watch`
  дефект, в `run` уже признанный дефектом. Это противоречило FR-07, а не
  только правилу § 6.3. Код 0 или kind `completed` на этой конфигурации —
  красный тест. Паритет предъявлен исполнением: обе подкоманды прогнаны
  на одной и той же красной спеке и обе обязаны выйти 1
  (`cli.py:1560-1567`; доставлено до этого бандла —
  `tests/test_exit_contract.py::TestRedPreRunValidationExit`).
- **And** ненулевой код без attempt-ов даёт `failed`, а не `refused`:
  `plan --gated` с неодобренным upstream-ом и `restore` на отказе
  needs-human обе закрываются `failed`, и различение «отказ правила» и
  «поломка инструмента» на них не утверждается — у этих подкоманд нет
  attempt-а, по которому диспетчер мог бы его увидеть (design § 6.3, «чего
  правило не различает»). Требование `refused` на любой из них — красный
  тест.
- **And** исключение, дошедшее сквозь handler `plan`, даёт `crashed`, а
  SIGTERM у `watch` — `interrupted` по флагу `executor._shutdown_requested`
  (цикл `watch` на нём делает `break` и возвращается с кодом 0,
  `cli.py:1617-1620`; kind даёт флаг, не код); ни та ни другая
  конфигурация не оставляет run-start без closure.
- **And** ни одна из восьми не персистит `last_run_stop_reason`
  (`state.set_meta` с этим ключом в их коде отсутствует — статический тест),
  и closure от него не зависит: reason этих подкоманд — свободная строка,
  которая может быть пустой.
- **And** `watch --tui` закрывается не позже выхода handler-а: остановка
  цикла в daemon-треде выхода handler-а не переживает, `RunContext` один на
  процесс, и одиночный run-start без closure у любой из восьми — красный
  тест.
- **And** оставшиеся две платящие формы — `evidence close-call` и `evidence
  purge` — сюда не входят и предъявлены там, где живут сами команды: BEH-11
  и BEH-42. Все одиннадцать позиций перечня FR-01 закрыты только тремя
  сценариями вместе.

### H. Аварийный spool при отказе DB

#### BEH-33: Отказ SQLite сохраняет mutation в spool; новый процесс доигрывает её в DB до любой работы
`traces: [FR-08]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_emergency_spool.py`
- **Given** двойник SQLite, роняющий запись, параметризованный по mutation:
  `record_attempt`, запись claims, budget authorization, gate verdict,
  `record_verify_evidence`, RED checkpoint, remedy, waiver; spool исправен.
- **When** mutation совершена; процесс завершён; в том же namespace запущен
  новый процесс (`spec-runner run --all` с fake CLI).
- **Then** spool содержит строку с `seq`, `run_id`, `namespace`,
  `task_id`/attempt, `table`, payload и SHA-256 строки, записанную с
  `fsync`; файл лежит в `.executor-*` поясе рядом с DB.
- **And** новый процесс после run-start и гардов старта, до выбора задачи,
  доигрывает spool в DB в порядке `seq`; `spec-runner status` / `tdd status` / `costs` показывают
  mutation; следующий checkpoint содержит её в DB snapshot; spool
  ротирован в архив с пометкой в manifest.
- **And** прежнее поведение «жить в памяти процесса» отсутствует:
  mutation, не подтверждённую ни DB, ни spool, ни один путь не считает
  записанной (BEH-35).

#### BEH-34: Replay идемпотентен, повреждённая строка spool останавливает старт
`traces: [FR-08]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_emergency_spool.py`
- **Given** spool из BEH-33 с тремя строками.
- **When** replay выполнен дважды подряд; затем в копии spool изменён один
  байт payload одной строки и запущен новый процесс.
- **Then** после двух replay число строк в DB равно числу после первого —
  дублей нет.
- **And** с повреждённой строкой replay отказывает, называя `seq` и
  ожидаемый/фактический SHA-256; прогон не стартует: `Refusal(kind=
  "instrument")`, exit 2, 0 `Popen`, closure `failed`.
- **And** решения принимаются по DB, не по spool (RK-03): пока replay не
  завершён, ни `get_next_tasks`, ни claims gate, ни budget guard не читают
  spool напрямую (статический тест: единственный читатель spool — replay).

#### BEH-35: Одновременный отказ DB и spool останавливает исполнение до следующего платного вызова
`traces: [FR-08, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_emergency_spool.py`
- **Given** двойники, роняющие DB и spool одновременно на записи attempt
  после GREEN TASK-001; вторая ready задача TASK-002; двойник store
  исправен; двойник `Popen`.
- **When** выполнен `spec-runner run --all`.
- **Then** после отказа двойник `Popen` не вызван ни разу (TASK-002 не
  начата); exit 2; closure kind `failed` с причиной,
  называющей обе неудачи.
- **And** если и store недоступен, stderr называет причину и exit code
  остаётся 2 — тихого продолжения нет.
- **And** `status` в следующем процессе показывает TASK-001 как незакрытую
  (без потерянного «успеха»), а не `done`.

### I. Read-surface по `run_id`

#### BEH-36: `evidence <run_id>` отвечает без клона, DB и Git, из одного `collect()`
`traces: [FR-09]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_evidence_read_surface.py`
- **Given** прогон из BEH-19 (checkpoint, closure `completed`, attempts с
  известной стоимостью); пустой каталог без клона и без `spec/`; доступ
  только к двойнику store.
- **When** из пустого каталога выполнены `spec-runner evidence <run_id>` и
  `spec-runner evidence <run_id> --json`.
- **Then** оба вывода содержат: run-start (подкоманда, policy identity,
  repository identity), статус `closed:completed`, последний acknowledged
  checkpoint (id, sequence, время, namespace), open calls (пусто), attempts
  (task, номер, outcome, стоимость или `unknown`, ссылки на call records),
  суммарную стоимость платных вызовов и — при наличии данных у store —
  стоимость хранения (CON-08).
- **And** `--json` валиден по `schemas/evidence-view.schema.json`;
  человеческий и JSON-вывод собраны из одного `collect()` (по образцу
  `tdd_status.py`): тест сравнивает значения полей обоих выводов.
- **And** двойник store не получил ни одного put: read-surface не пишет;
  команда не требует `project_root`, DB или Git (каталог остался пустым).

#### BEH-37: Read-surface называет crash/unknown, open call, legacy и недоступный store, но не решает за оператора
`traces: [FR-09, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_evidence_read_surface.py`
- **Given** четыре `run_id`: после `kill -9` (BEH-30); с open call (BEH-09);
  legacy (BEH-21 а); валидный, но store отвечает ошибкой.
- **When** для каждого выполнен `evidence <run_id>`.
- **Then** первый показан `crash/unknown`; второй перечисляет `call_id`,
  provenance, задачу и время call-start; третий отвечает «нет
  evidence-контракта» с перечнем недостающего; четвёртый — exit 2 с
  причиной, а не пустой отчёт.
- **And** следующий безопасный шаг показан как **рекомендация** restore-а с
  пометкой «не доказуемо: <причина>» там, где данных нет (например,
  «процесс жив — недоказуемо по lock-PID» для прогона без closure).
- **And** ответ ограничен одним `run_id`: агрегации по namespace или проекту
  нет (OUT-08).

#### BEH-38: Локальный `status` показывает `run_id` последнего прогона namespace-а
`traces: [FR-09, FR-01]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_cli_info.py`
- **Given** namespace с двумя завершёнными прогонами (A, затем B).
- **When** выполнены `spec-runner status` и `status --json`.
- **Then** человеческий вывод содержит строку с `run_id` B (full UUIDv4),
  JSON — ключ `run_id` = B и `pipeline_id`, если он был.
- **And** существующие ключи `status --json` сохранены; `schemas/status.schema.json`
  дополнена аддитивно.
- **And** для namespace без прогонов по новому контракту поле отсутствует
  либо `null` — не выдуманный id.

### J. Сквозные гарантии

#### BEH-39: Ни одна подтверждённая mutation не теряется на любой границе (fault-injection ×1000)
`traces: [FR-02, FR-03, FR-08]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_write_ahead_fault_injection.py`
- **Given** seam-ы с точками инъекции `os._exit` на границах: после
  run-start; после call-start до spawn; после spawn до результата; после
  результата до attempt; после attempt до checkpoint. Двойники store,
  SQLite и spool. Тест помечен `slow`, параметризован по границе, 1000
  повторений на границу.
- **When** каждая итерация запускает прогон, обрывает его в точке инъекции
  и затем запускает новый процесс в том же namespace.
- **Then** суммарно: 0 mutation, подтверждённых DB/spool/store и
  отсутствующих после рестарта; 0 `spawn` без предшествующего
  acknowledged call-start; 0 автоматических повторов open call (каждый
  open call виден как `needs-human`).
- **And** инъекция реализована двойниками и `os._exit` в seam-ах, не
  `sleep`-гонками; тест детерминирован по seed.
- **And** тест входит в `-m slow` и прогнан хотя бы раз перед снятием
  experimental (условие завершения требований).

#### BEH-40: Любой изменённый байт или отсутствующий обязательный файл — fail-closed до `Popen` и claims gate
`traces: [FR-03, FR-04, FR-05, FR-06, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_integrity_fail_closed.py`
- **Given** валидный checkpoint и bundle прогона BEH-19; параметризация по
  каждому файлу (DB snapshot, manifest, WIP artifact, spool, каждый call
  record, attempt export, closure) в двух режимах: один байт изменён;
  обязательный файл удалён (DB snapshot, manifest, WIP при `wip != none`,
  closure при `closed`).
- **When** для каждого варианта выполнены `restore`, следующий `run` в этом
  namespace (после restore из нетронутой копии) и `evidence`.
- **Then** все три отказывают, называя файл и ожидаемый/фактический digest;
  restore — до записи в каталог; `run` — до `Popen` и до
  `claims.check_claims` (двойники подтверждают 0 вызовов).
- **And** exit code — 2 у всех трёх: недоказанная целостность — вопрос без
  ответа, а не «нет» (как BEH-20 (2)/(7) и BEH-34); у `run` это
  `Refusal(kind="instrument")` с closure `failed`, у
  `restore --json` и `evidence --json` — `reason` с именем файла и обоими
  digest-ами.
- **And** изменённый manifest отвергнут по digest-у верхнего уровня, даже
  если все перечисленные в нём digests сходятся.
- **And** авто-«лечения» нет (OUT-09): ни один вариант не заканчивается
  успешным restore с предупреждением.

#### BEH-41: Ack и recovery укладываются в объявленные бюджеты, а не в CI-гейт
`traces: [FR-02, FR-05]`

- **checked_by**: `status: planned` `kind: manual` `owner: qa` `target: scripts/bench_durability.py`
- **Given** reference workload: одна задача с review, fake CLI, локальный
  адаптер store; reference-набор для restore (DB + WIP ≈ 10 MiB) в CI;
  набор ≈ 1 GiB для ручного drill-а.
- **When** прогнан бенчмарк-скрипт (не CI-обязательный) и CI-тест
  `test_restore_drill.py::test_validation_under_60s`.
- **Then** бенчмарк печатает call-start ack p95/p99 и время доступности
  checkpoint-а вне машины; результат сравнивается с NFR-02 (p95 ≤ 1 s,
  p99 ≤ 3 s, checkpoint p95 ≤ 60 s) и записывается в отчёт, не в
  assert CI.
- **And** CI-тест: валидация + вычисление следующего шага на
  reference-наборе ≤ 60 s (NFR-03).
- **And** 1 GiB drill выполнен вручную, время (цель ≤ 15 min при
  ≥ 100 Mbit/s) записано в отчёте drill-а в условии завершения (M-01).
- **And** таймаут ack конфигурируем и при превышении даёт BEH-06, не
  ожидание.

#### BEH-42: Retention вне 7–365 дней отклоняется; удаление оставляет audit-запись без содержимого
`traces: [FR-03, FR-06, FR-07]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/test_retention_policy.py`
- **Given** config с `retention_days: 3`, затем `400`, затем `30`; прогон с
  тремя checkpoint-ами (sequence 1–3) и closure.
- **When** config загружен и прогнан `validate`; затем санкционированное
  удаление промежуточных checkpoint-ов 1–2 (команда spec-runner или
  lifecycle store — Q-11) и удаление evidence по истечении retention.
- **Then** `3` и `400` дают `ConfigError` при загрузке и ошибку `validate`;
  `30` принят.
- **And** после удаления checkpoint-ов 1–2 restore из checkpoint-а 3
  проходит (RK-05); удалённые записи заменены audit-записью с `run_id`,
  ids, actor, reason и временем — без payload.
- **And** legal hold и правила удаления store не обходятся (OUT-10): при
  отказе store в удалении команда сообщает отказ, не удаляет локальную
  копию и не пишет audit-запись об удалении.
- **And** `evidence purge` меняет опубликованное состояние и потому — как и
  `evidence close-call` (BEH-11) — оставляет свою пару run-start + closure на
  каждом из трёх исходов, и kind выводит диспетчер по коду выхода:
  `completed`, когда истёкших объектов нет (код 0, работы не было) и после
  удаления с записью `deletions/<ts>.json` (код 0); `failed` при отказе store
  в `delete` (код 1) и при недоступном store (код 2). Различие «удалять было
  нечего» и «удалено» читается по полям записи, а не по kind-у. Open call
  она при этом не закрывает — после неё open call прежнего прогона
  предъявляется как раньше.

#### BEH-43: Ни байта checkpoint/evidence/spool в продуктовом Git после всех E2E
`traces: [FR-03, FR-04, FR-06]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/test_restore_drill.py`
- **Given** полный прогон сценариев BEH-01…BEH-42 на проектах с git
  automation, включая отказные ветки и degraded mode.
- **When** после каждого E2E выполнен `git status --porcelain` продуктового
  репо проекта и `rglob(".executor-*")`.
- **Then** `git status --porcelain` не содержит ни checkpoint, ни evidence,
  ни spool, ни WIP artifact: локальные копии лежат под `.executor-*` и
  покрыты `spec/.gitignore` (CON-04).
- **And** единственные новые tracked-изменения — те, что делает сам harness
  сегодня (status flips `tasks.md`, коммиты задач) плюс `tdd_namespace`,
  записанный restore-ом (BEH-20). Сам `spec/.gitignore` — harness-owned
  файл (#62/#96): `git_ops.ensure_runtime_gitignore` дописывает его перед
  каждой задачей, auto-commit его исключает, и в проекте, где его не
  трекают, он законно стоит в `porcelain` как untracked — это не находка
  сценария; находка — любой файл под `.executor-*` в том же выводе.
- **And** `git_ops.tracked_state_paths` (#273) не находит tracked
  `.executor-*` файлов ни в одном проекте после прогона.

#### BEH-44: Тесты не вызывают платного агента, существующие контракты меняются только аддитивно
`traces: [FR-01, FR-02]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/test_harness_guards.py`
- **Given** conftest-пояс `PaidBinaryReached` активен; все новые тесты
  используют `tests/fixtures/fake_claude.sh` или специализированные
  двойники.
- **When** прогнан `uv run pytest tests/ -q -m "not slow"`, затем
  `-m slow` для fault-injection.
- **Then** ни один тест не поднимает `PaidBinaryReached`; ни один не
  требует сети к реальному store (локальный адаптер / двойник).
- **And** `tests/test_json_result_contract.py`, schema-тесты
  `.executor-state.db`, `test_state.py`, `test_costs.py` зелёные без
  изменения ожиданий, кроме аддитивных полей `run_id` / `call_id` /
  `pipeline_id`; любое иное изменение ожидания существующего теста —
  находка ревью.
- **And** seam call-start не создал второго пути к бинарю провайдера: пояс
  `PaidBinaryReached` продолжает ловить каждый сайт (BEH-05).
- **And** в дереве не осталось и **прежнего** второго пути: статический тест
  утверждает, что `asyncio.create_subprocess_exec` не вызывается ни из одного
  модуля `src/spec_runner/`, а `run_claude_async` отсутствует и в
  `runner.py`, и в `__all__` пакета. Пояс продолжает перечислять
  `asyncio.create_subprocess_exec` среди перехватываемых точек — он ловит
  будущий регресс, а не сегодняшний код.

#### BEH-45: Документация, схемы и статус experimental говорят то же, что код; соседям объявлен контракт
`traces: [FR-01, FR-05, FR-09]`

- **checked_by**: `status: planned` `kind: manual` `owner: qa` `target: docs/architecture.md`
- **Given** PR, реализующие FR-01…FR-09.
- **When** ревьюер читает `docs/architecture.md` («Runtime-state inventory
  and delivery policy (#478)»), `docs/state-schema.md`, `schemas/`,
  CHANGELOG и README в тех же PR.
- **Then** `docs/architecture.md` описывает контракт checkpoint/evidence/
  closure и «operational minimum» для legacy; `schemas/` содержит
  версионированные `checkpoint-manifest`, `evidence-record`, `run-closure`,
  `restore-result`, `evidence-view` схемы; `docs/state-schema.md` описывает
  аддитивные столбцы и minor bump.
- **And** CHANGELOG под Unreleased называет статус `experimental` для
  `restore` до приёмки FR-01–FR-08 (CON-01) и ссылается на #480; после
  приёмки — запись о снятии статуса (BEH-21 б).
- **And** контракт `run_id`/`pipeline_id` объявлен соседям (devtools,
  Maestro) issue с `slug:` + `from:` (ADR-ECO-006) либо записью в
  `../prograph-vault/authored/notes/` — без правки их файлов; #480 закрыт
  ссылкой на PR с evidence; пункт `runtime-state-artifact-export` в
  `TODO.md` закрыт.
- **And** если владелец вырезает FR-09 при сокращении объёма, BEH-36…BEH-38
  снимаются с записью в CHANGELOG; BEH-30 и BEH-37 при этом теряют только
  ветку `evidence`, restore-ветка остаётся.

## Матрица трассируемости

| Behaviour | Functional requirements |
|---|---|
| BEH-01 | FR-01 |
| BEH-02 | FR-01 |
| BEH-03 | FR-01 |
| BEH-04 | FR-01, FR-07 |
| BEH-05 | FR-02 |
| BEH-06 | FR-02, FR-07 |
| BEH-07 | FR-02 |
| BEH-08 | FR-02, FR-06 |
| BEH-09 | FR-02, FR-05 |
| BEH-10 | FR-02, FR-06 |
| BEH-11 | FR-02 |
| BEH-12 | FR-03 |
| BEH-13 | FR-03 |
| BEH-14 | FR-03, FR-04 |
| BEH-15 | FR-03, FR-08 |
| BEH-16 | FR-04 |
| BEH-17 | FR-04, FR-05 |
| BEH-18 | FR-04 |
| BEH-19 | FR-05, FR-04 |
| BEH-20 | FR-05 |
| BEH-21 | FR-05 |
| BEH-22 | FR-06, FR-02 |
| BEH-23 | FR-06 |
| BEH-24 | FR-06, FR-01 |
| BEH-25 | FR-06 |
| BEH-26 | FR-06 |
| BEH-27 | FR-06, FR-09 |
| BEH-28 | FR-06 |
| BEH-29 | FR-07 |
| BEH-30 | FR-07, FR-09 |
| BEH-31 | FR-07, FR-03 |
| BEH-32 | FR-07 |
| BEH-33 | FR-08 |
| BEH-34 | FR-08 |
| BEH-35 | FR-08, FR-07 |
| BEH-36 | FR-09 |
| BEH-37 | FR-09, FR-07 |
| BEH-38 | FR-09, FR-01 |
| BEH-39 | FR-02, FR-03, FR-08 |
| BEH-40 | FR-03, FR-04, FR-05, FR-06, FR-07 |
| BEH-41 | FR-02, FR-05 |
| BEH-42 | FR-03, FR-06, FR-07 |
| BEH-43 | FR-03, FR-04, FR-06 |
| BEH-44 | FR-01, FR-02 |
| BEH-45 | FR-01, FR-05, FR-09 |
| BEH-46 | FR-07 |
| BEH-47 | FR-02, FR-03, FR-06 |
| BEH-48 | FR-03, FR-05, FR-07 |

Обратная трассировка по функциональным требованиям:

| Requirement | Behaviours |
|---|---|
| FR-01 | BEH-01, BEH-02, BEH-03, BEH-04, BEH-24, BEH-38, BEH-44, BEH-45 |
| FR-02 | BEH-05, BEH-06, BEH-07, BEH-08, BEH-09, BEH-10, BEH-11, BEH-22, BEH-39, BEH-41, BEH-44, BEH-47 |
| FR-03 | BEH-12, BEH-13, BEH-14, BEH-15, BEH-31, BEH-39, BEH-40, BEH-42, BEH-43, BEH-47, BEH-48 |
| FR-04 | BEH-14, BEH-16, BEH-17, BEH-18, BEH-19, BEH-40, BEH-43 |
| FR-05 | BEH-09, BEH-17, BEH-19, BEH-20, BEH-21, BEH-40, BEH-41, BEH-45, BEH-48 |
| FR-06 | BEH-08, BEH-10, BEH-22, BEH-23, BEH-24, BEH-25, BEH-26, BEH-27, BEH-28, BEH-40, BEH-42, BEH-43, BEH-47 |
| FR-07 | BEH-04, BEH-06, BEH-29, BEH-30, BEH-31, BEH-32, BEH-35, BEH-37, BEH-40, BEH-42, BEH-46, BEH-48 |
| FR-08 | BEH-15, BEH-33, BEH-34, BEH-35, BEH-39 |
| FR-09 | BEH-27, BEH-30, BEH-36, BEH-37, BEH-38, BEH-45 |

Все FR-01–FR-09 покрыты хотя бы одним сценарием; сценариев без FR нет.

Нефункциональные требования закреплены наблюдаемыми результатами сценариев
без введения дополнительных идентификаторов трассировки: NFR-01 (RPO 0 для
подтверждённых mutation, остановка на текущей границе) — BEH-33, BEH-35 и
сводно BEH-39; NFR-02 (ack p95/p99, checkpoint ≤ 60 s, конфигурируемый
таймаут ack) — BEH-06 и BEH-41; NFR-03 (recovery ≤ 15 min, валидация
≤ 60 s) — BEH-41; NFR-04 (SHA-256 на каждом файле, fail-closed до restore,
paid call и claims gate) — BEH-14, BEH-18, BEH-20 и сводно BEH-40; NFR-05
(redaction до публикации, единственный путь через redactor, TLS/шифрование
у адаптера) — BEH-27 и BEH-28; NFR-06 (1 MiB / 4 MiB, head+tail, digest,
размер, marker) — BEH-26; NFR-07 (retention 7–365, audit-запись удаления,
промежуточные checkpoints удаляемы) — BEH-42.

Ограничения и границы брифа предъявляются сценариями: CON-01 (experimental
до приёмки) — BEH-21, BEH-45; CON-02 (аддитивные столбцы, один seam) —
BEH-03, BEH-05, BEH-44; CON-03 (open call = unknown, решение человека) —
BEH-09, BEH-11; CON-04 (ни байта в продуктовый Git) — BEH-43; CON-05
(конечные команды без control-plane) — BEH-11, BEH-19, BEH-36; CON-06
(без локальных фактов) — BEH-14, BEH-17; CON-07 (legacy fail-closed) —
BEH-21, BEH-37; CON-08 (стоимость хранения по `run_id`) — BEH-36, BEH-42;
OUT-06 (правила claims не пересочинены) — BEH-19; OUT-08 (один `run_id`) —
BEH-37; OUT-09 (нет авто-лечения) — BEH-18, BEH-40; OUT-10 (legal hold) —
BEH-42.

Метрики требований предъявляются сценариями: M-01/M-05 (restore-drill, 0
потерь, 0 повторных вызовов) — BEH-16, BEH-19, BEH-39; M-02 (open call →
`needs-human` до повтора) — BEH-09; M-03 (ровно один call-start и
однозначный исход на `call_id`) — BEH-05, BEH-10, BEH-22; M-04 (closure на
каждом orderly exit, crash/unknown без неё) — BEH-29, BEH-30; M-06
(аудитор без каталога определяет результат, стоимость, шаг или причину
недоказуемости) — BEH-36, BEH-37; M-07 (0 байт в Git, 0 утечек, 0
продолжений после несовпадения) — BEH-20, BEH-27, BEH-40, BEH-43.

## Границы спецификации

Сценарии не предписывают решений, переданных design и product
открытыми вопросами требований (§10). Канал ack call-start (Q-02): BEH-05,
BEH-06 и BEH-39 проверяют факт «запись подтверждена раньше `spawn`» через
двойник того канала, который зафиксирует design; если design примет
spool-only режим, он получает пометку в closure и отдельную ветку BEH-29.
Формат WIP artifact (Q-03): BEH-16 проверяет байт-идентичное восстановление
через `git`, а не формат носителя. Доставка checkpoint-а (Q-05): BEH-13
считает checkpoint «полученным», когда двойник store его acknowledged, и не
различает синхронную и асинхронную доставку; BEH-31 фиксирует лишь, что
closure ссылается на acknowledged id и не ждёт без предела. Место seam-а
(Q-06): BEH-05 и BEH-44 проверяют единственность пути статически, где бы он
ни жил. Состав policy identity (Q-07): BEH-20 подменяет `review_policy` как
заведомо policy-relevant ключ; расширение состава добавляет параметры в тот
же тест. Движок redaction (Q-08): BEH-27 проверяет корпус и единственность
пути, не паттерны. Запись `tdd_namespace` restore-ом (Q-09): BEH-20
фиксирует рабочее допущение требований; если владелец выберет отказ с
инструкцией, ветка заменяется на отказ с текстом «объявите
`tdd_namespace: <value>`». Приоритет NFR-02/NFR-03 (Q-10): BEH-41 намеренно
`manual`/бенчмарк, кроме CI-половины ≤ 60 s; поднятие до `Must` переводит
его в `integration`. Исполнитель retention (Q-11): BEH-42 допускает и
команду, и lifecycle store. Источник open calls при старте `run` (Q-12):
BEH-09 требует обнаружения в новом процессе и каталоге, не называя, читает
ли `run` store или DB.

Имена команд `restore`, `evidence`, `evidence close-call` и флаги `--into`,
`--experimental` — рабочие имена требований (Q-04); design вправе
переименовать при сохранении семантики exit-кодов и отказов. Имена тестовых
файлов в `checked_by` взяты из §9 требований как ожидание, не предписание.

Численные значения объявляются upstream'ом: 1 MiB / 4 MiB (BEH-26), 1000
повторений (BEH-39), p95/p99 и 60 s (BEH-41), 7–365 дней и 30/180 дней
retention (BEH-42), 100 секретов (BEH-27), 20/10 прогонов drill-матрицы
(условие завершения требований) — сценарии проверяют против объявленных
значений, а не констант, зашитых здесь.

Вне спецификации остаются границы, подтверждённые upstream'ом: автоматическое
продолжение после restore и `run --resume` (CON-05, Q-04 — продолжение
всегда отдельный `run`); retry open call без человека (OUT-02); собственное
хранилище, IAM, KMS, retention-service (OUT-03); копии pushed-объектов,
virtualenv, кэшей, образов ОС (OUT-04, OUT-05); изменение правил claims /
TDD / waiver / remedy / budget / review (OUT-06); миграция legacy-прогонов
(OUT-07 брифа, в требованиях — CON-07); межпроектные отчёты и биллинг
(OUT-08); авто-лечение повреждённого артефакта (OUT-09); обход retention и
legal hold (OUT-10); правка соседних репозиториев — только issue/handoff
(BEH-45).

### Замечания к upstream

Правка требований — вне прав этого документа (гейт doc-scope, §6 SPEC-002);
ниже — что стоит поправить в узле требований бандла (10-requirements.md)
при его следующей редакции, чтобы бандл читался без оговорок.

- OUT-07 определён в брифе бандла (00-discovery/brief.md), а в требованиях
  присутствует только через оговорку §7 «OUT-01…OUT-10 брифа без
  изменений»; остальные OUT-id, на которые ссылается этот документ, в
  требованиях проговорены явно. Здесь OUT-07 цитируется вместе с CON-07,
  который требования формулируют сами.
