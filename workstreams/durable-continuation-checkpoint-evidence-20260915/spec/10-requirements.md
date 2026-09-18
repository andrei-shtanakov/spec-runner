---
spec_stage: requirements
status: draft
owner_role: product
traces_to:
- charter
upstream_hashes:
  charter: 54de41d055b252400d8bf0c4622e427eccf84998
---

# Requirements — Durable continuation checkpoint и evidence для run/call/attempt (spec-runner#480)

Стадия `requirements` governance-бандла
`workstreams/durable-continuation-checkpoint-evidence-20260915/`. Уточняет
требования чартера до проверяемых утверждений: каждое FR/NFR несёт приоритет,
описание, уточнения, критерии приёмки и трассировку к целям, персонам, jobs и
метрикам customer-брифа. Идентификаторы FR-01…FR-09 и NFR-01…NFR-07 взяты из
`00-discovery/brief.md` (customer-фрейм, blob `3f6e9807…`) без изменений;
формулировки заголовков и приёмки брифа сохранены и только уточнены.
Владение и retention объектов runtime-state остаются в SSOT
`docs/architecture.md#runtime-state-inventory-and-delivery-policy-478`; этот
документ её не пересказывает.

## 1. Постановка

Инвариант, принятый инвентаризацией #478: **факт, необходимый для
продолжения или аудита прогона, не существует только на машине оператора.**
Сегодня он не выполнен. `tasks.md` в Git восстанавливает очередь, но не
восстанавливает claims, RED checkpoints, verify-evidence, gate verdicts,
remedies, waivers, budget authorizations, review-loop state, стоимость и
выходы неудачных вызовов — всё это живёт в `spec/.executor-state.db` и
`spec/.executor-logs/` и никуда не доставляется.

Наблюдаемые дефекты, каждый из которых закрывается требованием ниже:

- три несвязанных идентификатора прогона — обрезанный `uuid4().hex[:8]` в
  structlog (`cli.py:2504`), собственный UUID `AuditLogger`
  (`audit_log.py:120`), ULID `pipeline_id` в `obs.init_logging` (`obs.py:250`);
  ни один не попадает в `attempts`, `agent_calls` или prompt-артефакты (FR-01);
- ledger `agent_calls` (`state.py:1900`) и `record_attempt` пишутся **после**
  возврата subprocess: падение между стартом провайдера и записью не оставляет
  следа, следующий запуск платит повторно (FR-02);
- `cli_plan.py` не пишет ни одной строки ledger-а — стоимость `plan --full` и
  gated planning не атрибутируема (FR-06);
- degraded mode (`state.py:2261`) держит attempt только в памяти процесса
  (FR-08);
- `tdd.resolve_namespace` (`tdd.py:219`) без `tdd_namespace` хеширует
  абсолютный `project_root`: перенос каталога делает active claims невидимыми
  (FR-05);
- `hooks.rescue_uncommitted` (`hooks.py:132`) прячет WIP в локальный stash,
  local-only commits живут в одном клоне (FR-04);
- ни один orderly exit не оставляет записи вне stderr (FR-07).

Форма решения задана владельцем (CON-02…CON-06): **continuation checkpoint**
(приватный консистентный снимок + Git-материал и WIP) и **evidence bundle**
(immutable append-only след каждого платного вызова, terminal attempt и
закрытия прогона), связанные одним каноническим `run_id`; write-ahead
протокол «запись раньше траты»; restore по `run_id` конечной командой.

## 2. Цели, персоны, jobs (из customer-брифа)

### Цели

- **G-01** Безопасно продолжить или проверить прерванный прогон с другой
  машины без повторения оплаченных вызовов и без утраты фактов, определяющих
  допустимый следующий шаг.
- **G-02** Адресуемый вне машины evidence-след каждого платного вызова и
  каждого завершения прогона, включая failed, blocked, early-stop и
  crash-unknown пути.
- **G-03** Восстановление и независимый аудит по `run_id` — штатная и
  однозначная операция после полной потери исходного рабочего каталога.

### Персоны

- **P-01** Оператор spec-runner (запускает, наблюдает, возобновляет) — primary.
- **P-02** Владелец репозитория/workstream-а — платит за вызовы, утверждает
  budget/waiver/remedy-решения.
- **P-03** Независимый ревьюер/аудитор — без доступа к исходной машине.
- **P-04** Владелец платформы и безопасности — доступ, redaction, retention.

### Jobs-to-be-done

- **J-01** Прерванный (машина, терминал, CI-runner) прогон восстановить с
  последней подтверждённой границы без молчаливого повтора оплаченного вызова.
  traces: [G-01, G-03]
- **J-02** При передаче workstream-а или переносе каталога восстановить
  continuation-state и следующий допустимый шаг по `run_id`, не реконструируя
  claims и authority вручную. traces: [G-01, G-03]
- **J-03** Проверяя расходы и исходы за неделю, получить полный evidence-след
  success/failed/blocked/refusal/crash-unknown путей, чтобы подтвердить
  стоимость и обоснованность каждого продолжения. traces: [G-02, G-03]

## 3. Термины

- **`run_id`** — full UUIDv4, созданный ровно один раз на CLI-invocation в
  `main()`, до диспетчеризации подкоманды. Ключ всех артефактов workstream-а.
