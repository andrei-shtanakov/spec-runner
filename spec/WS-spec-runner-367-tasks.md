---
traces_to:
- behaviour-spec
upstream_hashes:
  behaviour-spec: 8e8ddd2118718dc72e0778e2c7b5c5875d98d979
spec_stage: tasks
status: approved
version: 2
generated_by: fleet-agent
generated_at: '2026-09-06T03:26:49'
source_prompt_version: ''
validation: pass
approved_by: andrei-shtanakov
approved_at: '2026-09-06T00:57:00Z'
owner_role: stream-owner
---

## Milestone 1: verify-first: режим исполнения задачи «сначала живой прогон checked_by-группы» с durable evidence (SHA, селектор, config hash, исход green|test-failure|instrument-error) и ветвлением green→green-only / test-failure→TDD / instrument→стоп (spec-runner#367)

Сгенерировано task_bridge из behaviour-spec бандла WS-spec-runner-367 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Draft: исполнение только после человеческого approve.

### Решения открытых вопросов (уровень design, зафиксированы до исполнения)

Бандл WS-367 — 3-узловой (без стадии design); чтобы задачи не решали
архитектурные вопросы каждая по-своему, ответы фиксируются здесь — в границах
продуктовых рамок requirements (правка решений = правка этой спеки, не
молчаливый выбор исполнителя). Прецедент — WS-341.

- **Q-04 — где выполняется живой прогон:** в одноразовом detached-worktree от
  коммита-кандидата, как `verify_red`. Единственная форма, при которой вердикт
  относится к НАЗВАННОМУ коммиту и воспроизводим из него (FR-07), а прогон —
  первое действие после стадии `branch` (FR-05) без влияния грязного рабочего
  дерева.
- **Q-07 — повторная попытка verify-first задачи:** evidence переиспользуется
  по правилам, аналогичным `_reusable_checkpoint`: ось дерева —
  ПРОИСХОЖДЕНИЕ, не равенство (кандидат обязан происходить от коммита
  evidence тем же правилом `_descends_from`, что и red-гейт; недоказуемая
  ancestry — `instrument-error` классом `AncestryUnknown`, FR-11/BEH-18a);
  оси policy-config hash и состава объявленной группы — точное совпадение
  (FR-10: тот же состав; FR-11: наследование через изменившийся вопрос
  недопустимо). Оси привязаны к ПОТРЕБИТЕЛЯМ (major круга 6 — без этого
  два несовместимых прочтения): (а) ПРЕД-ПРОГОННОЕ решение в начале
  новой попытки «переиспользовать evidence или прогнать заново» судится
  всеми осями, включая четвёртую — совпадение git tree-hash кандидата с
  tree-hash коммита evidence (зелёное стареет в опасную сторону: код под
  тестом мог измениться); расхождение по любой оси — свежий живой
  прогон, не деградация до частичного переиспользования; (б) ПРИЁМ уже
  записанной evidence red-гейтом на пред-мержевом моменте судится ТОЛЬКО
  осью происхождения (FR-11, `_descends_from`) + config hash + состав —
  БЕЗ tree-hash: green-пас обязан писать пины отдельным файлом (FR-19),
  дерево кандидата закономерно отличается, и требование равенства дерева
  делало бы гейт неудовлетворимым ровно в момент, когда гейтуемая работа
  произошла (комментарий red-гейта «Descent, not equality»).
- **Q-09 — где живёт отображение `(RunOutcome, SelectionProof,
  ExecutionProof)` → исход:** одной таблицей-константой в модуле verify-first
  (единственный источник, три места соглашения не заводятся); тест FR-16
  перебирает ДЕКАРТОВО ПРОИЗВЕДЕНИЕ всех значений трёх enum-осей (не ключи
  таблицы — домен теста не задаётся проверяемым артефактом, BEH-23) и
  требует, чтобы каждая комбинация была отнесена таблицей ровно к одному из
  трёх исходов; новое значение любой оси без строки в таблице — красный
  тест, не молчаливый четвёртый исход. Таблица классифицирует
  ПОСТ-прогонную тройку; пред-прогонная ветка `instrument-error` — РОВНО
  шесть случаев: отсутствующая evidence, недостижимое окружение,
  недостижимый SHA, композитная test_command, необъявленная или
  неразбираемая группа (BEH-22 — вход виден до прогона, minor круга 8),
  неразрешимый адаптер-судья
  (FR-15/BEH-10 — пред-прогонный вход, «недостижимое окружение» его не
  покрывает: environment_id и имя адаптера — разные оси, BEH-15; minor
  круга 7) (minor круга 6: НЕ «весь
  перечень BEH-22» — пустой и недоказанный выбор наблюдаются только
  ПОСЛЕ прогона и классифицируются таблицей по осям
  RunOutcome/SelectionProof); пред-прогонные случаи покрываются
  отдельными кейсами теста BEH-23.
