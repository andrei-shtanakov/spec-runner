---
schema: discovery-brief
schema_version: 1
spec_stage: discovery
status: draft
generated_by: discovery-runtime
generated_at: '2026-09-15T10:55:06Z'
validation: pass
interview:
  frame: customer
  sessions:
  - participant_role: spec-runner-owner
coverage:
  goals: covered
  personas: covered
  jobs: covered
  functions: covered
  nfr: covered
  constraints: covered
  success_metrics: covered
  out_of_scope: covered
  risks: missing
  gate_passed: true
open_questions: 0
blocking_open_questions: 0
conflicts: 0
traces_to: []
---

# Discovery Brief — andrei-shtanakov/spec-runner (customer-фрейм)

## Goals

- **G-01** Позволить безопасно продолжить или проверить прерванный прогон spec-runner с другой машины без повторения уже оплаченных вызовов и без утраты фактов, определяющих допустимый следующий шаг
- **G-02** Обеспечить адресуемый вне машины evidence-след каждого платного вызова и каждого завершения прогона, включая failed, blocked, early-stop и crash-unknown пути
- **G-03** Сделать восстановление и независимый аудит прогона по run_id штатной и однозначной операцией после полной потери исходного рабочего каталога

## Personas

- **P-01** Оператор spec-runner, запускающий, наблюдающий и возобновляющий длительные прогоны
- **P-02** Владелец репозитория или workstream-а, оплачивающий вызовы и утверждающий бюджетные, waiver и remedy-решения
- **P-03** Независимый ревьюер или аудитор, проверяющий ход и результат прогона без доступа к исходной машине
- **P-04** Владелец платформы и безопасности, отвечающий за доступ, redaction и retention приватных checkpoint и evidence-артефактов

## Jobs-to-be-done

- **J-01** Когда длительный прогон прерывается из-за завершения машины, терминала или CI-runner, я хочу восстановить его с последней подтверждённой границы, чтобы продолжить без молчаливого повтора уже оплаченного вызова
  traces: [G-01, G-03]
- **J-02** Когда workstream передаётся другому оператору или переносится в новый рабочий каталог, я хочу восстановить continuation-state и следующий допустимый шаг по run_id, чтобы не реконструировать claims и authority-решения вручную
  traces: [G-01, G-03]
- **J-03** Когда я проверяю расходы и исходы прогонов за неделю, я хочу получить полный evidence-след success, failed, blocked, refusal и crash-unknown путей, чтобы подтвердить стоимость и обоснованность каждого продолжения
  traces: [G-02, G-03]

## Functional Requirements

- **FR-01** Назначать каждому CLI-прогону один глобально уникальный run_id и связывать с ним все вызовы, attempts, checkpoints, evidence и завершение
  **Priority**: Must
  **Acceptance**: Один full UUIDv4 создаётся ровно один раз на invocation и без изменения присутствует в structlog/OTel, audit, call records, checkpoint manifest, evidence bundle и run closure; pipeline_id хранится отдельно
  traces: [J-01, J-02, J-03]
- **FR-02** Записывать durable-намерение перед каждым платным subprocess и запрещать запуск без подтверждения записи
  **Priority**: Must
  **Acceptance**: Перед стартом процесса существует подтверждённый call-start с run_id, call_id, stage/provenance, policy identity и optional task/attempt; сбой после call-start без результата оставляет open call и не допускает silent retry
  traces: [J-01, J-03]
- **FR-03** Публиковать согласованный continuation checkpoint после каждого изменения состояния, необходимого для продолжения
  **Priority**: Must
  **Acceptance**: После attempts, checkpoints, claims, verify-evidence, waiver, remedy и budget authorization доступен новый checkpoint; тест с WAL-only записью восстанавливает её из checkpoint и краснеет при простой копии main DB
  traces: [J-01, J-02]
- **FR-04** Включать в continuation checkpoint Git-материал и незавершённую работу, без которых следующий шаг нельзя воспроизвести
  **Priority**: Must
  **Acceptance**: После удаления исходного каталога восстанавливаются опубликованные commits/refs, local-only commits, dirty или untracked work и runner-created rescue stash; lock, stop-файлы и временные worktrees не восстанавливаются
  traces: [J-01, J-02]
- **FR-05** Восстанавливать прогон по run_id в новом рабочем каталоге и вычислять следующий безопасный шаг
  **Priority**: Must
  **Acceptance**: Восстановление в другом абсолютном пути возвращает active claims, budget authority, effective TDD namespace и WIP; несовпадение repository, config или namespace отказывает до paid call и claims gate
  traces: [J-01, J-02, G-03]
- **FR-06** Публиковать immutable evidence для каждого платного вызова и terminal task attempt независимо от исхода
  **Priority**: Must
  **Acceptance**: Task execution, review, plan --full и gated planning создают адресуемые call records; success, failed, blocked, timeout и infrastructure error содержат identity, стоимость, bounded/redacted prompt и result либо их digests
  traces: [J-03, G-02]
