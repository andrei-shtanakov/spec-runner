# Tasks — Contract layer: pydantic-модели, кросс-артефактная валидация, ports

Волна: `ws/w-contracts`. Scope: `src/disputatio/contracts/**`, `tests/contracts/**`, `spec/.tdd-evidence/*/ws-w-contracts/**`.
Каждая leaf-задача — TDD-цикл (red → green → refactor) с evidence в `spec/.tdd-evidence/*/ws-w-contracts/**`.
Приёмка волны: `uv run pytest -q tests/contracts/` — зелёный; `uv run ruff format .`, `uv run ruff check .`, `pyrefly check` — ноль ошибок.

## Легенда

| Приоритет | | Статус | |
|---|---|---|---|
| 🔴 P0 | критично, блокирует волну | ⬜ TODO | не начата |
| 🟠 P1 | нужно для полноты контракта | 🔄 IN PROGRESS | в работе |
| 🟡 P2 | улучшение | ✅ DONE | завершена |
| 🟢 P3 | nice to have | ⏸️ BLOCKED | заблокирована |

---

### TASK-001: Зависимости и каркас пакета contracts
- 🔴 P0 | 🔄 IN_PROGRESS | Est: 1h

**Description:**
Добавить зависимости слоя контрактов и создать скелет пакета и тестов.
Изменения `pyproject.toml`/`uv.lock` — только в этой задаче (harness-guard
снапшотит их per-attempt; последующие задачи их не трогают).

**Checklist:**
- [ ] `uv add pydantic pyyaml` (pydantic уже в baseline D0 — проверить, не дублировать)
- [ ] При требовании pyrefly: `uv add --dev types-PyYAML`
- [ ] Создать `src/disputatio/contracts/__init__.py` (пустой публичный контракт)
- [ ] Создать `tests/contracts/__init__.py`-заглушку не требуется — проверить discovery pytest
- [ ] Smoke-тест импорта `disputatio.contracts` проходит `uv run pytest -q tests/contracts/`
- [ ] Гейты: `ruff format`, `ruff check`, `pyrefly check` — зелёные

**Traces to:** [REQ-017], [DESIGN-001], [DESIGN-012]
**Depends on:** —
**Blocks:** [TASK-002]

---

### TASK-002: base.py — SCHEMA_V1, ArtifactModel, NestedModel
🔴 P0 | ⬜ TODO | Est: 2h

**Description:**
Базовый класс всех артефактов: поле `schema_` с `alias="schema"`,
`Literal["disputatio/v1"]`, `extra="forbid"`, `frozen=True`,
`populate_by_name=True`; общий `NestedModel` для вложенных моделей без поля
`schema`. Фундамент версионирования и round-trip всех артефактов.

**Checklist:**
- [ ] RED: `tests/contracts/test_base.py` — default `"disputatio/v1"`; `schema: "disputatio/v2"` → ValidationError; лишнее поле → ValidationError; frozen (присваивание → ошибка); round-trip через `model_dump_json(by_alias=True)` с ключом `"schema"` в JSON
- [ ] GREEN: `base.py` с `SCHEMA_V1: Final`, `ArtifactModel`, `NestedModel`
- [ ] REFACTOR: докстринги, evidence в `spec/.tdd-evidence/*/ws-w-contracts/`
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-001], [REQ-002], [DESIGN-002]
**Depends on:** [TASK-001]
**Blocks:** [TASK-003], [TASK-004], [TASK-005], [TASK-006], [TASK-007], [TASK-008]

---

### TASK-003: session.py — SessionState и вложенные модели
🔴 P0 | ⬜ TODO | Est: 3h

**Description:**
`SessionPhase` (12 состояний §2, верхний регистр), `TaskMode`, вложенные
`TaskSpec`, `AgentRef`, `AgentsSpec`, `Limits`, `BudgetUsed` (на `NestedModel`,
без `schema`), корневой `SessionState(ArtifactModel)`.