- **Потолок времени прогона (BEH-14, NFR-02):** на селектор —
  `REPLAY_TIMEOUT_SECONDS` (сегодня 900 с, прецедент named-константы);
  потолок ГРУППЫ — НЕЗАВИСИМАЯ named-константа
  `VERIFY_GROUP_TIMEOUT_SECONDS = 1800` с бюджетной семантикой: таймаут
  каждого следующего селектора = `min(REPLAY_TIMEOUT_SECONDS, остаток
  бюджета группы)`; исчерпание бюджета — `instrument-error`. Сумма
  по-селекторных потолков потолком группы НЕ является (иначе групповая
  ветвь BEH-14 недостижима по построению — первым всегда срабатывал бы
  по-селекторный таймаут, minor круга 5). Оба значения потребляются
  импортом, не числом, зашитым в тест; групповой бюджет логируется ДО
  первого прогона.
- **Порядок исполнения (major кругов 5–6):** ДО задачи, доводящей
  green-only до DONE (BEH-20, TASK-008), обязана идти ТОЛЬКО регистрация
  гейтов и аудит чтений режима (BEH-24) — red-гейт обязан быть спрошен и
  удовлетворён ссылкой на evidence, что невозможно до регистрации; иначе
  окно с тривиальным SATISFIED (mode != tdd) — трактовка «третий режим =
  гарантий нет», запрещённая FR-17/Q-01. При этом BEH-25/BEH-28/BEH-29
  (Given — УЖЕ пройденный green-only путь) обязаны идти ПОСЛЕ TASK-008 —
  перестановка TASK-009 целиком сделала бы её недоводимой её же объёмом.
  Поэтому BEH-24 выделен в отдельную TASK-015; цепочка:
  …TASK-007 → TASK-015 → TASK-008 → TASK-009 → TASK-010….
  Остаточное окно ОБЪЯВЛЯЕТСЯ (minor круга 7): в интервале
  TASK-008…TASK-010 запись claim'ов на файлы группы ещё не ведётся и
  гейт claims отвечает зелёным на пустом множестве; окно наблюдаемо
  только на промежуточных коммитах интеграционной ветки (PR мержится
  целиком, ни один прогон конвейера не исполняется с ветки до мержа) и
  закрывается TASK-010 до конца workstream'а — принято осознанно, не
  молча.
- **Q-08 (файловые цели в словаре селекторов) — НЕ решается здесь:** остаётся
  за границами workstream'а (behaviour-spec «Границы спецификации»), задачи
  его не трогают.

### TASK-001: `**Mode:** verify_first` резолвится пер-задачно при любом дефолте проекта
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-01

**Checklist:**
- [x] реализовать BEH-01: `**Mode:** verify_first` резолвится пер-задачно при любом дефолте проекта
- [x] проверка группы: tests/test_verify_first_mode.py (kind: contract) зелёные на BEH-01

**Traces to:** [FR-01]

### TASK-002: Объявленная группа доходит до исполнения в объявленном порядке и без интерпретации (+1 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-02, BEH-03.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-02 (—BEH-03)
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-02: Объявленная группа доходит до исполнения в объявленном порядке и без интерпретации
- [x] реализовать BEH-03: Группа не выводится ни из чего
- [x] проверка группы: tests/test_verify_first_declaration.py (kind: contract), tests/test_verify_first_declaration.py (kind: integration) зелёные на BEH-02, BEH-03

**Traces to:** [FR-02], [FR-03]

### TASK-003: Неверное объявление отказывает до исполнения — в `validate` и на старте (+1 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-04, BEH-05.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-04 (—BEH-05)
**Depends on:** [TASK-002]

**Checklist:**
- [x] реализовать BEH-04: Неверное объявление отказывает до исполнения — в `validate` и на старте
- [x] реализовать BEH-05: Отказ на селекторе воспроизводит формулировку адаптера и называет ожидаемую форму
- [x] проверка группы: tests/test_verify_first_validate.py (kind: integration) зелёные на BEH-04, BEH-05

**Traces to:** [FR-03], [FR-24]

### TASK-004: Задача, не объявившая verify-first, ведёт себя в точности как сегодня
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-06.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-06
**Depends on:** [TASK-003]