- **FR-07** Создавать отдельную immutable run-closure запись для каждого штатного завершения или ранней остановки
  **Priority**: Must
  **Acceptance**: completed, no-ready, validation failure, budget или policy refusal, session timeout, infrastructure error и operator stop создают closure с причиной и ids последних checkpoint/evidence; run-start без closure трактуется как crash/unknown
  traces: [J-01, J-03]
- **FR-08** Сохранять continuation-relevant mutation в аварийный durable spool при отказе основной state DB
  **Priority**: Must
  **Acceptance**: Ошибка SQLite при записи attempt сохраняет payload и ordering/join keys в spool и восстанавливается новым процессом; одновременный отказ DB и spool останавливает выполнение до следующего платного вызова
  traces: [J-01, J-03]
- **FR-09** Давать оператору и аудитору read-surface по run_id без необходимости восстанавливать весь рабочий каталог
  **Priority**: Should
  **Acceptance**: По run_id можно получить последний checkpoint, closure, открытые calls, список attempts и evidence-ссылки с исходами и стоимостью; данные читаются с другой машины без доступа к исходному клону
  traces: [J-02, J-03]

## Non-Functional

- **NFR-01** Не терять ни одной подтверждённой continuation-relevant mutation
  **Target**: RPO 0 для записей, подтверждённых DB, spool или artifact store; 1000 fault-injection падений в каждой write-ahead границе дают 0 потерянных подтверждённых mutations и 0 silent retries
  traces: [FR-02, FR-03, FR-08]
- **NFR-02** Не добавлять существенную задержку перед платными вызовами и между шагами
  **Target**: Durable call-start acknowledgement p95 <= 1 s и p99 <= 3 s; новый checkpoint для reference workload доступен вне машины p95 <= 60 s при здоровом хранилище
  traces: [FR-02, FR-03]
- **NFR-03** Восстанавливать прогон достаточно быстро для штатного операторского handoff
  **Target**: Полное восстановление набора до 1 GiB в новом каталоге занимает <= 15 min при сети >= 100 Mbit/s; после загрузки валидация и вычисление следующего безопасного шага занимают <= 60 s
  traces: [FR-04, FR-05]
- **NFR-04** Обнаруживать повреждение, неполноту и подмену любого опубликованного артефакта
  **Target**: 100% файлов manifest, checkpoint, WIP и evidence имеют SHA-256; изменение любого байта или отсутствие обязательного файла даёт fail-closed до restore, paid call или claims gate
  traces: [FR-03, FR-04, FR-05, FR-06, FR-07]
- **NFR-05** Защищать приватный source, prompts и provider output
  **Target**: Шифрование TLS 1.2+ при передаче и AES-256 либо эквивалентное managed encryption в покое; доступ только явно назначенным ролям; тестовый корпус из 100 секретов даёт 0 известных секретов в опубликованных bounded logs
  traces: [FR-06, FR-09]
- **NFR-06** Ограничивать объём публикуемых prompt и result logs без потери доказательства исходных байтов
  **Target**: Не более 1 MiB redacted prompt и 4 MiB redacted result на call; при усечении сохраняются SHA-256 полного исходного содержимого, исходный размер и явный marker truncated
  traces: [FR-06]
- **NFR-07** Иметь предсказуемую retention и своевременное удаление приватных артефактов
  **Target**: По умолчанию checkpoints хранятся до closure плюс 30 дней, evidence и closure — 180 дней; политика настраивается в диапазоне 7–365 дней, санкционированное удаление завершается в течение 24 h и оставляет audit-запись без удалённого содержимого
  traces: [FR-03, FR-06, FR-07]

## Constraints

- **CON-01** До выполнения и приёмки всего набора FR-01–FR-08 функция считается экспериментальной; частичный срез нельзя обозначать как безопасное продолжение прогона.

- **CON-02** Реализация должна встраиваться в существующий контур spec-runner: SQLite с WAL, tasks.md, Git-состояние workstream-а, текущие subprocess-вызовы провайдеров, claims, TDD namespace, waivers и бюджетные санкции.

- **CON-03** Нельзя менять контракт платного провайдера или рассчитывать на его идемпотентность. Если подтверждение результата вызова потеряно, такой вызов остаётся open/unknown и требует решения человека.

- **CON-04** Checkpoint, evidence, prompts, provider output, SQLite и WIP нельзя автоматически помещать в продуктовый Git. Для них допустимо только приватное, явно авторизованное хранилище с контролем доступа, редактированием секретов и retention-политикой.

- **CON-05** Первая версия не должна требовать отдельного always-on control-plane или дежурной команды. Публикация, восстановление и аудит должны выполняться существующим оператором через конечные команды с однозначным результатом.