**Checklist:**
- [ ] RED: `test_session.py` — позитив (пример §4.1 как fixture); все 12 значений enum; `current_round=0` валиден, `-1` — нет; `Limits` `ge=1` (`schema_retries` `ge=0`); `BudgetUsed` `ge=0` с дефолтами; UUID/datetime парсятся из строк JSON; round-trip; лишние поля во вложенных моделях → ValidationError
- [ ] GREEN: `session.py`
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-003], [DESIGN-003]
**Depends on:** [TASK-002]
**Blocks:** [TASK-009], [TASK-010]

---

### TASK-004: proposal.py — ProposalMeta и parse_proposal()
🟠 P1 | ⬜ TODO | Est: 3h

**Description:**
Модель фронтматтера (`round ge=1`, `role: Literal["author"]`,
`responds_to: str | None`, `files_touched`, `self_declared_status`) и чистая
функция `parse_proposal(text) -> (ProposalMeta, str)` с тремя различимыми
классами ошибок; тело — байт-в-байт.

**Checklist:**
- [ ] RED: `test_proposal.py` — позитив (§4.2 fixture); `round=0` → ошибка; `role: "reviewer"` → ошибка; отсутствие `---` в начале → `MissingFrontmatterError`; битый YAML и не-dict YAML → `FrontmatterYamlError`; невалидная схема → pydantic `ValidationError`; тело с CRLF, trailing whitespace и `---` внутри тела возвращается неизменным; round-trip модели
- [ ] GREEN: `proposal.py` — `ProposalParseError`, наследники, `parse_proposal` через `yaml.safe_load`
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-004], [REQ-005], [DESIGN-004]
**Depends on:** [TASK-002]
**Blocks:** [TASK-011]

---

### TASK-005: verification.py — GateResult, DiffStats, VerificationReport
🔴 P0 | ⬜ TODO | Est: 2h

**Description:**
`GateStatus` (`pass_ = "pass"`, fail, skip), `OverallStatus`, `GateResult`
(`exit_code: int | None`, `duration_s ge=0`, опциональный `reason`),
`DiffStats` (все `ge=0`), `VerificationReport` (пустой `gates` валиден —
режим analyze). Модель не пересчитывает `overall`.

**Checklist:**
- [ ] RED: `test_verification.py` — позитив (§4.3 fixture); пустой `gates` валиден; `status: "skip"` с `exit_code=None` и `reason`; сериализация enum в голые строки `"pass"`/`"fail"`; отрицательный `duration_s` → ошибка; `overall=fail` при всех gates pass парсится (инвариант производителя); round-trip; невалидная схема/лишние поля → ошибка
- [ ] GREEN: `verification.py`
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-006], [DESIGN-005]
**Depends on:** [TASK-002]
**Blocks:** [TASK-009], [TASK-010]

---

### TASK-006: review.py — Severity, Verdict, Issue, Review
🔴 P0 | ⬜ TODO | Est: 2h

**Description:**
Модель «сырого» ревью: форма и диапазоны без нормативных правил §4.4 —
blocker без evidence, пустой `checked`, отрицательный вердикт без issues
парсятся успешно (иначе деградация REQ-011 невозможна).

**Checklist:**
- [ ] RED: `test_review.py` — позитив (§4.4 fixture); `confidence` 0.0/1.0 валидны, −0.1/1.1 — нет; `line_hint=None` и `line_hint=0` → ошибка (`ge=1`); «сырое» ревью (blocker с `evidence=""`, `checked=[]`, `reject` без issues) парсится; `role: "author"` → ошибка; round-trip; невалидная схема/лишние поля → ошибка
- [ ] GREEN: `review.py`
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-007], [DESIGN-006]
**Depends on:** [TASK-002]
**Blocks:** [TASK-009]

---

### TASK-007: decision.py — Outcome, Decision
🟠 P1 | ⬜ TODO | Est: 1.5h