**Checklist:**
- [x] реализовать BEH-06: Задача, не объявившая verify-first, ведёт себя в точности как сегодня
- [x] проверка группы: tests/test_execution_mode.py (kind: contract) зелёные на BEH-06

**Traces to:** [FR-04]

### TASK-005: Прогон — первое действие задачи, до любого платного вызова (+2 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-07, BEH-08, BEH-09.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-07 (—BEH-09)
**Depends on:** [TASK-004]

**Checklist:**
- [x] реализовать BEH-07: Прогон — первое действие задачи, до любого платного вызова
- [x] реализовать BEH-08: Прогон ограничен объявленной группой
- [x] реализовать BEH-09: Вердикт относится к названному коммиту, а не к рабочему дереву
- [x] проверка группы: tests/test_verify_run_order.py (kind: integration) зелёные на BEH-07, BEH-08, BEH-09

**Traces to:** [FR-05], [FR-06], [FR-07]

### TASK-006: Неразрешимый адаптер и композитная `test_command` — отказ, а не догадка (+5 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-10, BEH-11, BEH-12, BEH-13, BEH-14, BEH-23.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-10 (—BEH-23)
**Depends on:** [TASK-005]

**Checklist:**
- [x] реализовать BEH-10: Неразрешимый адаптер и композитная `test_command` — отказ, а не догадка
- [x] реализовать BEH-11: `green` требует трёх фактов по каждому объявленному селектору
- [x] реализовать BEH-12: Прошедший прогон с недоказанным выбором зелёным не является
- [x] реализовать BEH-13: Пустой и пропущенный выбор — не здоровье
- [x] реализовать BEH-14: Потолок времени прогона объявлен и превышение — отказ, а не зависание
- [x] реализовать BEH-23: Три исхода и ни одного молчаливого четвёртого
- [x] проверка группы: tests/test_verify_outcomes.py (kind: integration), tests/test_verify_outcomes.py (kind: contract) зелёные на BEH-10, BEH-11, BEH-12, BEH-13, BEH-14, BEH-23

**Traces to:** [FR-07], [FR-15], [FR-08], [FR-09], [FR-06], [FR-16]

### TASK-007: Evidence durable и несёт полный объявленный состав (+5 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-15, BEH-16, BEH-17, BEH-18, BEH-18a, BEH-19.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-15 (—BEH-19)
**Depends on:** [TASK-006]

**Checklist:**
- [x] реализовать BEH-15: Evidence durable и несёт полный объявленный состав
- [x] реализовать BEH-16: По evidence прогон воспроизводится и сравнивается третьей стороной
- [x] реализовать BEH-17: Смена значения из `POLICY_KEYS` обесценивает прежнюю evidence
- [x] реализовать BEH-18: Смена объявленной группы обесценивает прежнюю evidence
- [x] реализовать BEH-18a: Evidence чужого дерева не наследуется
- [x] реализовать BEH-19: Green-only не притворяется красным
- [x] проверка группы: tests/test_verify_evidence.py (kind: contract), tests/test_verify_evidence.py (kind: integration) зелёные на BEH-15, BEH-16, BEH-17, BEH-18, BEH-18a, BEH-19

**Traces to:** [FR-10], [FR-11], [FR-12], [FR-21]

### TASK-008: `green` доводит задачу до DONE без покупки красного (+2 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-20, BEH-21, BEH-22.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-20 (—BEH-22)
**Depends on:** [TASK-015]

**Checklist:**
- [x] реализовать BEH-20: `green` доводит задачу до DONE без покупки красного
- [x] реализовать BEH-21: `test-failure` отправляет задачу в неизменённый TDD-цикл
- [x] реализовать BEH-22: `instrument-error` останавливает задачу fail-closed
- [x] проверка группы: tests/test_verify_branching.py (kind: e2e), tests/test_verify_branching.py (kind: integration) зелёные на BEH-20, BEH-21, BEH-22

**Traces to:** [FR-13], [FR-05], [FR-14], [FR-15]

### TASK-009: Пред-терминальная оценка, waiver и lifecycle (+2 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-25, BEH-28, BEH-29 (Given всех трёх — уже
пройденный green-only путь, поэтому строго ПОСЛЕ TASK-008; BEH-24
выделен в TASK-015 — major круга 6).
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-25 (—BEH-29)
**Depends on:** [TASK-008]

