---
spec_stage: charter
status: draft
owner_role: product
traces_to:
- discovery-brief
upstream_hashes:
  discovery-brief: 3f6e98075ebcf037371445458cc3a2cd752dd830
---

# Charter — Durable continuation checkpoint и evidence для run/call/attempt (spec-runner#480)

Инвариант конвейера, принятый инвентаризацией #478: **факт, необходимый для
продолжения или аудита прогона, не существует только на машине оператора.**
Сегодня spec-runner его не выполняет. `tasks.md` в Git восстанавливает очередь,
но не восстанавливает claims, RED checkpoints, verify-evidence, gate verdicts,
remedies и waivers, budget authorizations, review-loop state, стоимость и
выходы неудачных или частичных вызовов — всё это живёт в
`spec/.executor-state.db` и `spec/.executor-logs/`, которые никуда не
доставляются. Планирующие вызовы (`plan --full`, `plan --gated`) вообще не
имеют ledger-identity, а единственный существующий канал доставки —
`post_review` — срабатывает только на успешном completion-path задачи.

Workstream закрывает разрыв двумя связанными, но разными артефактами:
**continuation checkpoint** (приватный консистентный снимок состояния плюс
Git-материал и WIP, нужные для следующего шага) и **evidence bundle**
(immutable, append-only след каждого платного вызова, terminal attempt и
завершения прогона), связанными одним каноническим `run_id`. Форма решения
задана владельцем в customer-брифе (CON-02…CON-06) и в
`docs/architecture.md#runtime-state-inventory-and-delivery-policy-478`.

## Контекст и проблема

### Как это устроено сегодня

- **Три несвязанных идентификатора прогона.** `cli.py:2508` привязывает к
  structlog обрезанный `uuid4().hex[:8]` под именем `run_id`; `AuditLogger`
  (`audit_log.py:120`) чеканит собственный полный UUID; `obs.init_logging`
  (`obs.py:250`) чеканит ULID `pipeline_id`. Ни один из них не попадает в
  строки `attempts`, `agent_calls` или в prompt-артефакты. Аудитор не может
  соединить OTel-событие, audit-запись и стоимость вызова одним ключом.