**Description:**
`Outcome` (`continue_ = "continue"` — приём для ключевого слова), `Decision`
с единственным model_validator'ом слоя: терминальный outcome ⇒
`next_round_directive is None`.

**Checklist:**
- [ ] RED: `test_decision.py` — позитив (§4.5 fixture); `outcome="continue"` c директивой валиден; `outcome="converged"` с непустой директивой → ValidationError; каждое из 5 значений enum сериализуется в строку; round-trip; невалидная схема/лишние поля → ошибка
- [ ] GREEN: `decision.py`
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-008], [DESIGN-007]
**Depends on:** [TASK-002]
**Blocks:** [TASK-011]

---

### TASK-008: event.py — EventSource, EventType, Event
🟠 P1 | ⬜ TODO | Est: 1.5h

**Description:**
Схема события `events.jsonl`: `ts: datetime`, `session`, `round ge=0`
(0 — вне раунда), `source` (4 значения), `type` (7 значений §8),
непрозрачный `payload: dict[str, Any]`.

**Checklist:**
- [ ] RED: `test_event.py` — позитив (§8 fixture); `round=0` валиден; произвольный вложенный payload проходит и переживает round-trip; неизвестный `type` → ошибка; лишнее поле на верхнем уровне → ошибка, лишние ключи внутри `payload` — нет; round-trip
- [ ] GREEN: `event.py`
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-009], [DESIGN-008]
**Depends on:** [TASK-002]
**Blocks:** [TASK-009], [TASK-010]

---

### TASK-009: review_rules.py — чистые функции кросс-валидации §4.4
🔴 P0 | ⬜ TODO | Est: 4h

**Description:**
Анти-галлюцинационное ядро: `ReviewErrorCode`, `ReviewValidationError`,
`ReviewValidationResult` (`accepted`, `retryable`),
`degrade_unevidenced_issues`, `validate_review`,
`validate_review_against_verification`. Результат — значение, не исключение;
порядок нормативный: checked → деградация → вердикт; round mismatch —
отдельная ошибка.