**Checklist:**
- [x] реализовать BEH-25: Пред-терминальная оценка выполняется ровно как для `tdd`
- [x] реализовать BEH-28: Waiver остаётся отдельным инструментом авторитета
- [x] реализовать BEH-29: Lifecycle-переходы не ослабляются — `lifecycle.has_verify_evidence` даёт green-only задаче отдельное основание для перехода в GREEN (не подделку красного); `execution.py` теперь пишет свои lifecycle-переходы (`GREEN_IMPLEMENTING`, `DONE`) для `verify_first` так же, как для `tdd`
- [x] проверка группы: tests/test_verify_gates.py (kind: integration), tests/test_verify_gates.py (kind: contract) зелёные на BEH-25, BEH-28, BEH-29

**Traces to:** [FR-18], [FR-20], [FR-21]

### TASK-010: Файлы объявленной группы заморожены на время задачи (+1 смежных BEH)
P2 | 🔄 IN_PROGRESS   Est: 0.5d

Реализовать сценарии BEH-26, BEH-27.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-26 (—BEH-27)
**Depends on:** [TASK-009]

**Checklist:**
- [ ] реализовать BEH-26: Файлы объявленной группы заморожены на время задачи
- [ ] реализовать BEH-27: Заморозка снимается на DONE и не облагает соседей
- [ ] проверка группы: tests/test_verify_claims.py (kind: integration) зелёные на BEH-26, BEH-27

**Traces to:** [FR-19]

### TASK-011: Живой прогон — собственная стадия (+1 смежных BEH)
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-30, BEH-31.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-30 (—BEH-31)
**Depends on:** [TASK-010]

**Checklist:**
- [ ] реализовать BEH-30: Живой прогон — собственная стадия
- [ ] реализовать BEH-31: Evidence и путь задачи предъявляются через CLI
- [ ] проверка группы: tests/test_verify_cli.py (kind: integration) зелёные на BEH-30, BEH-31

**Traces to:** [FR-22], [FR-23], [FR-12]

### TASK-012: Внешние контракты меняются только аддитивно
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-32.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-32
**Depends on:** [TASK-011]

**Checklist:**
- [ ] реализовать BEH-32: Внешние контракты меняются только аддитивно
- [ ] проверка группы: tests/test_json_result_contract.py (kind: contract) зелёные на BEH-32

**Traces to:** [FR-04]

### TASK-013: Документация и CHANGELOG объявляют режим и его границы
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-33.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-33
**Depends on:** [TASK-012]

**Checklist:**
- [ ] реализовать BEH-33: Документация и CHANGELOG объявляют режим и его границы
- [ ] проверка группы: docs/architecture.md (kind: manual) зелёные на BEH-33

**Traces to:** [FR-11], [FR-01]

### TASK-014: Стоимость класса измерена, и ни один тест не вызывает реального агента
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-34.
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-34
**Depends on:** [TASK-013]

**Checklist:**
- [ ] реализовать BEH-34: Стоимость класса измерена, и ни один тест не вызывает реального агента
- [ ] проверка группы: tests/test_verify_first_cost.py (kind: e2e) зелёные на BEH-34

**Traces to:** [FR-13], [FR-05]

### TASK-015: Регистрация гейтов и аудит чтений режима (BEH-24, до green-only)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарий BEH-24 (регистрация tdd.red/tdd.claims для
verify-first в обеих конфигурациях + аудит каждого сайта чтения И ЗАПИСИ
режима — Q-01/FR-17: третье значение не читается как «гарантий нет», а
записи литералом не хоронят переиспользование). Выделен из TASK-009 и
поставлен ПЕРЕД TASK-008: red-гейт обязан спрашиваться по evidence, что
невозможно до регистрации (major кругов 5–6). Записи — major круга 7:
`RedCheckpoint(..., execution_mode="tdd")` литералом в `_judge_red_commit` (tdd.py:841,
вызывается из run_red_phase) делал бы `_reusable_checkpoint` слепым для verify-first
(cp.execution_mode != resolve_execution_mode(task)) — каждый ретрай
ветви test-failure заново покупал бы платный RED-авторинг (AC FR-14).
Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-24
**Depends on:** [TASK-007]

**Checklist:**
- [x] реализовать BEH-24: Третий режим не читается как «гарантий нет»
- [x] записи режима — фактическая величина, не литерал: `RedCheckpoint(..., execution_mode=resolve_execution_mode(task))` в `_judge_red_commit` (tdd.py:841, вызывается из run_red_phase — minor круга 8: сайт назван точно); тест краснеет на литерале — на ретрае verify-first задачи чекпойнт переиспользуется, второй RED-авторинг не покупается (AC FR-14)
- [x] проверка группы: tests/test_verify_gates.py (kind: contract) зелёные на BEH-24

**Traces to:** [FR-17], [FR-14]