- **`pipeline_id`** — внешний родительский correlation id
  (`ORCHESTRA_PIPELINE_ID` или сгенерированный ULID, #482). Может группировать
  несколько invocation-ов; хранится **рядом** с `run_id`, никогда его не
  заменяет.
- **`call_id`** — full UUIDv4 на каждый платный subprocess; уникален внутри
  прогона и между прогонами.
- **Платный subprocess (paid call)** — запуск CLI провайдера с prompt-ом.
  Полный перечень сайтов: RED authoring, RED agent round (#220), GREEN,
  review, review-роли, `plan --full` (каждый вызов стадии), `plan --gated`,
  `plan "<описание>"` (интерактивный цикл, **каждый круг** — отдельный
  платный вызов), `review-pr` verify и fix, `doctor`. Бриф в FR-06 называет
  минимум (task execution, review, `plan --full`, gated planning); чартер
  расширяет до всех сайтов — требования принимают полный перечень.
  Интерактивный `plan` назван здесь отдельно, потому что это третий,
  самостоятельный путь к бинарю провайдера в `cli_plan.py` (не `--full` и не
  `--gated`), достижимый любым `spec-runner plan "<описание>"` без флагов;
  умолчание «раз это `plan`, значит он покрыт строкой `plan --full`» — ровно
  тот обход seam-а, который запрещает FR-02.
- **Provenance / stage** — существующее поле `provenance` (`red`, `green`,
  `review`, `review:<role>`), расширенное значениями для планирования
  (`plan:<stage>` для `plan --full` и `plan --gated`, `plan:interactive` для
  интерактивного цикла), `review-pr` (`review-pr:verify`, `review-pr:fix`) и
  `doctor`. Одно поле, один словарь.
- **Policy identity** — то, что делает call-start сравнимым при restore:
  `config_hash` над `gates.POLICY_KEYS`, effective TDD namespace и версия
  контракта артефактов. Точный состав — вход design (Q-07).
- **Call-start** — durable-запись намерения запустить платный subprocess:
  `run_id`, `call_id`, provenance, policy identity, optional `task_id` /
  номер attempt, digest redacted prompt-а, timestamp. Существует **до**
  `Popen`.
- **Call-result** — отдельная durable-запись исхода того же `call_id`:
  outcome (`success` / `failed` / `blocked` / `timeout` /
  `infrastructure_error`), стоимость либо явный `unknown`, bounded/redacted
  result и его digest, return code.
- **Open call** — call-start без call-result. Семантика: «мог быть запущен и
  оплачен» — `unknown`, никогда «не было».
- **Acknowledgement (ack)** — подтверждение durable-канала, что запись
  сохранена. Какой канал имеет право подтверждать call-start — вход design
  (Q-02); требование — без ack процесс не стартует.
- **Run-start** — durable-запись начала прогона (`run_id`, `pipeline_id`,
  подкоманда, policy identity, версия контракта, repository identity),
  выполняемая в `main()` до диспетчеризации handler-а платящей подкоманды.
- **Run-closure** — отдельная immutable запись штатного завершения прогона
  (§FR-07). Run-start без closure = crash/unknown.
- **Continuation-relevant mutation** — запись, без которой следующий шаг
  либо повторяет работу, либо теряет активное ограничение: строки
  `attempts`, `red_checkpoints`, `tdd_claims` (запись и release),
  `tdd_phases`, `verify_evidence`, `gate_verdicts`, `phase_waivers` /
  `waivers_applied`, `tdd_remedies`, `budget_authorizations`, `pr_*`, а также
  harness-written status flips `tasks.md` (#192).
- **Continuation checkpoint** — приватный консистентный снимок state DB
  (SQLite backup API или эквивалентный транзакционный snapshot, включающий
  WAL-only страницы) + версионированный **manifest** + Git-материал и WIP.
- **Manifest** — версионированный файл checkpoint-а: contract version,
  `run_id`, `pipeline_id`, repository identity, workstream (namespace,
  `spec_prefix` / `change_id`), ref/HEAD, published-ref reachability,
  `config_hash`, effective TDD namespace и способ получения
  (`declared` / `computed`), join keys (последние ids attempt/call/closure),
  SHA-256 всех файлов, явный перечень исключённых объектов.
- **WIP artifact** — переносимый носитель Git-материала, не достижимого из
  published ref: local-only commits, dirty/untracked work, runner-created
  rescue stash. Формат — вход design (Q-03).
- **Evidence bundle** — immutable, append-only набор записей под `run_id`:
  run-start, call-start/call-result каждого платного вызова, logical
  append-only export строк terminal attempt-а, run-closure.
- **Emergency spool** — durable append-log, не зависящий от SQLite, для
  mutation, которые DB не подтвердила.
- **Artifact store** — приватное авторизованное хранилище с ролевым доступом,
  redaction, retention 7–365 дней; интегрируется по публичному контракту
  (put/get/list/delete + acl). Первая реализация — вход product (Q-01).
- **Durable boundary** — точка, в которой все continuation-relevant mutation
  подтверждены и последний checkpoint acknowledged. Только с неё прогон
  «можно продолжить».
- **Legacy run** — прогон, начатый до нового контракта: нет run-start,
  manifest или call records нужной версии.
- **Restore** — конечная операторская команда, восстанавливающая прогон по
  `run_id` в новом абсолютном пути и вычисляющая следующий безопасный шаг,
  либо останавливающаяся `needs-human` с точной причиной.

## 4. Функциональные требования

#### FR-01: Назначать каждому CLI-прогону один глобально уникальный run_id и связывать с ним все вызовы, attempts, checkpoints, evidence и завершение

**Priority**: Must

Один full UUIDv4 `run_id` создаётся ровно один раз на invocation в `main()`
и передаётся **без изменения** во все каналы: structlog/OTel, `AuditLogger`,
`attempts`, `agent_calls`, `pr_agent_calls`, prompt-артефакты, checkpoint
manifest, evidence bundle и run-closure. `pipeline_id` хранится отдельным
полем рядом.

Уточнения:

- Существующий обрезанный display-id (`cli.py:2504`) и независимый audit
  UUID (`audit_log.py:120`) **заменяются**, не дублируются: structlog
  получает полный `run_id`; `AuditLogger` принимает `run_id` извне и не
  чеканит свой. Человеческий вывод вправе показывать сокращение, поле — нет.
- Ledger-таблицы `attempts`, `agent_calls`, `pr_agent_calls` получают
  аддитивные столбцы `run_id` и (для call-ledger-ов) `call_id`; для строк,
  записанных до контракта, значение `NULL`. Миграция аддитивна; описывается
  в `docs/state-schema.md` и `schemas/executor-state.schema.json`; версия
  контракта состояния — minor bump (инвариант 12).
- `--json-result` и `status --json` получают аддитивное поле `run_id`
  (и `pipeline_id`, когда он есть); существующие ключи и их семантика не
  меняются; `tests/test_json_result_contract.py` и golden-фикстуры
  обновляются только добавлением.
- `run_id` есть у **каждой** подкоманды (для логов), но run-start/closure
  (FR-07) пишут только подкоманды, способные платить или менять
  continuation-state: `run`, `retry`, `watch`, `plan`, `review-pr`, `doctor`,
  `tdd abandon/repair/resume/release`, `budget authorize`, `restore`,
  `evidence close-call`, `evidence purge`. Read-only команды (`status`,
  `costs`, `validate`, `report`, `evidence <run_id>`) run-start не пишут.
  Две последние платного вызова не делают и попадают сюда по второй половине
  критерия: `close-call` возвращает задачу в выбираемые, `purge` удаляет
  опубликованные объекты — тот же класс, что уже привёл в перечень
  неплатящие `tdd abandon/…` и `budget authorize`. Read-only — подкоманда
  `evidence <run_id>`, а не группа `evidence` целиком.
- **Носитель run-start — диспетчер, не lock.** Признак «подкоманда платит или
  меняет continuation-state» принадлежит перечню выше, а не тому, берёт ли
  подкоманда executor lock: `retry` и `watch` его не берут вовсе, а `run
  --force` намеренно его пропускает. Поэтому run-start пишется **одной**
  точкой в `main()` до вызова handler-а — для всех подкоманд перечня без
  исключений, включая `run --force`. Привязка run-start к lock оставила бы
  три платящих пути (`retry`, `watch`, `run --force`) без run-start и, по
  FR-07, без closure — то есть с call-start-ами под `run_id`, для которого
  прогона «не было», и с прочтением такого прогона как legacy.
- `watch` — один invocation, один `run_id` на все задачи цикла.
- Prompt-артефакт (`prompts_log`) несёт `run_id` и `call_id` в заголовке
  файла, оставаясь «prompt как отправлен» (#282).
- Каждый OTel-record и audit-запись содержат оба поля, когда `pipeline_id`
  существует; при его отсутствии — только `run_id`.

**Acceptance**:

- Тест поднимает прогон с `ORCHESTRA_PIPELINE_ID`, выполняет одну задачу с
  review (fake CLI) и проверяет: OTel JSONL, audit-log, `agent_calls`,
  prompt-артефакты, manifest и closure содержат один и тот же full UUIDv4
  `run_id` и тот же `pipeline_id` в отдельном поле.
- Второй invocation получает другой `run_id`; тот же `pipeline_id` при том же
  окружении.
- `grep`/статический тест: `uuid4().hex[:8]` в `cli.py` и
  `uuid.uuid4()` в `audit_log.py` как источники идентичности отсутствуют.
- Старая DB (без столбцов) открывается, мигрирует, старые строки читаются с
  `run_id IS NULL`; `costs` суммирует их как прежде.
- `test_json_result_contract.py` зелёный с аддитивным `run_id`.

traces: [G-01, G-02, J-01, J-02, J-03, P-01, P-03, M-03, CON-02]

#### FR-02: Записывать durable-намерение перед каждым платным subprocess и запрещать запуск без подтверждения записи

**Priority**: Must

Перед стартом каждого платного subprocess существует **подтверждённый**
call-start с `run_id`, `call_id`, provenance, policy identity, optional
`task_id`/attempt и digest redacted prompt-а. Без ack `Popen` не вызывается.
Сбой после call-start без call-result оставляет open call; ни restore, ни
следующий запуск не повторяют его молча.

Уточнения:

- **Один seam.** Call-start и call-result пишутся в одном месте, через
  которое проходят все сайты платных вызовов (§3, «Платный subprocess»).
  Ни один путь к бинарю провайдера его не обходит — по образцу пояса
  `PaidBinaryReached` (`test_harness_guards.py`). Где именно seam живёт
  (`runner.py` spawn-точка, `prompts_log`, новый модуль) — вход design
  (Q-06).
- **Порядок на сайте вызова:** budget guard (#213) → prompt на диск (#282)
  → call-start → ack → `Popen` → call-result → `record_agent_call` /
  `record_attempt` → checkpoint (FR-03). Budget-отказ происходит **до**
  call-start: отказанный вызов — не платный вызов, строки call-start у него
  нет (как сегодня у `agent_calls`: «a refusal is no row»); отказ виден в
  attempt/closure и в `append_not_started` prompt-артефакта (#296).
- **Отказ ack = fail-closed до траты.** Канал не подтвердил call-start →
  задача останавливается на текущей безопасной границе с типизированным
  `Refusal(kind="instrument")` → `INFRASTRUCTURE` / exit 2 (#230), closure
  пишется (FR-07). Никакого «попробуем без записи».
- **Один `call_id` — ровно один call-start и не более одного call-result.**
  Повторная запись результата того же `call_id` отвергается.
- **Open call блокирует продолжение.** При старте любого прогона в том же
  namespace и при restore наличие open call для задачи → эта задача
  `needs-human` до платного вызова; `run --all` пропускает её с причиной,
  `run --task` отказывает. Обнаружение — по evidence bundle/checkpoint, а
  не по памяти процесса.
- **Правило namespace-wide, а не по `run_id`.** Решение про open calls
  принимает namespace целиком, а не ключ одного прогона: `restore <run_id>`
  отказывает `needs-human`, если open call остался у **любого** прогона того
  же workstream-а, в том числе начатого позже восстанавливаемого, и первый
  `run` после restore задаёт тот же вопрос заново, а не доверяет индексу,
  приехавшему внутри snapshot-а. Конфигурация, в которой «по `run_id`» и
  «namespace-wide» расходятся, одна и предъявляется прямо: прогон A закрыт,
  более поздний прогон C того же namespace оставил open call, восстановлен
  A — повтор C обязан быть предъявлен, а не сделан молча. Перечень путей,
  читающих или решающих open calls, и механизм namespace-wide для каждого —
  предмет design, и он обязан быть полным.
- **Ограничение названо, а не умолчано.** Namespace в дереве привязан к
  абсолютному пути, поэтому два одновременно живых рабочих каталога с одним
  объявленным namespace и одним workstream (например, restore выполнен, пока
  исходная машина жива) этим требованием не покрыты: обнаружение потребовало
  бы листать индекс на каждом старте и держать над ним lease, а ни того, ни
  другого этот workstream не вводит.
- **Операторская дверь.** Open call закрывается только аудируемой командой
  с обязательным `--reason` и записанным actor (по образцу `tdd abandon`,
  `budget authorize`): исход `resolved_unknown` (человек решил, повторять
  или нет), запись — новый call-result, ссылающийся на open call. Имя
  команды — Q-04.
- Timeout, `is_error` при exit 0, пустой ответ, crash провайдера — всё это
  **call-result** с соответствующим outcome (классификация
  `runner.classify_agent_answer`, #241), не open call. Open call — только
  когда процесс spec-runner умер между ack и записью результата.
- `call_id` пишется в `agent_calls`/`pr_agent_calls` рядом с существующим
  `provenance` (FR-01), так что ledger стоимости и evidence соединяются
  одним ключом.

**Acceptance**:

- Двойник artifact store, отказывающий в call-start → ни одного `Popen`
  (monkeypatch-двойник), задача остановлена, exit 2, closure с причиной
  `call_start_not_acknowledged`.
- Fault-injection на границах «после call-start до spawn» и «после spawn до
  результата», 1000 повторений (`slow`): 0 платных вызовов без call-start,
  0 автоматических повторов open call; restore после падения между spawn и
  результатом выдаёт `needs-human` **до** любого subprocess.
- Матрица сайтов (RED authoring, RED agent round (#220), GREEN, review,
  review:<role>, `plan --full`, `plan --gated`, интерактивный `plan`,
  `review-pr verify`, `review-pr fix`, `doctor`): у каждого сайта call-start
  предшествует `Popen` (порядок наблюдается двойником).
- Budget-отказ перед вызовом → нет call-start; prompt-артефакт заканчивается
  `=== NOT STARTED: … ===` как сегодня.
- Повторный call-result для того же `call_id` → отказ записи, первая запись
  неизменна.
- Операторская команда закрытия open call без `--reason` → отказ; с
  `--reason` → новая запись с actor, задача снова выбираема.

traces: [G-01, G-02, J-01, J-03, P-01, P-02, M-02, M-03, CON-03, OUT-02]

#### FR-03: Публиковать согласованный continuation checkpoint после каждого изменения состояния, необходимого для продолжения

**Priority**: Must

После каждой continuation-relevant mutation (§3) доступен новый checkpoint:
консистентный снимок state DB через SQLite backup API (или эквивалентный
транзакционный snapshot, включающий WAL-only страницы), версионированный
manifest и Git/WIP-материал (FR-04). Простая копия файла `.db` checkpoint-ом
не является.

Уточнения:

- **Точки публикации** — сайты записи перечисленных таблиц и
  `bookkeeping.commit_status_flip`; одна функция «после mutation», вызываемая
  из них, а не разбросанные вызовы (по образцу `run_plugin_hooks_for`, #307).
- **Снимок синхронен, доставка — по NFR-02.** Локальный консистентный
  snapshot берётся в точке mutation до возврата; публикация во внешний store
  укладывается в p95 ≤ 60 s. Может ли доставка быть асинхронной и что
  считается «доступен» — вход design (Q-05); требование — closure (FR-07)
  ссылается только на **acknowledged** checkpoint id.
- **Идентичность checkpoint-а:** `checkpoint_id` (UUIDv4) + монотонный
  `sequence` внутри `run_id`; manifest предыдущего checkpoint-а указан как
  `supersedes`. Для continuation обязателен только **последний** (RK-05).
- **Состав manifest-а** — §3. Effective TDD namespace записывается вместе со
  способом получения `declared` / `computed`; при `computed` — также seed
  без абсолютного пути невозможен, поэтому manifest хранит **значение**, а
  не способ его пересчёта.
- **Degraded mode:** если DB отказала, checkpoint включает spool (FR-08) и
  помечен `degraded: true`; snapshot DB берётся из последнего успешного
  состояния файла.
- **Без локальных фактов** (CON-06): абсолютные пути, PID, lock, stop-marker,
  временные worktrees, `.executor-progress.txt` в checkpoint не входят;
  manifest перечисляет их в `excluded`.
- `.executor-*` пояс `spec/.gitignore` покрывает локальные копии
  checkpoint/spool (CON-04).

**Acceptance**:

- Тест пишет attempt, запрещая SQLite чекпоинтить WAL (`wal_autocheckpoint=0`,
  открытое соединение), снимает checkpoint, восстанавливает в новом
  каталоге: attempt виден. Тот же тест с `shutil.copy` main DB — красный.
- После каждой mutation из перечня §3 (attempt, red checkpoint, claim
  record/release, verify evidence, waiver, remedy, budget authorization, gate
  verdict, `pr_*`, status flip) двойник store получает новый checkpoint с
  большим `sequence`; после read-only команд — не получает.
- Manifest валиден по `schemas/checkpoint-manifest.schema.json`; содержит
  `contract_version`, `run_id`, `pipeline_id`, namespace + `namespace_source`,
  `config_hash`, `head`, `published_ref`, `digests`, `excluded`.
- `grep` по manifest и всем файлам checkpoint-а: ни одного абсолютного пути
  исходного каталога, PID или имени временного worktree.
- Checkpoint после `budget authorize` из отдельного invocation содержит
  authorization (authority mutations публикуются даже без attempt).

traces: [G-01, J-01, J-02, P-01, P-02, M-01, M-05, CON-02, CON-04, CON-06]

#### FR-04: Включать в continuation checkpoint Git-материал и незавершённую работу, без которых следующий шаг нельзя воспроизвести

**Priority**: Must

После удаления исходного каталога по checkpoint восстанавливаются:
опубликованные commits/refs (по ссылке — reachability из published ref),
local-only commits, dirty или untracked work и runner-created rescue stash.
Lock, stop-файлы, временные worktrees и process identity не
восстанавливаются и не становятся authority.

Уточнения:

- **Repository identity** в manifest: нормализованный remote URL и root
  commit (`git rev-list --max-parents=0`) — чтобы restore мог отказать при
  подмене репозитория (FR-05), не полагаясь на путь.
- **Published material — ссылкой.** Для каждого ref, на который ссылается
  continuation (base branch, task-ветка, integration branch #254): имя,
  SHA, и факт достижимости из published ref на forge. Байты pushed-объектов
  не копируются (OUT-04).
- **Unpublished material — в WIP artifact.** Local-only commits
  (`<published-base>..HEAD` каждой in-flight task-ветки), dirty/untracked
  work по семантике `git_ops.uncommitted_work_paths` (runtime-state
  исключён), rescue stash с меткой `spec-runner rescue: <task>` — переносятся
  так, чтобы `git` в новом каталоге восстанавливал их байт в байт. Формат
  (git bundle + encrypted tar / access-controlled ref) — Q-03.
- **Исключено всегда:** `.executor-*` (кроме DB snapshot-а в самом
  checkpoint-е), `.lock`, `.<prefix>spec.lock`, `.executor-stop`,
  `spec-runner-red-*`/`verify-*`/`claim-*` worktrees, virtualenv, tool
  caches (OUT-05). Restore их не создаёт; lock создаётся заново живым
  процессом.
- WIP artifact — часть checkpoint-а и подчиняется тем же digests,
  redaction-политике и retention (NFR-04, NFR-05, NFR-07). Пустой WIP —
  явная запись `wip: none`, не отсутствие файла.

**Acceptance**:

- Restore-drill: прогон с local-only commit на task-ветке, dirty tracked
  файлом, untracked файлом и rescue stash; каталог удалён; restore в другой
  абсолютный путь → `git log` содержит local-only commit, dirty/untracked
  байты идентичны (SHA-256), stash с той же меткой существует.
- В восстановленном каталоге нет `.lock`, `.executor-stop`, временных
  worktrees; `spec-runner status` не считает lock stale от чужого PID.
- Прогон без WIP → manifest `wip: none`, restore проходит.
- Файл WIP artifact-а изменён на один байт → restore отказывает до
  распаковки (NFR-04).
- Published ref, на который ссылается manifest, недоступен на forge →
  restore `needs-human` с именем ref и SHA, без попытки реконструкции
  (OUT-09).

traces: [G-01, J-01, J-02, P-01, M-01, M-05, CON-04, CON-06, OUT-04, OUT-05]

#### FR-05: Восстанавливать прогон по run_id в новом рабочем каталоге и вычислять следующий безопасный шаг

**Priority**: Must

Конечная команда восстанавливает прогон по `run_id` в другой абсолютный
путь: проверяет digests и identity, возвращает active claims, budget
authority, effective TDD namespace и WIP, вычисляет следующий безопасный шаг
либо останавливается `needs-human` с точной причиной. Несовпадение
repository, config или namespace — отказ **до** платного вызова и **до**
claims gate.

Уточнения:

- **Поверхность:** `spec-runner restore <run_id> --into <dir>` (Q-04
  подтверждает имя). Команда конечна, не запускает платный вызов и не
  «продолжает» сама: её результат — восстановленный каталог и напечатанный
  следующий шаг (`spec-runner run --task TASK-101`, `spec-runner tdd
  resume …`, `needs-human: <reason>`), exit 0 / 1 (`needs-human`) / 2
  (instrument). `--json` даёт машинный отчёт (`schemas/restore-result.schema.json`).
- **Порядок проверок (все — до записи в каталог):** contract version →
  digests всех файлов (NFR-04) → repository identity → policy identity
  (`config_hash`) → effective namespace → open calls **всего workstream-а**
  (FR-02: правило namespace-wide, а не по восстанавливаемому `run_id`) →
  spool. Более поздний прогон того же workstream-а, **оставивший
  изменения, которых этот snapshot не несёт**, — тоже отказ `needs-human`:
  они (authorizations, claims, remedy) живут под собственными `run_id` и в
  snapshot не попали, а продолжение с него потеряло бы их молча; отказ
  называет как выход прогон, чей `restore` **исполним**: последний
  прогон workstream-а с acknowledged checkpoint-ом, после которого не
  начинался ни один блокирующий прогон (собственная запись текущего
  `restore` не в счёт). Прогон, у которого checkpoint есть, но которого
  блокирует более поздний, выходом не называется — это была бы петля. Если
  такого прогона нет — отказ говорит об этом, называет блокирующий `run_id`
  и выхода не предлагает. Наблюдаемым признаком «оставил» служит
  опубликованный checkpoint прогона; отсюда **принятый пробел** (решение
  владельца 2026-09-18): прогон, убитый в окне между записью mutation и
  доставкой checkpoint-а в store, признака не имеет и восстановлению не
  мешает, а его правка теряется молча. Читать отсутствие признака
  fail-closed нельзя: прогон, оставивший open call, по построению умер до
  своей closure, а дверь закрывает call, не прогон, — и тогда путь
  «закрыть open call → восстановиться» не работал бы ни разу. Сужение окна
  (синхронный ack для подкоманд без платного вызова) — отдельная задача, не
  это требование. Прогон, ничего такого
  не оставивший, восстановлению не мешает — иначе закрыть open call ради
  восстановления и затем восстановиться было бы нельзя ни одним путём
  (аудируемая дверь `evidence` сама становится более поздним прогоном), а
  `restore`, чей run-start диспетчер пишет до собственных проверок,
  отказывал бы своей же записи. Какая подкоманда в какой половине — решает
  design; требование фиксирует критерий и то, что **перечень обязан быть
  полным**: подкоманда, которая пишет run-start, но не отнесена ни к одной
  половине, — дефект, а не «по умолчанию не блокирует». Откат на ранний
  checkpoint workstream-а, у которого есть более поздние **блокирующие**
  прогоны, эта команда как операцию не предлагает.
- **Namespace:** manifest хранит effective value и `namespace_source`. Если
  активный config объявляет `tdd_namespace`, равный записанному, — ок. Если
  объявляет другой — отказ с обоими значениями и способом получения. Если
  не объявляет — restore записывает восстановленное значение как
  `tdd_namespace` в config восстановленного каталога (видимая, коммитуемая
  правка; Q-09), потому что `computed` в новом пути **гарантированно** даст
  другой хеш. Отказ на mismatch — всегда, не предупреждение (RK-06).
- **Restore применяет, не пересочиняет.** DB — из snapshot-а (+ replay spool,
  FR-08); claims, checkpoints, authorizations, waivers — строками из DB,
  никакой реконструкции из Git (OUT-06). После restore `claims.check_claims`
  видит старые claims, `budget.effective_limits` — старые authorizations.
- **Legacy run** (CON-07): `run_id` без run-start/manifest нужной версии →
  fail-closed с перечнем недостающих гарантий и ссылкой на ручной путь
  (`docs/architecture.md` «operational minimum»); автоматической
  реконструкции нет.
- **Работает до приёмки всего набора — только под явным experimental-флагом**
  (CON-01, RK-04): команда отказывает без `--experimental`, пока CHANGELOG
  не снял статус.
- Restore идемпотентен по каталогу: повторный запуск в непустой `--into`
  отказывает, не перезаписывает.

**Acceptance**:

- Restore-drill (чартер): claim, budget authorization, waiver, local-only
  commit, dirty work, rescue stash; каталог удалён; restore в другой
  абсолютный путь → все факты на месте; `tdd status` показывает claim;
  `costs` показывает authorization; effective namespace == manifest; claims
  gate видит claim (тест модифицирует замороженный файл → gate отказывает);
  следующий шаг напечатан; оплаченный вызов не повторён (0 `Popen` при
  восстановлении, следующий `run` стартует с нужного шага).
- Namespace mismatch: тот же checkpoint, активный config с другим
  `tdd_namespace` → exit 1, сообщение с обоими значениями и источниками,
  0 `Popen`, claims gate не вызывался.
- Repository mismatch (другой root commit) и policy mismatch (другой
  `review_policy`) → отказ до записи в каталог.
- Open call в bundle → `needs-human` с `call_id`, provenance и задачей.
- Open call **другого** прогона того же workstream-а (прогон A закрыт,
  более поздний C оставил open call, восстанавливается A) → `needs-human` с
  `run_id` прогона C, `call_id` и provenance, 0 `Popen`; первый `run` в
  восстановленном каталоге предъявляет тот же open call, а не повторяет
  его.
- Legacy run → fail-closed с перечнем «нет run-start», «нет manifest»,
  «нет call records».
- Без `--experimental` до снятия статуса → отказ с текстом CON-01.
- Валидация и вычисление шага ≤ 60 s (NFR-03) на reference-наборе.

traces: [G-01, G-03, J-01, J-02, P-01, P-03, M-01, M-02, M-05, M-07, CON-01, CON-06, CON-07, OUT-06, OUT-09]

#### FR-06: Публиковать immutable evidence для каждого платного вызова и terminal task attempt независимо от исхода

**Priority**: Must

Task execution, review, `plan --full`, gated planning — и остальные сайты
§3 — создают адресуемые call records. Success, failed, blocked, timeout и
infrastructure error содержат identity, стоимость (или явный `unknown`),
bounded/redacted prompt и result либо их digests. Terminal task attempt
добавляет logical append-only export своих строк.

Уточнения:

- **Call record** = call-start + call-result (FR-02) под одним `call_id`,
  адресуем по `run_id/call_id`. Redacted prompt и result — bounded по
  NFR-06, с SHA-256 и размером полного содержимого; полный текст — в
  prompt-артефакте локально (как сегодня), в bundle — bounded копия.
- **Attempt evidence:** при записи terminal attempt (success/failed/blocked)
  экспортируются строки этой задачи/attempt-а из `attempts`,
  `red_checkpoints`, `tdd_claims`, `tdd_phases`, `tdd_remedies`,
  `phase_waivers`/`waivers_applied`, `budget_authorizations`,
  `gate_verdicts`, `verify_evidence`, `agent_calls` — JSONL, версионированная
  схема, с namespace, task, attempt, `config_hash`, actor, reason,
  referenced SHA/blob, timestamp, supersession status (#478). `pr_*` — при
  завершении раунда `review-pr`.
- **Планирование** получает ledger-identity: `plan --full` и `plan --gated`
  пишут call records с provenance `plan:<stage>`, интерактивный `plan` — с
  `plan:interactive`, все с `task_id = NULL`;
  `costs` показывает их отдельной строкой «planning», не смешивая с task
  cost (по образцу `pr_cost_rows`, #218).
- **Immutable:** запись после публикации не переписывается; исправление —
  новая запись со ссылкой на предшественника. Store-контракт должен
  запрещать overwrite (или spec-runner проверяет ETag/версию).
- Стоимость `unknown` хранится как `null`, никогда `0.0` (#213).
- Evidence публикуется на **каждом** исходе, включая `TASK_BLOCKED`,
  timeout, `INFRASTRUCTURE` и `is_error` при exit 0; `post_review` не
  является каналом evidence (он только для успешного completion-path).

**Acceptance**:

- Матрица outcome × site: {success, `TASK_FAILED`, blocked, timeout,
  infrastructure error} × {GREEN, review, review:<role>, `plan --full`,
  `plan --gated`, интерактивный `plan`, `review-pr fix`, `doctor`} — каждая
  клетка оставляет call
  record с `run_id`, `call_id`, provenance, стоимостью или `null`, bounded
  prompt/result, SHA-256 и размером полного содержимого.
- Terminal attempt (`done`, `failed`, `blocked`) → JSONL-экспорт строк,
  валидный по `schemas/evidence-record.schema.json`; строки другой задачи
  отсутствуют.
- `plan --full` с fake CLI → три call records (`plan:requirements`,
  `plan:design`, `plan:tasks`); интерактивный `plan "<описание>"` с тем же
  fake CLI → по одному call record (`plan:interactive`) на круг цикла;
  `costs` показывает их суммой «planning», `task_cost` любой задачи не
  изменился.
- Попытка перезаписать опубликованную запись → отказ; чтение возвращает
  первую.
- Secret-корпус (NFR-05) в prompt и result → 0 секретов в bundle, digests
  полного содержимого сохранены.

traces: [G-02, J-03, P-02, P-03, M-03, M-06, CON-04, CON-08]

#### FR-07: Создавать отдельную immutable run-closure запись для каждого штатного завершения или ранней остановки

**Priority**: Must

Каждый orderly exit платящей подкоманды — успешное завершение, ранняя
остановка, отказ правила, поломка инструмента, сигнал и необработанное
исключение — создаёт closure с kind, причиной и ids последних acknowledged
checkpoint/evidence. Run-start без closure трактуется как crash/unknown,
никогда как пустой успех.

Уточнения:

- **Run-start** пишется до первой работы — в `main()` до диспетчеризации
  handler-а платящей подкоманды (§FR-01, «носитель run-start — диспетчер, не
  lock»), поэтому `retry`, `watch` и `run --force`, не берущие executor lock,
  получают его на общих основаниях. Closure — ровно одна на run-start;
  повторная отклоняется. Отказ executor lock («lock занят») наступает **после**
  run-start и потому даёт обычную пару run-start + closure, а не одинокий
  run-start.
- **Kind выводится механически, а не назначается сайтом.** Closure пишет одна
  точка — `finally` диспетчера вокруг handler-а, — и kind она выводит из двух
  фактов, наблюдаемых ею самой: как handler ушёл (необработанное исключение,
  сигнал, код выхода) и осталась ли невыполненная работа (задача, по которой
  этот invocation записал attempt и которая не в статусе `success`, либо
  открытый call). Ни один сайт выхода kind не выбирает и причину контексту не
  сообщает; перечня сайтов требование не заводит.
- **Виды closure — пять, и словарь пинуется схемой**: `completed`, `refused`,
  `failed`, `interrupted`, `crashed`. `completed` выводится единственным
  сочетанием «код 0 и невыполненной работы нет»; `refused` — ненулевой код,
  когда отказ правила виден по `error_kind` последнего неуспешного attempt-а
  в словаре дерева `ERROR_KINDS`; `failed` — всякий иной ненулевой код и код
  0 при невыполненной работе; `interrupted` — сигнал или `KeyboardInterrupt`;
  `crashed` — необработанное исключение. Дефолт «причина неизвестна →
  `completed`» и отказ сериализации, оставляющий run-start без closure,
  запрещены оба — первый даёт пустой успех, второй превращает штатный выход в
  неотличимый от crash.
- **Различения, которых правило не делает, названы прямо**: отказ правила от
  поломки инструмента у подкоманд, attempt-ов не пишущих; код 1 «работа
  плоха» от кода 2 «инструмент не смог» (оба `failed`, различитель — поле
  `exit_code`); остановка таймером или stop-marker-ом от «делать было
  нечего», когда обе дают код 0 без невыполненной работы (сигнал сюда не
  входит: его диспетчер наблюдает сам). Требование их и не
  утверждает: платой за них был бы словарь причин по сайтам, который обязан
  догонять дерево.
- **`reason` — свободная строка, не словарь.** Схема пинует kinds; reason не
  перечисляется, не валидируется и может быть пустым. Диспетчер берёт его из
  исключения, факта сигнала, `error_kind` последнего неуспешного attempt-а,
  строкового аргумента `SystemExit` или персистированного
  `last_run_stop_reason` — что из этого есть. Требования «reason совпадает с
  текстом `status`» нет.
- **`RUN_STOP_REASONS` не растёт и ключом closure не является.** Словарь
  `status` и внешних читателей (audit-таблица Maestro) остаётся сегодняшним,
  включая его дефолт `completed` на ранних остановках; closure его не читает
  и потому от этого дефекта не зависит. Правдивость самого `status` — вне
  объёма этого требования.
- **Состав:** `run_id`, `pipeline_id`, подкоманда, kind, reason, exit code,
  `last_checkpoint_id`, `last_call_ids`/`attempt_ids`, число open calls,
  `degraded`/spool status, timestamps start/end. `completed` с пустым
  `attempt_ids` и нулём вызовов читается как «делать было нечего» — по этим
  полям, а не отдельным kind-ом.
- **Пути без attempt** покрыты по построению: run-start пишет диспетчер до
  handler-а, closure — его же `finally`, поэтому `run` без ready-задач,
  `validate`-отказ при старте, dirty-spec guard, tracked-state-DB guard
  (#273), занятый lock, `retry` с несуществующим `--task-id`, `doctor` при
  отказе оператора на cost gate, `plan` с usage-ошибкой, `review-pr` на draft
  PR, `tdd` с `RemedyError`, `budget authorize` с `AuthorizationError`,
  `restore` на любом отказе проверок и `evidence purge` без истёкших
  объектов закрываются, не создав ни одного attempt-а. Выходы, предшествующие
  `start()` (ошибка config/профиля, отказ argparse на обязательном флаге),
  run-start не пишут и потому closure не имеют — это BEH-04, а не пробел.
- Closure — **последняя** запись прогона: пишется после последнего
  checkpoint-ack; если ack последнего checkpoint не получен, closure несёт
  `last_checkpoint_id` предыдущего acknowledged, kind `failed` и exit 2.
- SIGKILL/OOM/потеря машины closure не оставляют — по построению; read-surface
  (FR-09) и restore (FR-05) читают такой прогон как `crash/unknown`.
- Отказ записи closure — не тихий: stderr называет причину; exit code
  прогона не улучшается из-за неё.

**Acceptance**:

- Матрица по шести строкам правила вывода — необработанное исключение,
  сигнал, ненулевой код с отказом правила в последнем attempt-е, прочий
  ненулевой код, код 0 с невыполненной работой, код 0 без неё → у каждой
  ровно одна closure соответствующего kind с `last_checkpoint_id` последнего
  acknowledged checkpoint-а, и `completed` только на последней.
- Каждая платящая подкоманда перечня FR-01 (одиннадцать позиций: `run` и
  десять вне его — восемь самостоятельных команд и две формы `evidence`) в
  отдельном invocation, включая `retry`, `watch` и `run --force` (executor
  lock не берётся ни одной из трёх) → ровно один run-start и ровно одна
  closure; «lock занят» у обычного `run` → та же пара, exit 1.
- `completed` не выдан ни одному исходу с невыполненной работой, и это
  предъявлено там, где код процесса лжёт: `retry`, чья задача осталась
  `blocked` (exit 0), и `watch`, остановленный `max_consecutive_failures`
  (exit 0), дают `failed`.
- Ни один сайт выхода не выбирает kind сам: в дереве нет ни `note_stop`, ни
  таблиц причин по подкомандам, ни новых значений `RUN_STOP_REASONS`
  (статический тест).
- `kill -9` прогона после run-start → closure отсутствует; `evidence <run_id>`
  и `restore` классифицируют `crash/unknown`.
- Двойник store, отказавший в ack последнего checkpoint-а → closure `failed`
  с id предыдущего и exit 2.
- Повторная closure для того же `run_id` → отказ записи.
- Closure валидна по `schemas/run-closure.schema.json`.

traces: [G-02, J-01, J-03, P-02, P-03, M-04, CON-05]

#### FR-08: Сохранять continuation-relevant mutation в аварийный durable spool при отказе основной state DB

**Priority**: Must

Ошибка SQLite при записи continuation-relevant mutation сохраняет payload и
ordering/join keys в spool, независимый от DB; новый процесс восстанавливает
их в DB до любой другой работы. Одновременный отказ DB и spool
останавливает выполнение до следующего платного вызова.

Уточнения:

- **Формат:** append-only JSONL с `fsync` на запись; строка = `seq`
  (монотонный), `run_id`, `namespace`, `task_id`/attempt, `table`,
  payload, SHA-256 строки. Расположение — рядом со state DB (тот же
  `.executor-*` пояс), плюс включение в каждый checkpoint (FR-03).
- **Кто пишет:** `_enter_degraded_mode` (`state.py:2352`) и каждый путь,
  сегодня падающий в in-memory. Текущее поведение «уведомить один раз и
  жить в памяти» заменяется: mutation либо подтверждена DB, либо
  подтверждена spool-ом, либо прогон останавливается на текущей границе
  (инвариант 5). `state_degraded` уведомление остаётся.
- **Кто читает:** старт любого процесса на этом namespace (после run-start и
  гардов старта, до выбора задачи; executor lock этой границы не задаёт —
  `retry` и `watch` его не берут, `run --force` пропускает) и `restore`: replay в DB в порядке `seq`, идемпотентно
  (повторный replay не дублирует строк); после успешного replay spool
  ротируется в архив с пометкой в checkpoint. Отказ replay → прогон не
  стартует (`INFRASTRUCTURE`, exit 2).
- **Оба отказали:** ни DB, ни spool не подтвердили → `Refusal(kind=
  "instrument")`, следующий платный вызов не стартует, closure `failed` с
  exit 2 (по возможности — в store; если и он недоступен, stderr + exit 2).
- Spool не является вторым источником истины (RK-03): читатели принимают
  решения по DB; spool только доигрывается в неё.

**Acceptance**:

- Двойник SQLite, падающий на `record_attempt` → attempt в spool (payload +
  join keys + `seq`); новый процесс восстанавливает его в DB, `status` его
  показывает, следующий checkpoint его содержит.
- Тот же сценарий на `record_claims`, `record_authorization`,
  `record_verdict`, `record_verify_evidence` — каждая mutation из §3.
- Двойник, роняющий DB и spool одновременно → 0 последующих `Popen`,
  exit 2, closure `failed`.
- Replay дважды → строки не дублируются; изменённый байт в spool-строке →
  replay отказывает, прогон не стартует.
- Fault-injection на границе «после attempt до checkpoint» ×1000 (`slow`):
  0 потерянных подтверждённых mutations (NFR-01).

traces: [G-01, J-01, J-03, P-01, M-01, NFR-01, CON-02]

#### FR-09: Давать оператору и аудитору read-surface по run_id без необходимости восстанавливать весь рабочий каталог

**Priority**: Should

По `run_id` можно получить последний checkpoint, closure (или
`crash/unknown`), открытые calls, список attempts и evidence-ссылки с
исходами и стоимостью — с другой машины, без доступа к исходному клону и
без restore.

Уточнения:

- **Поверхность:** `spec-runner evidence <run_id> [--json]` (Q-04). Читает
  только store; не требует `project_root`, DB или Git. `--json` пинуется
  `schemas/evidence-view.schema.json`.
- **Состав ответа:** run-start (подкоманда, policy identity, repository
  identity), статус прогона (`closed:<kind>` / `open` (процесс жив по
  lock-PID недоказуемо — показывается как `no closure`) / `crash/unknown`),
  последний acknowledged checkpoint (id, sequence, время, namespace),
  open calls (`call_id`, provenance, task, время call-start), attempts
  (task, номер, outcome, стоимость или `unknown`, ссылки на call records),
  суммарная стоимость платных вызовов и, при наличии данных store,
  стоимость хранения (CON-08).
- **Только один `run_id`** (OUT-08): никакой агрегации по проектам,
  трендов или биллинга; список прогонов namespace-а — не в scope.
- Read-surface никогда не пишет в store и не принимает решений за оператора;
  следующий безопасный шаг он показывает как **рекомендацию** restore-а
  (FR-05), помечая недоказуемое как «не доказуемо: <причина>» (M-06).
- `status` (локальный) показывает `run_id` последнего прогона namespace-а —
  чтобы оператор знал, что вводить в `evidence`/`restore`.

**Acceptance**:

- В каталоге без клона, только с доступом к store: `evidence <run_id>` даёт
  последний checkpoint, closure, open calls, attempts с исходами и
  стоимостью, ссылки на call records.
- Прогон после `kill -9` → `crash/unknown`; прогон с open call →
  перечислен `call_id`; legacy `run_id` → «нет evidence-контракта».
- `--json` валиден по схеме; человеческий и JSON-вывод собираются из одного
  `collect()` (по образцу `tdd_status.py`), так что не расходятся.
- Store недоступен → exit 2 с причиной, не пустой отчёт.

traces: [G-02, G-03, J-02, J-03, P-01, P-03, M-06, CON-05, OUT-08]

## 5. Нефункциональные требования

#### NFR-01: Не терять ни одной подтверждённой continuation-relevant mutation

**Priority**: Must

RPO 0 для записей, подтверждённых DB, spool или artifact store.
Неподтверждённая mutation останавливает исполнение на текущей безопасной
границе, а не после следующего вызова (инвариант 5).

**Acceptance**:

- Fault-injection (`slow`, 1000 повторений на каждой границе: после
  run-start; после call-start до spawn; после spawn до результата; после
  результата до attempt; после attempt до checkpoint): 0 потерянных
  подтверждённых mutations, 0 silent retries, 0 платных вызовов без
  call-start.
- Инъекция реализована двойниками store/SQLite/spool и `os._exit` в
  seam-ах — не `sleep`-гонками.

traces: [G-01, FR-02, FR-03, FR-08, M-01, M-02]

#### NFR-02: Не добавлять существенную задержку перед платными вызовами и между шагами

**Priority**: Should

Durable call-start acknowledgement p95 ≤ 1 s и p99 ≤ 3 s; новый checkpoint
для reference workload доступен вне машины p95 ≤ 60 s при здоровом
хранилище. Приоритет `Should`: цели измеряются бенчмарком на reference
workload, а не CI-гейтом; деградация хранилища — fail-closed с closure, не
тихое продолжение (RK-01). Подтверждение приоритета — Q-10.

**Acceptance**:

- Бенчмарк-скрипт (не CI-обязательный) на reference workload (одна задача с
  review, локальный адаптер store): call-start ack p95/p99 в бюджете;
  checkpoint доступен p95 ≤ 60 s.
- Таймаут ack конфигурируем; превышение → отказ до spawn (FR-02), не
  ожидание без предела.

traces: [FR-02, FR-03, P-01, RK-01]

#### NFR-03: Восстанавливать прогон достаточно быстро для штатного операторского handoff

**Priority**: Should

Полное восстановление набора до 1 GiB в новом каталоге ≤ 15 min при сети
≥ 100 Mbit/s; после загрузки валидация и вычисление следующего безопасного
шага ≤ 60 s. Приоритет `Should` по тем же основаниям, что NFR-02 (Q-10);
вторая половина (≤ 60 s) проверяется в CI на reference-наборе.

**Acceptance**:

- Reference-набор (DB + WIP ≈ 10 MiB) в CI: валидация + вычисление шага
  ≤ 60 s.
- 1 GiB-набор — ручной drill в условии завершения (M-01), время записано в
  отчёте drill-а.

traces: [FR-04, FR-05, P-01, M-01]

#### NFR-04: Обнаруживать повреждение, неполноту и подмену любого опубликованного артефакта

**Priority**: Must

100 % файлов manifest, checkpoint, WIP и evidence имеют SHA-256; изменение
любого байта или отсутствие обязательного файла даёт fail-closed до restore,
paid call и claims gate.

**Acceptance**:

- Для каждого файла checkpoint-а и bundle: замена одного байта → restore,
  следующий `run` в этом namespace и `evidence` отказывают с именем файла и
  ожидаемым/фактическим digest.
- Удаление обязательного файла (DB snapshot, manifest, WIP при
  `wip != none`, closure при `closed`) → тот же отказ.
- Manifest сам подписан digest-ом верхнего уровня; изменённый manifest →
  отказ.
- Отказ происходит **до** любого `Popen` и до `check_claims`.

traces: [FR-03, FR-04, FR-05, FR-06, FR-07, M-07, OUT-09]

#### NFR-05: Защищать приватный source, prompts и provider output

**Priority**: Must

TLS 1.2+ при передаче, AES-256 или эквивалентное managed encryption в
покое, доступ только явно назначенным ролям; тестовый корпус из 100
секретов даёт 0 известных секретов в опубликованных bounded logs.

Уточнения: шифрование и IAM — у store (OUT-03); spec-runner отвечает за
redaction **до** публикации и за невозможность публикации в обход
redaction; корпус секретов — фикстура репо (`tests/fixtures/secrets-corpus/`),
формат и движок redaction — Q-08.

**Acceptance**:

- Корпус 100 секретов (ключи провайдеров, токены forge, приватные ключи,
  `.env`-строки) в prompt и в result fake CLI → 0 вхождений в bundle;
  SHA-256 и размер полного содержимого сохранены.
- Адаптер store, объявивший `tls: false` или отсутствующее шифрование в
  покое → отказ конфигурации при загрузке (не runtime).
- Публикация без прохождения redaction невозможна: единственный путь
  публикации проходит через redactor (статический тест по образцу
  `PaidBinaryReached`).

traces: [FR-06, FR-09, P-04, M-07, CON-04, RK-02]

#### NFR-06: Ограничивать объём публикуемых prompt и result logs без потери доказательства исходных байтов

**Priority**: Must

Не более 1 MiB redacted prompt и 4 MiB redacted result на call; при
усечении сохраняются SHA-256 полного исходного содержимого, исходный размер
и явный marker `truncated`. Усечение — по образцу `prompts_log.bound`
(head **и** tail, поскольку frozen-files block добавляется последним, #214).

**Acceptance**:

- Prompt 3 MiB → bounded ≤ 1 MiB с head и tail, marker `truncated`,
  `full_sha256` и `full_size` совпадают с исходником.
- Result 10 MiB → bounded ≤ 4 MiB аналогично.
- Содержимое в бюджете → без marker-а, digest совпадает с bounded == full.

traces: [FR-06, P-03, RK-02]

#### NFR-07: Иметь предсказуемую retention и своевременное удаление приватных артефактов

**Priority**: Must

По умолчанию checkpoints хранятся до closure плюс 30 дней, evidence и
closure — 180 дней; политика настраивается в диапазоне 7–365 дней;
санкционированное удаление завершается в течение 24 h и оставляет
audit-запись без удалённого содержимого. Legal hold и правила удаления store
не обходятся (OUT-10).

**Acceptance**:

- `retention_days` вне 7–365 → `ConfigError` при загрузке config и в
  `validate`.
- Удаление (команда или lifecycle store — Q-11) оставляет audit-запись с
  `run_id`, ids, actor, reason, временем — и без payload.
- Промежуточные checkpoints (не последний) удаляемы по политике без потери
  restore-способности последнего (RK-05).

traces: [FR-03, FR-06, FR-07, P-04, CON-08, OUT-10]

## 6. Ограничения

Из customer-брифа, применены к требованиям:

- **CON-01** — статус experimental до приёмки FR-01–FR-08 целиком: `restore`
  за `--experimental` (FR-05), CHANGELOG называет статус.
- **CON-02** — встраивание в существующий контур: аддитивные столбцы, один
  seam call-start, существующие таблицы как logical export (FR-01, FR-02,
  FR-06).
- **CON-03** — контракт провайдера не меняется; open call = `unknown`,
  решение человека через аудируемую дверь (FR-02).
- **CON-04** — ни байта в продуктовый Git: `.executor-*` пояс, только store
  (FR-03, FR-04, NFR-05).
- **CON-05** — конечные команды оператора (`restore`, `evidence`, закрытие
  open call), без control-plane (FR-05, FR-09).
- **CON-06** — версионированный переносимый формат без локальных фактов
  (manifest §3, FR-03, FR-04).
- **CON-07** — legacy: диагностика и ручной путь, не реконструкция (FR-05).
- **CON-08** — новые расходы на store — отдельное одобрение владельца (Q-01);
  стоимость хранения атрибутируема `run_id` и видна в `evidence`/`costs`
  (FR-09).

Дополнительно (из инвариантов чартера): `.executor-state.db` schema и
`--json-result` меняются только аддитивно (инвариант 12); все тесты — с fake
CLI под поясом `PaidBinaryReached`, ни одного реального провайдера.

## 7. Вне объёма

OUT-01…OUT-10 брифа без изменений. Для требований это означает: нет
автоматического продолжения после restore (оператор запускает `run`), нет
retry open call без человека, нет собственного хранилища/IAM/KMS, нет копий
pushed-объектов, virtualenv и кэшей, нет изменения правил claims/TDD/
waiver/remedy/budget/review, нет миграции legacy-прогонов, нет
межпроектных отчётов, нет авто-«лечения» повреждённого артефакта, нет обхода
retention/legal hold.

## 8. Метрики успеха

M-01…M-07 брифа. Требования обеспечивают их измеримость: M-01/M-05 —
restore-drill (FR-04, FR-05, `--json` отчёт restore); M-02 — open call в
bundle + отказ до `Popen` (FR-02, FR-05); M-03 — ровно один call-start и
однозначный исход на `call_id` (FR-02, FR-06); M-04 — closure на каждом
orderly exit, `crash/unknown` при отсутствии (FR-07, FR-09); M-06 —
read-surface (FR-09); M-07 — `git status` чист после E2E, redaction-корпус,
integrity-отказы (CON-04, NFR-04, NFR-05).

## 9. Трассировка

### FR/NFR → цели, jobs, персоны, метрики, ограничения

| ID | Priority | Goals | Jobs | Personas | Metrics | CON / OUT |
|---|---|---|---|---|---|---|
| FR-01 | Must | G-01, G-02 | J-01, J-02, J-03 | P-01, P-03 | M-03 | CON-02 |
| FR-02 | Must | G-01, G-02 | J-01, J-03 | P-01, P-02 | M-02, M-03 | CON-03, OUT-02 |
| FR-03 | Must | G-01 | J-01, J-02 | P-01, P-02 | M-01, M-05 | CON-02, CON-04, CON-06 |
| FR-04 | Must | G-01 | J-01, J-02 | P-01 | M-01, M-05 | CON-04, CON-06, OUT-04, OUT-05 |
| FR-05 | Must | G-01, G-03 | J-01, J-02 | P-01, P-03 | M-01, M-02, M-05, M-07 | CON-01, CON-06, CON-07, OUT-06, OUT-09 |
| FR-06 | Must | G-02 | J-03 | P-02, P-03 | M-03, M-06 | CON-04, CON-08 |
| FR-07 | Must | G-02 | J-01, J-03 | P-02, P-03 | M-04 | CON-05 |
| FR-08 | Must | G-01 | J-01, J-03 | P-01 | M-01 | CON-02 |
| FR-09 | Should | G-02, G-03 | J-02, J-03 | P-01, P-03 | M-06 | CON-05, OUT-08 |
| NFR-01 | Must | G-01 | J-01 | P-01, P-02 | M-01, M-02 | — |
| NFR-02 | Should | G-01 | J-01 | P-01 | — | CON-05 |
| NFR-03 | Should | G-01, G-03 | J-01, J-02 | P-01 | M-01 | — |
| NFR-04 | Must | G-01, G-03 | J-01, J-02 | P-01, P-03 | M-07 | OUT-09 |
| NFR-05 | Must | G-02 | J-03 | P-04 | M-07 | CON-04, OUT-03 |
| NFR-06 | Must | G-02 | J-03 | P-03 | — | — |
| NFR-07 | Must | G-02 | — | P-04 | — | CON-08, OUT-10 |

### FR/NFR → критерии приёмки чартера → тестовые артефакты

| ID | Критерий приёмки чартера | Тест (ожидаемый артефакт) |
|---|---|---|
| FR-01 | Identity: один `run_id` во всех каналах, `pipeline_id` отдельно, второй invocation — другой | `test_run_identity.py` (OTel/audit/ledger/prompt/manifest/closure сравнение); `test_json_result_contract.py` (аддитивно); `test_state.py` (миграция столбцов) |
| FR-02 | Не стартует без ack; write-ahead fault-injection; open call → `needs-human` до subprocess | `test_call_start_before_spawn.py` (двойник store, 0 `Popen`, матрица сайтов); `test_write_ahead_fault_injection.py` (`slow`); `test_open_call_door.py` |
| FR-03 | WAL-разница; checkpoint после каждой mutation | `test_checkpoint_is_a_snapshot.py` (backup API vs `cp`); `test_checkpoint_after_every_mutation.py`; `schemas/checkpoint-manifest.schema.json` |
| FR-04 | Restore-drill: WIP, local-only commits, stash; lock/stop/worktrees нет | `test_restore_drill.py::test_git_material_round_trip`; `::test_temporary_state_never_restored` |
| FR-05 | Restore-drill; namespace mismatch; legacy; experimental-флаг | `test_restore_drill.py` (claims/authority/namespace/next step); `test_restore_refusals.py` (repo/policy/namespace/open call/legacy/experimental) |
| FR-06 | Evidence на каждом исходе × сайте; planning в ledger; immutable | `test_evidence_every_outcome.py` (матрица); `test_planning_has_ledger_identity.py`; `schemas/evidence-record.schema.json` |
| FR-07 | Closure на каждом exit, включая до attempt; `kill -9` → crash/unknown | `test_closure_every_exit.py` (матрица kinds); `::test_sigkill_leaves_no_closure`; `schemas/run-closure.schema.json` |
| FR-08 | Spool при отказе SQLite; оба отказали → стоп | `test_emergency_spool.py` (каждая mutation; replay идемпотентен; оба отказали → exit 2 + closure) |
| FR-09 | Read-surface без клона | `test_evidence_read_surface.py` (один `collect()`, crash/unknown, legacy, store недоступен); `schemas/evidence-view.schema.json` |
| NFR-01 | 1000 fault-injection на каждой границе, 0 потерь | `test_write_ahead_fault_injection.py` (`slow`, параметризация по границе) |
| NFR-02 | Latency бенчмарк | `scripts/bench_durability.py` (ручной); тест таймаута ack |
| NFR-03 | Recovery time | `test_restore_drill.py::test_validation_under_60s` (reference); ручной 1 GiB drill |
| NFR-04 | Один байт / отсутствующий файл → fail-closed до restore/paid call/claims gate | `test_integrity_fail_closed.py` (параметризация по файлу) |
| NFR-05 | Redaction-корпус, 0 секретов; публикация только через redactor | `test_redaction_corpus.py`; `tests/fixtures/secrets-corpus/`; статический пояс |
| NFR-06 | 1 MiB / 4 MiB, head+tail, digest+size+marker | `test_bounded_evidence_logs.py` |
| NFR-07 | Retention вне 7–365 отклонена; удаление с audit-записью | `test_retention_policy.py`; `test_config.py` |
| CON-04 | `git status` чист после всех E2E | `rglob`/`git status --porcelain` проверка в каждом E2E |
| CON-01 | Пояс `PaidBinaryReached`; `restore` за `--experimental` | `test_harness_guards.py` (существующий); `test_restore_refusals.py` |

Имена тестовых файлов — ожидание требований, не предписание; стадия design
вправе переименовать при сохранении покрытия.

### Риски чартера → требования-меры

| Риск | Мера | Требование |
|---|---|---|
| RK-01 latency на critical path | измеримый бюджет ack; таймаут → fail-closed; spool-как-ack только по решению design | NFR-02, FR-02, Q-02 |
| RK-02 утечка секретов | redaction до публикации, единственный путь через redactor, корпус-тест, bounded | NFR-05, NFR-06, FR-06 |
| RK-03 второй домен состояния | DB — authority; spool доигрывается; bundle read-only; read-surface не решает | FR-08, FR-09, FR-05 |
| RK-04 ложная восстановимость | `--experimental` до приёмки FR-01–FR-08; CHANGELOG-статус | FR-05, CON-01 |
| RK-05 объём артефактов | обязателен только последний checkpoint; retention промежуточных; стоимость хранения по `run_id` | FR-03, NFR-07, FR-09 |
| RK-06 namespace drift | effective value + source в manifest; restore записывает `tdd_namespace`; mismatch = отказ | FR-03, FR-05, Q-09 |
| RK-07 расползание в оркестратор | конечные команды; один `run_id`; OUT-01/OUT-08 | FR-05, FR-09, §7 |

## 10. Открытые вопросы

- **Q-01 · owner_role: product · blocking: false.** Первый artifact store
  (чартер Q-A): CI artifact service forge, объектное хранилище владельца или
  каталог на управляемом томе? Рабочее допущение требований: абстракция
  store с одним публичным контрактом (put/get/list/delete + acl + версия/
  ETag для immutability) и **первый адаптер — локальный управляемый том**;
  облачный адаптер и его расходы — отдельное одобрение владельца (CON-08)
  до подключения, не до design.
- **Q-02 · owner_role: architects · blocking: false.** Достаточно ли
  локального durable spool как acknowledgement call-start (с асинхронной
  публикацией) для инварианта 5, или ack обязан приходить от внешнего store
  до spawn (чартер Q-B)? Требование FR-02 фиксирует только «без ack —
  без `Popen`» и таймаут; design обязан показать, какой канал проходит
  NFR-01 при потере машины. Предложение чартера: внешний ack по умолчанию,
  spool-only — явно включённый режим с пометкой в closure.
- **Q-03 · owner_role: architects · blocking: false.** Формат WIP artifact
  (чартер Q-C): `git bundle` + encrypted tar или access-controlled ref на
  forge? Требование FR-04 — байт-идентичное восстановление и те же digests/
  redaction/retention, что у остального checkpoint-а. Предложение: `git
  bundle` — не требует прав push и переносим между forge.
- **Q-04 · owner_role: product · blocking: false.** Подтвердить имена
  поверхностей, принятые требованиями (чартер Q-D): `spec-runner restore
  <run_id> --into <dir>`, `spec-runner evidence <run_id> [--json]`,
  аудируемая дверь закрытия open call (рабочее имя `spec-runner evidence
  close-call <run_id> --call <call_id> --reason …`), аддитивные `run_id` /
  `pipeline_id` в `status --json` и `--json-result`. `run --resume`
  отклонён: продолжение — всегда отдельный `run` после конечного `restore`
  (CON-05).
- **Q-05 · owner_role: architects · blocking: false.** Публикация
  checkpoint-а: синхронно в точке mutation или локальный snapshot синхронно
  + асинхронная доставка с ack, на который ссылается closure? Что именно
  считается «доступен вне машины» для NFR-02 (60 s) и как closure ждёт
  последний ack без бесконечного ожидания.
- **Q-06 · owner_role: architects · blocking: false.** Где живёт единый
  seam call-start/call-result (FR-02): spawn-точка `runner.py`, расширение
  `prompts_log`, новый модуль — и как через него проходят `cli_plan.py`
  (сегодня `subprocess.run` инъекцией), `review_pr.py` и `doctor.py`, не
  создавая второй путь к бинарю провайдера.
- **Q-07 · owner_role: architects · blocking: false.** Состав policy
  identity в call-start/run-start/manifest: `config_hash` над
  `gates.POLICY_KEYS` достаточно, или добавить effective namespace, budget
  ceiling snapshot, версию контракта, CLI/model провайдера? Критерий:
  restore должен отказать на любом расхождении, которое меняет допустимость
  следующего шага, и не отказывать на косметических.
- **Q-08 · owner_role: architects · blocking: false.** Движок redaction
  (NFR-05): паттерны + entropy-эвристика, denylist из окружения процесса
  (значения переменных с `TOKEN`/`KEY`/`SECRET`), внешний инструмент? Где
  живёт корпус 100 секретов и как он не становится сам утечкой в репо
  (синтетические значения известной формы).
- **Q-09 · owner_role: product · blocking: false.** Допустимо ли, что
  `restore` записывает восстановленный effective namespace как
  `tdd_namespace` в config восстановленного каталога (FR-05, RK-06)? Это
  правка проектного файла оператора; альтернатива — отказ с инструкцией
  «объявите `tdd_namespace: <value>` и повторите». Рабочее допущение —
  запись с явным выводом diff-а.
- **Q-10 · owner_role: product · blocking: false.** NFR-02 и NFR-03
  назначены `Should` (бенчмарк и ручной drill, не CI-гейт), остальные NFR —
  `Must`. Бриф приоритетов NFR не задавал. Подтвердить или поднять до
  `Must` с указанием CI-измеримого критерия.
- **Q-11 · owner_role: architects · blocking: false.** Кто исполняет
  retention (NFR-07): lifecycle-политика store по метаданным
  (`run_id`, kind, closure time) или команда spec-runner
  (`evidence purge`)? Где лежит audit-запись удаления — в store или в
  compliance `AuditLogger`.
- **Q-12 · owner_role: architects · blocking: false.** Обнаружение open
  call при старте обычного `run` в том же namespace (FR-02): читать store
  (сетевой вызов на каждом старте) или локальную DB, в которую call-start
  тоже записан? Второе требует, чтобы call-start в DB и в store были одной
  транзакционной единицей — иначе появляется второй домен (RK-03).

## 11. Условие завершения стадии

Требования считаются выполненными, когда все FR с приоритетом Must и все NFR
с приоритетом Must подтверждены перечисленными в §9 тестами, зелёными в CI
на каждом PR (`-m "not slow"`, fault-injection — под `slow` хотя бы раз
перед снятием experimental), NFR-02/NFR-03 измерены бенчмарком и ручным
drill-ом с записью результата, restore-drill (M-01) и open-call матрица
(M-02) выполнены не менее чем на 20 и 10 прогонах, три реальных прогона
перенесены (M-05), FR-09 либо реализован, либо явно вырезан решением
владельца с записью в CHANGELOG, `docs/architecture.md` (#478) дополнен
описанием контракта, manifest/evidence/closure схемы версионированы в
`schemas/`, `#480` закрыт ссылкой на PR с evidence, пункт
`runtime-state-artifact-export` в `TODO.md` закрыт, контракт
`run_id`/`pipeline_id` объявлен соседям (devtools, Maestro) issue-ом без
правки их файлов.

## Источники

- `00-charter.md` (blob `54de41d0…`) — цель, объём, инварианты 1–13,
  критерии приёмки, риски RK-01…RK-07, вопросы Q-A…Q-D.
- `00-discovery/brief.md` (customer-фрейм, blob `3f6e9807…`) —
  G/P/J/FR/NFR/CON/M/OUT; источник требований, ID неизменны.
- Issue #480 (inbox) — постановка; наследует инвентаризацию #478.
- `docs/architecture.md:229` — «Runtime-state inventory and delivery policy
  (#478)»: SSOT владения и retention; «Required delivery mechanism».
- `TODO.md:768` — `runtime-state-artifact-export` (#480, открыт).
- `src/spec_runner/cli.py:2504`, `audit_log.py:120`, `obs.py:250` — три
  идентификатора прогона.
- `src/spec_runner/state.py:1900` (`record_agent_call`), `:2171`
  (`record_attempt`), `:2352` (`_enter_degraded_mode`); таблицы `attempts`,
  `agent_calls`, `red_checkpoints`, `tdd_claims`, `tdd_phases`,
  `tdd_remedies`, `phase_waivers`, `waivers_applied`, `budget_authorizations`,
  `gate_verdicts`, `verify_evidence`, `pr_*`.
- `src/spec_runner/tdd.py:219` — `resolve_namespace`; `hooks.py:132` —
  rescue stash label; `git_ops.uncommitted_work_paths` (#229).
- `src/spec_runner/prompts_log.py` — `log_prompt`/`append_output`/
  `append_not_started`/`bound` (#282/#295/#296).
- `src/spec_runner/gates.py:107` — `POLICY_KEYS`; `phases.py` — `Refusal`
  (#230); `runner.py` — `classify_agent_answer` (#241).
- Прецеденты fail-closed: #192, #213, #231, #273, #296.