**Checklist:**
- [ ] RED: `test_review_rules.py` — все четыре правила §4.4: `request_changes`/`reject` без blocker|major → `NEGATIVE_VERDICT_WITHOUT_MAJOR`; blocker/major с пустым и пробельным `evidence` деградируется до minor, id в `degraded_issue_ids`; approve при `overall==fail` → `APPROVE_WITH_FAILED_VERIFICATION`; `checked=[]` → `EMPTY_CHECKED` с `retryable=True`
- [ ] RED: композиции — деградация единственного blocker → отрицательный вердикт остаётся без major → отклонение (порядок нормативен); `review.round != verification.round` → `ROUND_MISMATCH`; несколько ошибок аккумулируются в одном результате; `review` в результате заполнен только при отсутствии ошибок
- [ ] RED: чистота — исходный Review не изменён после деградации (frozen + поэлементное сравнение)
- [ ] GREEN: `review_rules.py` (`model_copy(update=...)`, frozen dataclass'ы результата)
- [ ] REFACTOR: докстринги, evidence
- [ ] Гейты ruff/pyrefly — зелёные

**Traces to:** [REQ-010], [REQ-011], [REQ-012], [REQ-013], [DESIGN-009]
**Depends on:** [TASK-005], [TASK-006]
**Blocks:** [TASK-011]

---

### TASK-010: ports.py — Protocol-интерфейсы и фейки
🔴 P0 | ⬜ TODO | Est: 3h

**Description:**
`@runtime_checkable` Protocol'ы `StateStore`, `EventSink`, `AgentAdapter`,
`Verifier` + `AgentStepResult(NestedModel)`; синхронные сигнатуры, нуль
реализаций в пакете (INV-11, композиция — только w-runtime). Фейки в
`tests/contracts/fakes.py` — исполняемая фиксация контракта.

**Checklist:**
- [ ] RED: `test_ports.py` — `isinstance(fake, Port)` для каждого из 4 фейков; объект без нужного метода не проходит isinstance; `AgentStepResult` — форма и round-trip (без поля `schema`)
- [ ] GREEN: `ports.py`; `fakes.py` — `InMemoryStateStore` (dict), `ListEventSink` (append), `ScriptedAgentAdapter`, `StubVerifier` — все без I/O
- [ ] Проверить: в `src/disputatio/contracts/` нет ни одной реализации портов (grep-проверка в тесте или ревью)
- [ ] REFACTOR: докстринги (включая write-ahead семантику в `StateStore.save`), evidence
- [ ] Гейты ruff/pyrefly — зелёные (pyrefly типизирует фейки против Protocol'ов)

**Traces to:** [REQ-014], [REQ-015], [DESIGN-010]
**Depends on:** [TASK-003], [TASK-005], [TASK-008]
**Blocks:** [TASK-011]

---

### TASK-011: Публичный API `__init__.py` и приёмка волны
🔴 P0 | ⬜ TODO | Est: 2h

**Description:**
Реэкспорт всех моделей, enum'ов, функций, ошибок и портов через
`disputatio.contracts` с `__all__`; финальный прогон приёмки волны.
Потребители импортируют только из пакета, не из подмодулей.

**Checklist:**
- [ ] RED: тест публичного контракта — каждый элемент `__all__` импортируем из `disputatio.contracts`; ключевые имена (все модели, `parse_proposal`, `validate_review_against_verification`, 4 порта) присутствуют
- [ ] GREEN: `__init__.py` с `__all__`
- [ ] Полный прогон: `uv run pytest -q tests/contracts/` — зелёный
- [ ] `uv run ruff format .` → `pyrefly check` → `uv run ruff check .` — ноль ошибок
- [ ] Матрица покрытия DESIGN-011 закрыта: на каждый артефакт есть позитив, негатив (schema v2, лишние поля, неверный enum), round-trip, граничные
- [ ] Evidence всех TDD-циклов лежит в `spec/.tdd-evidence/*/ws-w-contracts/**`

**Traces to:** [REQ-016], [REQ-017], [DESIGN-001], [DESIGN-011], [DESIGN-012]
**Depends on:** [TASK-004], [TASK-007], [TASK-009], [TASK-010]
**Blocks:** —

---

## Граф зависимостей

```
TASK-001 (deps + каркас)
    └─► TASK-002 (base.py)
            ├─► TASK-003 (session) ──────────────┐
            ├─► TASK-004 (proposal) ─────────────┼───────────┐
            ├─► TASK-005 (verification) ──┬──────┤           │
            ├─► TASK-006 (review) ────────┤      │           │
            ├─► TASK-007 (decision) ──────┼──────┼───────────┤
            └─► TASK-008 (event) ─────────┼──────┤           │
                                          ▼      ▼           ▼
                              TASK-009 (rules)  TASK-010   TASK-011
                                          │     (ports)    (API+приёмка)
                                          └──────┴────────────▲
```

Критический путь: TASK-001 → TASK-002 → TASK-005/006 → TASK-009 → TASK-011.
TASK-003…008 независимы между собой и могут выполняться параллельно после TASK-002.

## Сводка

| Milestone | Задачи | Оценка | Выход |
|---|---|---|---|
| M1: Фундамент | TASK-001, TASK-002 | ~3h | ArtifactModel, версионирование, гейты настроены |
| M2: Модели артефактов | TASK-003…TASK-008 | ~13h | Все 6 артефактов §4.1–4.5 + §8, round-trip |
| M3: Правила и ports | TASK-009, TASK-010 | ~7h | Четыре правила §4.4, 4 Protocol'а + фейки |
| M4: Приёмка | TASK-011 | ~2h | `uv run pytest -q tests/contracts/` зелёный, публичный API зафиксирован |

Итого: 11 задач, ~25h. P0 — 8, P1 — 3.