- **Оплата предшествует записи.** Ledger `agent_calls` (`state.py:485`) и
  `record_attempt` пишутся **после** возврата subprocess. Падение процесса
  между стартом провайдера и записью результата не оставляет следа: следующий
  запуск видит задачу как нетронутую и молча оплачивает вызов повторно.
  `prompts_log` (#282/#296) оставляет файл с prompt-ом до старта, но только на
  локальном диске и без identity вызова.
- **Планирование вне ledger-а.** `agent_calls` индексирован по `task_id`;
  `cli_plan.py` не пишет ни одной строки ledger-а. Стоимость `plan --full` и
  gated planning не атрибутируема.
- **Degraded mode теряет факты.** При `OperationalError` на `record_attempt`
  (`state.py:2261`) состояние продолжает жить только в памяти процесса;
  оператора уведомляют один раз, но после завершения процесса attempt, его
  стоимость и причина остановки исчезают.
- **Namespace привязан к абсолютному пути.** `tdd.resolve_namespace`
  (`tdd.py:219`) без явного `tdd_namespace` хеширует
  `Path(project_root).resolve() | spec_prefix`. Перенос каталога меняет
  namespace, и все active claims и checkpoints становятся невидимы — claims
  gate молча пропускает то, что был обязан заморозить.
- **Незавершённая работа локальна.** `hooks.rescue_uncommitted` (`hooks.py:95`)
  прячет dirty/untracked work в локальный stash; local-only commits на
  task-ветке до `push` существуют в одном клоне. После потери каталога
  восстановить «где остановились» нельзя.
- **Нет записи о завершении.** Ни один orderly exit — completed, no-ready,
  validation failure, budget refusal, session timeout, operator stop — не
  оставляет записи вне stderr. Отличить «прогон завершился штатно без работы»
  от «процесс убит» постфактум невозможно.

### Что уже есть и не нужно изобретать

- **Append-only таблицы с полной семантикой владения**: `attempts`,
  `agent_calls`, `red_checkpoints`, `tdd_claims`, `tdd_phases`,
  `tdd_remedies`, `phase_waivers`/`waivers_applied`, `budget_authorizations`,
  `gate_verdicts`, `verify_evidence`, `pr_*` (`state.py:348`–`:612`,
  `review_pr.py:297`–`:342`). Logical export — это они, без новой модели.
- **Провенанс платного вызова**: `provenance` (`red`, `green`, `review`,
  `review:<role>`) уже проходит через `record_agent_call`, `log_prompt` и
  budget guard — `call_id` и `run_id` добавляются к существующему seam-у, не
  создают второй.
- **Инвариант терминальной секции** prompt-артефакта (#295/#296): «файл без
  терминальной секции = runner умер mid-call» — это ровно семантика open call,
  которую нужно сделать durable и адресуемой.
- **Инвентаризация #478** (`docs/architecture.md:229`): матрица объектов с
  классом continuation/evidence/temporary и решением по доставке. Она остаётся
  SSOT владения и retention; charter её не пересказывает.
- **`ExecutorLock` с PID-диагностикой, stop-marker, `.gitignore`-пояс
  `.executor-*`** — временное состояние, которое явно **не** доставляется.

## Цель

Сделать так, чтобы **любой прогон, остановленный на закрытой durable-границе,
можно было продолжить или проверить с другой машины по одному `run_id`**:
без повторной оплаты записанного вызова, без потери active claims, authority
и WIP, и без догадок — всё, что нельзя доказать (open call, несовпадение
identity/digest/namespace, повреждённый артефакт), останавливает продолжение
как `needs-human` с точной причиной до платного вызова и до claims gate.

## Пользователи и ожидаемая ценность

- **P-01 Оператор spec-runner** — primary. Прерванный на ноутбуке, в терминале
  или CI-runner прогон восстанавливается в новом каталоге конечной командой;
  следующий шаг вычисляется, а не реконструируется вручную (J-01, J-02).
- **P-02 Владелец репозитория/workstream-а** — платит за вызовы. Каждая
  единица расхода имеет call-start, исход и связь с санкцией (budget
  authorization, waiver, remedy), которая её разрешила; расходы на хранение
  атрибутируемы тем же `run_id` (J-03, CON-08).
- **P-03 Независимый ревьюер/аудитор** — без доступа к машине по `run_id`
  видит closure, open calls, attempts, evidence с исходами и стоимостью, и
  либо следующий безопасный шаг, либо причину, почему он не доказуем (FR-09,
  M-06).
- **P-04 Владелец платформы и безопасности** — артефакты идут только в
  приватное авторизованное хранилище, с redaction, ролевым доступом и
  retention; ни байта в продуктовый Git (CON-04, NFR-05, NFR-07).

## Требования (из customer-брифа, ID неизменны)

### Функциональные

- **FR-01** · Must — Назначать каждому CLI-прогону один глобально уникальный
  `run_id` и связывать с ним все вызовы, attempts, checkpoints, evidence и
  завершение. *Приёмка:* один full UUIDv4 создаётся ровно один раз на
  invocation и без изменения присутствует в structlog/OTel, audit, call
  records, checkpoint manifest, evidence bundle и run closure; `pipeline_id`
  хранится отдельно.
- **FR-02** · Must — Записывать durable-намерение перед каждым платным
  subprocess и запрещать запуск без подтверждения записи. *Приёмка:* перед
  стартом процесса существует подтверждённый call-start с `run_id`,
  `call_id`, stage/provenance, policy identity и optional task/attempt; сбой
  после call-start без результата оставляет open call и не допускает silent
  retry.
- **FR-03** · Must — Публиковать согласованный continuation checkpoint после
  каждого изменения состояния, необходимого для продолжения. *Приёмка:* после
  attempts, checkpoints, claims, verify-evidence, waiver, remedy и budget
  authorization доступен новый checkpoint; тест с WAL-only записью
  восстанавливает её из checkpoint и краснеет при простой копии main DB.
- **FR-04** · Must — Включать в continuation checkpoint Git-материал и
  незавершённую работу, без которых следующий шаг нельзя воспроизвести.
  *Приёмка:* после удаления исходного каталога восстанавливаются
  опубликованные commits/refs, local-only commits, dirty или untracked work и
  runner-created rescue stash; lock, stop-файлы и временные worktrees не
  восстанавливаются.
- **FR-05** · Must — Восстанавливать прогон по `run_id` в новом рабочем
  каталоге и вычислять следующий безопасный шаг. *Приёмка:* восстановление в
  другом абсолютном пути возвращает active claims, budget authority,
  effective TDD namespace и WIP; несовпадение repository, config или
  namespace отказывает до paid call и claims gate.
- **FR-06** · Must — Публиковать immutable evidence для каждого платного
  вызова и terminal task attempt независимо от исхода. *Приёмка:* task
  execution, review, `plan --full` и gated planning создают адресуемые call
  records; success, failed, blocked, timeout и infrastructure error содержат
  identity, стоимость, bounded/redacted prompt и result либо их digests.
- **FR-07** · Must — Создавать отдельную immutable run-closure запись для
  каждого штатного завершения или ранней остановки. *Приёмка:* completed,
  no-ready, validation failure, budget или policy refusal, session timeout,
  infrastructure error и operator stop создают closure с причиной и ids
  последних checkpoint/evidence; run-start без closure трактуется как
  crash/unknown.
- **FR-08** · Must — Сохранять continuation-relevant mutation в аварийный
  durable spool при отказе основной state DB. *Приёмка:* ошибка SQLite при
  записи attempt сохраняет payload и ordering/join keys в spool и
  восстанавливается новым процессом; одновременный отказ DB и spool
  останавливает выполнение до следующего платного вызова.
- **FR-09** · Should — Давать оператору и аудитору read-surface по `run_id`
  без необходимости восстанавливать весь рабочий каталог. *Приёмка:* по
  `run_id` можно получить последний checkpoint, closure, открытые calls,
  список attempts и evidence-ссылки с исходами и стоимостью; данные читаются
  с другой машины без доступа к исходному клону.

### Нефункциональные

- **NFR-01** · durability — Не терять ни одной подтверждённой
  continuation-relevant mutation. *Цель:* RPO 0 для записей, подтверждённых
  DB, spool или artifact store; 1000 fault-injection падений в каждой
  write-ahead границе дают 0 потерянных подтверждённых mutations и 0 silent
  retries.
- **NFR-02** · latency — Не добавлять существенную задержку перед платными
  вызовами и между шагами. *Цель:* durable call-start acknowledgement
  p95 ≤ 1 s и p99 ≤ 3 s; новый checkpoint для reference workload доступен вне
  машины p95 ≤ 60 s при здоровом хранилище.
- **NFR-03** · recovery time — Восстанавливать прогон достаточно быстро для
  штатного операторского handoff. *Цель:* полное восстановление набора до
  1 GiB в новом каталоге ≤ 15 min при сети ≥ 100 Mbit/s; после загрузки
  валидация и вычисление следующего безопасного шага ≤ 60 s.
- **NFR-04** · integrity — Обнаруживать повреждение, неполноту и подмену
  любого опубликованного артефакта. *Цель:* 100 % файлов manifest,
  checkpoint, WIP и evidence имеют SHA-256; изменение любого байта или
  отсутствие обязательного файла даёт fail-closed до restore, paid call или
  claims gate.
- **NFR-05** · confidentiality — Защищать приватный source, prompts и
  provider output. *Цель:* TLS 1.2+ при передаче и AES-256 либо эквивалентное
  managed encryption в покое; доступ только явно назначенным ролям; тестовый
  корпус из 100 секретов даёт 0 известных секретов в опубликованных bounded
  logs.
- **NFR-06** · bounded logs — Ограничивать объём публикуемых prompt и result
  logs без потери доказательства исходных байтов. *Цель:* не более 1 MiB
  redacted prompt и 4 MiB redacted result на call; при усечении сохраняются
  SHA-256 полного исходного содержимого, исходный размер и явный marker
  `truncated`.
- **NFR-07** · retention — Иметь предсказуемую retention и своевременное
  удаление приватных артефактов. *Цель:* по умолчанию checkpoints хранятся до
  closure плюс 30 дней, evidence и closure — 180 дней; политика настраивается
  в диапазоне 7–365 дней; санкционированное удаление завершается в течение
  24 h и оставляет audit-запись без удалённого содержимого.

## Объём

- **Одна identity на прогон и на вызов.** Full UUIDv4 `run_id`, созданный
  один раз в `main()` и пробрасываемый в structlog/OTel, `AuditLogger`,
  `agent_calls`, prompt-артефакты, manifest и closure; full UUIDv4 `call_id`
  на каждый платный subprocess; `pipeline_id` — отдельное поле рядом, не
  замена. Существующий обрезанный display-id и независимый audit-UUID
  заменяются, не дублируются. Покрывает FR-01.
- **Write-ahead протокол публикации**: durable run-start → durable call-start
  до каждого платного subprocess (task RED authoring, RED agent round (#220),
  GREEN, review и review-роли, `plan --full`, gated planning, `review-pr`
  verify/fix, `doctor`) → call-result/attempt → run-closure на каждом orderly
  exit. Без acknowledgement call-start процесс не стартует. Покрывает FR-02,
  FR-06, FR-07.
- **Continuation checkpoint**: консистентный снимок state DB через SQLite
  backup API (или эквивалентный транзакционный snapshot, включающий
  WAL-only страницы) + версионированный manifest (repository, workstream,
  ref, HEAD, config hash, **effective TDD namespace и способ его получения**
  declared/computed, join keys, digests, явный перечень исключённых полей) +
  Git material: reachability commit-а из published ref, local-only commits,
  dirty/untracked work и rescue stash как encrypted WIP artifact или
  access-controlled ref. Обновляется после каждой continuation-relevant
  mutation (FR-03). Покрывает FR-03, FR-04.
- **Restore по `run_id`** в новом абсолютном пути: конечная команда, которая
  проверяет digests и identity, применяет сохранённый effective namespace,
  восстанавливает WIP и вычисляет следующий безопасный шаг, либо
  останавливается `needs-human` с точной причиной. Покрывает FR-05.
- **Evidence bundle**: immutable call records и logical append-only export
  таблиц terminal attempt-а, с bounded/redacted prompt+result и SHA-256
  полного содержимого; failed/blocked/timeout/infrastructure error
  доставляются так же, как success. Покрывает FR-06.
- **Emergency spool**: независимый от SQLite durable append-log payload-ов и
  join keys для mutation, оставшихся in-memory после `OperationalError`;
  restore и следующий процесс его читают; отказ DB и spool одновременно —
  fail-closed на текущей безопасной границе. Покрывает FR-08.
- **Read-surface по `run_id`** (FR-09, Should): последний checkpoint,
  closure, open calls, attempts, evidence-ссылки с исходами и стоимостью, без
  клона.
- **Интеграция с хранилищем через его публичный контракт**: приватный
  orchestrator-managed artifact store или эквивалентный CI artifact service;
  redaction, ролевой доступ, retention 7–365 дней и аудит удаления. Выбор
  конкретного хранилища и любые новые расходы — отдельное одобрение владельца
  (CON-08).
- **Legacy-режим** для прогонов, начатых до нового контракта: диагностика и
  явный ручной путь либо fail-closed с перечнем недостающих гарантий; никакой
  автоматической миграции (CON-07).
- **Документация**: `docs/architecture.md#runtime-state-inventory-and-delivery-policy-478`
  остаётся матрицей владения и retention и дополняется описанием контракта;
  README/CHANGELOG описывают команды, формат manifest и версию контракта.

## Вне объёма

Зафиксировано брифом (OUT-01…OUT-10), здесь — только чтобы workstream не
расширился молча:

- Никакого scheduler-а, распределённого оркестратора, always-on
  control-plane, очереди работников или автоматического failover (OUT-01,
  CON-05).
- Никакого exactly-once, отмены или дедупликации у платного провайдера:
  open call — это `unknown`, решение о повторе принимает человек (OUT-02,
  CON-03).
- Никакого собственного object storage, IAM, KMS или retention-service —
  только интеграция по публичному контракту выбранной инфраструктуры
  (OUT-03).
- Checkpoint не заменяет Git и не является backup-ом машины: ни образов ОС,
  virtualenv, tool caches, временных worktrees, locks, процессов (OUT-04,
  OUT-05).
- Правила claims, TDD namespace, waiver, remedy, budget approval и review не
  меняются: checkpoint сохраняет и при restore пересверяет их эффективное
  состояние (OUT-06).
- Никакой автоматической миграции старых прогонов и ретроактивных гарантий
  (OUT-07).
- Никаких межпроектных dashboards, трендов, billing или аналитики портфеля —
  только чтение по одному `run_id` (OUT-08).
- Никакого автоматического «лечения» повреждённого checkpoint-а, отсутствующего
  evidence или неизвестного шага: правильный ответ — `stopped/needs-human`
  с причиной (OUT-09).
- Никакого бессрочного хранения и обхода утверждённых правил удаления,
  доступа или legal hold (OUT-10).

## Продуктовые инварианты

1. **Один `run_id`, везде одинаковый.** Full UUIDv4, созданный один раз на
   invocation; во всех каналах — OTel, audit, ledger, prompt-артефакты,
   manifest, closure — байт в байт тот же. `pipeline_id` лежит рядом и никогда
   его не подменяет (FR-01, M-03).
2. **Запись раньше траты.** Ни один платный subprocess не стартует без
   подтверждённого call-start. Один `call_id` — ровно один durable call-start
   и ровно один исход: результат либо явно открытое `unknown` (FR-02, M-03).
3. **Open call — не пусто, а неизвестно.** Call-start без результата после
   crash означает «мог быть запущен и оплачен»; restore никогда не повторяет
   его молча — только `needs-human` (FR-02, CON-03, M-02).
4. **Нет closure — значит crash.** Каждый orderly exit, включая no-ready и
   ранний refusal без attempt, пишет closure; run-start без closure читается
   как crash/unknown, никогда как пустой успех (FR-07, M-04).
5. **Подтверждено — значит не потеряно.** Mutation, которую подтвердили DB,
   spool или artifact store, переживает любое падение; неподтверждённая
   останавливает исполнение на текущей безопасной границе, а не после
   следующего вызова (FR-08, NFR-01).
6. **Checkpoint — это snapshot, а не копия файла.** WAL-only записи входят в
   checkpoint; тест обязан различать backup API и `cp .db` (FR-03).
7. **Переносимость без локальных фактов.** Абсолютные пути, временные
   worktrees, lock-файлы, stop-marker и process identity — не часть
   контракта; effective TDD namespace хранится явно вместе со способом его
   получения, и несовпадение с активным — отказ до paid call и claims gate
   (FR-05, CON-06).
8. **Восстанавливается ровно то, что нужно для следующего шага.**
   Published refs, local-only commits, dirty/untracked work, rescue stash —
   да; lock, stop, temp worktrees — никогда, и они не становятся authority
   (FR-04).
9. **Целостность прежде всего.** Каждый файл имеет SHA-256; любой изменённый
   байт или отсутствующий обязательный файл — fail-closed (NFR-04, M-07).
10. **Ни байта в продуктовый Git.** Checkpoint, evidence, prompts, provider
    output, SQLite и WIP идут только в приватное авторизованное хранилище с
    redaction, доступом по ролям и retention (CON-04, NFR-05, NFR-07, M-07).
11. **Усечение не уничтожает доказательство.** Bounded log хранит SHA-256 и
    размер полного содержимого и явный marker `truncated` (NFR-06).
12. **Существующие контракты не трогаем.** Провайдеры вызываются как сегодня;
    claims/TDD/waiver/remedy/budget/review — по существующим правилам;
    `.executor-state.db` schema и `--json-result` меняются только аддитивно
    или с major bump (CON-02, CON-03, OUT-06).
13. **Экспериментально до полного набора.** Пока FR-01–FR-08 не приняты
    целиком, ни один частичный срез не называется «безопасным продолжением
    прогона» (CON-01).

## Критерии приёмки

- **Identity.** Тест поднимает прогон с `ORCHESTRA_PIPELINE_ID`, выполняет
  одну задачу с review и проверяет: OTel JSONL, audit-log, `agent_calls`,
  prompt-артефакты, checkpoint manifest и closure содержат один и тот же full
  UUIDv4 `run_id` и тот же `pipeline_id` в отдельном поле; второй invocation
  получает другой `run_id` (FR-01, M-03).
- **Write-ahead.** Fault-injection на каждой из границ (после run-start, после
  call-start до spawn, после spawn до результата, после результата до
  attempt, после attempt до checkpoint) в 1000 повторениях: 0 потерянных
  подтверждённых mutations, 0 платных вызовов без call-start, 0 автоматических
  повторов open call; restore после падения между spawn и результатом выдаёт
  `needs-human` до любого subprocess (FR-02, NFR-01, M-02).
- **Не стартует без acknowledgement.** Двойник artifact store, отказывающий в
  call-start, → ни одного `Popen`, задача останавливается на безопасной
  границе, closure с причиной записана (FR-02, FR-07).
- **WAL-разница.** Тест записывает attempt, не давая SQLite чекпоинтить WAL,
  снимает checkpoint и восстанавливает его в новом каталоге: attempt виден;
  тот же тест с простым копированием main DB красный (FR-03).
- **Restore-drill.** Прогон с активным claim, budget authorization, waiver,
  local-only commit на task-ветке, dirty work и rescue stash; исходный
  каталог удаляется; restore по `run_id` в другой абсолютный путь возвращает
  все перечисленные факты, effective namespace совпадает с manifest, claims
  gate видит старые claims, следующий шаг вычисляется без оплаты уже
  записанного вызова (FR-04, FR-05, M-01, M-05).
- **Namespace mismatch.** Тот же checkpoint, но активный config даёт другой
  effective namespace → отказ до paid call и до claims gate, с указанием
  обоих значений и способа получения (FR-05, CON-06).
- **Evidence на каждом исходе.** Матрица success / `TASK_FAILED` / blocked /
  timeout / infrastructure error × task GREEN / review / review-роль /
  `plan --full` / gated planning: каждая клетка оставляет адресуемый call
  record с identity, стоимостью (или явным `unknown`), bounded prompt/result и
  SHA-256 полного содержимого (FR-06, NFR-06).
- **Closure на каждом exit.** Матрица completed / no-ready / validation
  failure / budget refusal / policy refusal / session timeout / infrastructure
  error / operator stop, в том числе до создания attempt: каждая оставляет
  closure с причиной и ids последних checkpoint/evidence; kill -9 не оставляет
  closure, и read-surface классифицирует прогон как crash/unknown (FR-07,
  M-04).
- **Spool.** Двойник SQLite, падающий на `record_attempt`, → attempt в spool,
  новый процесс восстанавливает его в DB и checkpoint; двойник, роняющий DB и
  spool одновременно, → исполнение остановлено до следующего платного вызова
  с closure `infrastructure error` (FR-08).
- **Read-surface.** По `run_id` без клона и без доступа к исходной машине
  доступны последний checkpoint, closure, open calls, attempts и
  evidence-ссылки с исходами и стоимостью (FR-09, M-06).
- **Integrity.** Изменение одного байта в любом файле checkpoint/evidence или
  удаление обязательного файла → restore, paid call и claims gate отказывают
  (NFR-04).
- **Redaction.** Корпус из 100 секретов в prompt и provider output → 0
  известных секретов в опубликованных bounded logs; SHA-256 и размер полного
  содержимого сохранены (NFR-05, NFR-06).
- **Latency.** Бенчмарк на reference workload: call-start ack p95 ≤ 1 s,
  p99 ≤ 3 s; checkpoint доступен вне машины p95 ≤ 60 s (NFR-02).
- **Recovery time.** Набор ~1 GiB восстанавливается ≤ 15 min при
  ≥ 100 Mbit/s; валидация и вычисление шага ≤ 60 s (NFR-03).
- **Retention.** Политика вне диапазона 7–365 дней отклоняется при загрузке
  config; санкционированное удаление оставляет audit-запись без содержимого
  (NFR-07).
- **Ни байта в Git.** После всех E2E `git status` продуктового репо не
  содержит ни checkpoint, ни evidence, ни spool; `.gitignore`-пояс покрывает
  их пути (CON-04, M-07).
- **Legacy.** Прогон, начатый без нового контракта, при restore даёт
  fail-closed с перечнем недостающих гарантий либо явный legacy-путь;
  никакой автоматической реконструкции (CON-07).
- **Не платим в тестах.** Все перечисленные тесты проходят с fake CLI под
  поясом `PaidBinaryReached`; ни одного реального провайдера.
- **Совместимость.** `tests/test_json_result_contract.py` и schema-тесты
  `.executor-state.db` зелёные без изменения ожиданий, кроме аддитивных
  полей `run_id`/`call_id`.

## Риски и меры

Бриф оставил раздел рисков незаполненным (`coverage.risks: missing`);
ниже — риски, зафиксированные product-ролью; engineer-фрейм уточнит их на
стадии design.

- **RK-01 · Latency на critical path.** Синхронный ack call-start перед каждым
  платным вызовом добавляет сетевой RTT к каждому шагу; при деградации
  хранилища прогон замедляется или встаёт. *Мера:* NFR-02 как измеримый
  бюджет; локальный durable spool как первая ступень acknowledgement с
  асинхронной публикацией — только если design докажет, что spool один
  удовлетворяет инварианту 5; иначе fail-closed с closure, не тихое
  продолжение.
- **RK-02 · Утечка секретов через prompt/result.** Prompts содержат
  приватный source и могут содержать секреты из окружения; provider output —
  тоже. *Мера:* redaction до публикации, корпус-тест NFR-05, bounded размеры
  NFR-06, доступ по ролям; publication без прохождения redaction невозможна.
- **RK-03 · Второй домен состояния.** Spool, checkpoint и bundle могут
  превратиться в конкурирующие источники истины с DB. *Мера:* инвариант —
  DB остаётся authority continuation; spool только доигрывается в DB при
  следующем старте; bundle — read-only evidence; ни один читатель не
  принимает решение по bundle, если DB/checkpoint доступны.
- **RK-04 · Ложное чувство восстановимости.** Частичная реализация (например,
  identity + evidence без spool) будет прочитана как «можно продолжать».
  *Мера:* CON-01 как инвариант 13; флаг/команда restore недоступны до
  приёмки FR-01–FR-08; CHANGELOG называет статус «experimental».
- **RK-05 · Стоимость и объём артефактов.** Checkpoint после каждой mutation
  на длинном прогоне — сотни снимков. *Мера:* только последний checkpoint
  обязателен для continuation (предыдущие — retention 30 дней после
  closure); стоимость хранения атрибутируется `run_id` и видна в `costs`
  (CON-08); новые расходы — отдельное одобрение владельца.
- **RK-06 · Namespace drift при переносе.** Восстановленный прогон с
  вычисленным namespace может незаметно разойтись с активным при следующем
  ручном переносе. *Мера:* manifest хранит declared/computed; restore
  рекомендует зафиксировать `tdd_namespace` явно; mismatch — всегда отказ, не
  предупреждение.
- **RK-07 · Расширение scope в оркестратор.** Соблазн добавить очередь,
  автоматический failover или межпроектные отчёты. *Мера:* OUT-01, OUT-08
  как явные границы; каждая новая команда обязана быть конечной и выполняться
  оператором (CON-05).

## Открытые вопросы

Бриф закрыт без открытых вопросов (0 blocking). Ниже — вопросы, которые
charter передаёт следующим стадиям; ни один не блокирует requirements.

- **Q-A · owner_role: product · blocking: false.** Какое хранилище считается
  первым «orchestrator-managed private artifact store»: CI artifact service
  текущего forge, объектное хранилище владельца или каталог на управляемом
  томе? Ответ определяет расходы по CON-08 и требует явного одобрения
  владельца до design. *Предложение:* абстракция хранилища с одним
  публичным контрактом (put/get/list/delete + acl) и первым адаптером для
  локального управляемого тома, чтобы приёмка не ждала облачного решения.
- **Q-B · owner_role: architect · blocking: false.** Достаточно ли локального
  durable spool как acknowledgement call-start (с асинхронной публикацией)
  для инварианта 5, или ack обязан приходить от внешнего хранилища до spawn?
  *Предложение:* внешний ack по умолчанию; spool-only — только как явно
  включённый режим с closure-предупреждением, если он вообще проходит
  NFR-01.
- **Q-C · owner_role: architect · blocking: false.** Формат WIP artifact для
  local-only commits, dirty work и rescue stash: `git bundle` + encrypted
  tar, или access-controlled ref в forge? *Предложение:* `git bundle` с
  шифрованием на стороне хранилища — не требует прав на push в чужой ref и
  переносим между forge.
- **Q-D · owner_role: product · blocking: false.** Имена CLI-поверхностей
  (`spec-runner run --resume <run_id>`, `spec-runner restore <run_id>`,
  `spec-runner evidence <run_id>`) и место `run_id` в `status`/`--json-result`.
  *Предложение:* аддитивные поля в существующих JSON-контрактах, отдельная
  команда для restore и read-surface; решить в requirements.

## Условие завершения

Workstream закончен, когда все критерии приёмки зелёные в CI на каждом PR с
fake CLI; матрица restore-drill (M-01) и матрица open-call (M-02) выполнены
хотя бы на 20 и 10 прогонах соответственно; не менее трёх реальных активных
прогонов перенесены в другой checkout или машину и продолжены без ручного
восстановления (M-05); инварианты 1–13 подтверждены тестами; manifest и формат
evidence версионированы и задокументированы; `docs/architecture.md` (#478)
обновлён как матрица владения и retention; CHANGELOG снимает статус
«experimental» только после приёмки FR-01–FR-08 целиком (CON-01); #480 закрыт
ссылкой на PR с evidence; пункт `runtime-state-artifact-export` в `TODO.md`
закрыт; контракт `run_id`/`pipeline_id` объявлен соседям (devtools, Maestro)
issue/handoff-ом без правки их файлов.

## Источники

- `workstreams/durable-continuation-checkpoint-evidence-20260915/spec/00-discovery/brief.md`
  (customer-фрейм, blob `3f6e9807…`) — G/P/J/FR/NFR/CON/M/OUT; источник
  требований, ID неизменны.
- Issue #480 «Durable continuation checkpoint и evidence для
  run/call/attempt» (inbox) — постановка и признаки готовности; наследует
  инвентаризацию #478.
- `docs/architecture.md:229` — «Runtime-state inventory and delivery policy
  (#478)»: матрица объектов, классов и решений по доставке; SSOT владения и
  retention.
- `TODO.md` — пункты `executor-state-inventory` (#478, закрыт) и
  `runtime-state-artifact-export` (#480, открыт).
- `src/spec_runner/cli.py:2508` — обрезанный display `run_id`;
  `src/spec_runner/audit_log.py:120` — независимый audit UUID;
  `src/spec_runner/obs.py:250` — ULID `pipeline_id`.
- `src/spec_runner/state.py:348`–`:612` — append-only таблицы состояния;
  `:485` — `agent_calls` (индекс по `task_id`); `:1900` —
  `record_agent_call`; `:2261` — degraded mode на `record_attempt`.
- `src/spec_runner/tdd.py:219` — `resolve_namespace` и fallback по
  абсолютному `project_root`.
- `src/spec_runner/hooks.py:95` — `rescue_uncommitted` (локальный stash).
- `src/spec_runner/prompts_log.py` — инвариант терминальной секции
  (#295/#296) как прообраз семантики open call.
- `src/spec_runner/review_pr.py:297`–`:342` — `pr_*` таблицы того же
  state-домена.
- Прецеденты fail-closed вместо fallback: #192 (bookkeeping), #213 (budget
  guard), #231 (rescue stash), #273 (tracked state DB), #296 (артефакт без
  терминальной секции).