- **CON-06** Формат checkpoint и evidence обязан быть версионированным и переносимым между абсолютными путями и машинами; локальные пути, временные worktree, lock-файлы и process identity не могут быть частью переносимого контракта.

- **CON-07** Существующие незавершённые прогоны без нового evidence-контракта нельзя автоматически объявлять восстановимыми. Для них допустим только явный legacy-режим или fail-closed с объяснением недостающих гарантий.

- **CON-08** Любые новые расходы на внешний artifact store требуют отдельного одобрения владельца. Стоимость хранения и передачи должна быть атрибутируема по run_id и доступна для аудита вместе со стоимостью платных вызовов.


## Success Metrics

- **M-01** В матрице не менее чем из 20 ежемесячных restore-drill после полного удаления исходного каталога 100% прогонов, остановленных на закрытой durable-границе, восстанавливаются в другом пути и продолжаются с точного следующего шага; потерянных подтверждённых mutations и повторных платных вызовов — 0.

  traces: [G-01, G-03]
- **M-02** В 100% restore-drill с намеренно оставленным open call восстановление выдаёт unknown/needs-human до любого повтора вызова; автоматических или скрытых повторов — 0.

  traces: [G-01, G-03]
- **M-03** Для 100% платных вызовов в новых прогонах по run_id находится ровно один durable call-start и однозначный исход: closure либо явно открытое unknown-состояние; вызовов без классификации — 0.

  traces: [G-02]
- **M-04** Для 100% штатно завершившихся, failed, blocked, refused и early-stop прогонов опубликована неизменяемая closure; отсутствие closure всегда обнаруживается и классифицируется как crash/unknown.

  traces: [G-02]
- **M-05** Не менее трёх реальных активных прогонов переданы в другой checkout или на другую машину и продолжены без ручного восстановления claims, checkpoints, verify-evidence, waivers, budget authority или Git/WIP.

  traces: [G-01, G-03]
- **M-06** В ежемесячной независимой выборке не менее десяти прогонов аудитор без доступа к исходному рабочему каталогу в 100% случаев определяет результат, стоимость, использованные санкции и следующий безопасный шаг либо причину, почему он не доказуем.

  traces: [G-02, G-03]
- **M-07** За три месяца зафиксировано 0 случаев публикации checkpoint или evidence в продуктовый Git, 0 известных утечек секретов и 0 восстановлений, продолжившихся после несовпадения identity, digest или authority.

  traces: [G-01, G-02, G-03]

## Out of Scope

- **OUT-01** Не строить новый scheduler, распределённый оркестратор, always-on control-plane, очередь работников или механизм автоматического failover между машинами.

- **OUT-02** Не обещать exactly-once исполнение со стороны платного провайдера, не отменять и не дедуплицировать уже отправленные ему запросы. Spec-runner фиксирует durable call-start и останавливает open call как unknown; решение о повторе принимает человек.

- **OUT-03** Не реализовывать собственное объектное хранилище, IAM, шифрование ключей или retention-service. Эти функции предоставляет выбранная инфраструктура; spec-runner отвечает за корректную интеграцию, manifest, hashes, доступ и удаление по её публичному контракту.

- **OUT-04** Не заменять Git и не становиться универсальным backup-инструментом репозитория или машины. Git продолжает хранить продуктовую историю, а checkpoint захватывает только материал и состояние, необходимые для продолжения конкретного run_id.

- **OUT-05** Не сохранять полные образы ОС, virtualenv, tool caches, временные worktrees, locks, процессы или произвольные файлы вне границы прогона. Версии и identity зависимостей можно зафиксировать для проверки, но их установка остаётся обязанностью существующего bootstrap/tooling.

- **OUT-06** Не придумывать заново правила claims, TDD namespace, waiver, remedy, budget approval или review. Их определяют существующие контракты spec-runner и governance; checkpoint только сохраняет и при restore пересверяет их эффективное состояние.

- **OUT-07** Не выполнять автоматическую миграцию старых прогонов без нового write-ahead и evidence-контракта и не объявлять их восстановимыми задним числом. Для legacy допустимы диагностика и ручной путь, но не выдуманная гарантия.

- **OUT-08** Не строить в этом workstream межпроектные dashboards, тренды стоимости, capacity planning, billing или аналитику портфеля. В scope остаётся адресуемое по одному run_id чтение состояния, evidence и стоимости, предусмотренное FR-09.

- **OUT-09** Не угадывать и не чинить автоматически повреждённый checkpoint, отсутствующий evidence, несовпадающую identity или неизвестный следующий шаг. Корректный результат таких случаев — stopped/needs-human с точной причиной.

- **OUT-10** Не гарантировать бессрочное хранение и не обходить утверждённые правила удаления, доступа или legal hold. Жизненный цикл артефактов ограничен принятой retention-политикой и её аудитируемыми процедурами.
