---
spec_stage: requirements
status: approved
version: 2
generated_by: claude@claude-opus-4-8
generated_at: 2026-07-05
source_prompt_version: sha256:pending
validation: pass
approved_by: andrei-shtanakov
approved_at: '2026-07-06T01:08:59Z'
---

# C1: STAGES → загружаемый профиль (spec-runner) — Requirements

> Keystone-изменение маршрута steward (NEXT-STEPS Phase 1). Цель — вынести захардкоженную
> цепочку стадий в данные, **без изменения поведения** по умолчанию. Разблокирует богатые
> профили governance-слоя. Upstream-трасса: steward REQ-001.

## Контекст (реальный код)

- `src/spec_runner/spec.py:17` — `STAGES = ("requirements","design","tasks")`.
- Зависят от него: `downstream_stages` (spec.py:159), `resolve_next_stage` (165),
  `mark_downstream_stale` (~226), валидация `spec_stage` (114).
- `src/spec_runner/prompt.py:20-22` — карта `stage → template`; словари маркеров/промптов (58-84).
- `src/spec_runner/validate.py:124` — `validate_spec_stage` диспатчит по имени стадии (if/elif).
- `src/spec_runner/__init__.py` экспортирует `SPEC_STAGES`.

## Out of Scope

- `stages.py::STAGES` — исполнительные фазы задачи (codex/parse/…), другой механизм.
- Богатые профили (`team`: charter→…→decomposition) и их методология — владение steward (G1);
  здесь только механизм загрузки.
- `gate-check` линтер и git-аппрув энфорс (CODEOWNERS/CI) — steward Phase 2 (G2/G3).
- Делегирование Maestro decomposer → spec-runner authoring (C4) — команда Maestro.

## Requirements

#### REQ-301: Профиль стадий как данные
**Priority**: 🔴 P0
**Description**: Ввести `StageProfile` — упорядоченный список стадий, каждая с
`{name, template, markers, validator, upstream}`. Встроенный профиль `lite` = текущие три стадии.
**Acceptance Criteria**:
- [ ] `StageProfile`/`StageDef` заданы; `lite` воспроизводит текущую цепочку 1:1
- [ ] Профиль грузится из бандла (`importlib.resources`), опц. пользовательский

#### REQ-302: spec.py читает стадии из профиля
**Priority**: 🔴 P0
**Description**: `downstream_stages`, `resolve_next_stage`, `mark_downstream_stale`, проверка
`spec_stage` берут порядок из профиля, а не из модульной константы.
**Acceptance Criteria**:
- [ ] Все четыре места параметризованы профилем (дефолт `lite`)
- [ ] Поведение при `lite` идентично текущему

#### REQ-303: prompt.py — карты стадий из профиля
**Priority**: 🔴 P0
**Description**: `stage→template` и словари маркеров/промптов берутся из `StageDef`, не из
модульных словарей.
**Acceptance Criteria**:
- [ ] Сборка промпта стадии читает `StageDef`
- [ ] Маркеры `SPEC_*_READY/_END` для `lite` не изменились

#### REQ-304: validate.py — диспатч по профилю
**Priority**: 🔴 P0
**Description**: `validate_spec_stage` резолвит валидатор через реестр `{validator_key: callable}`
из профиля вместо if/elif.
**Acceptance Criteria**:
- [ ] Реестр валидаторов; `tasks` использует существующий `task.py`-парсер
- [ ] Вердикты для `lite` не изменились

#### REQ-305: Zero behaviour change (инвариант)
**Priority**: 🔴 P0
**Description**: По умолчанию (`lite`) весь пайплайн ведёт себя как сейчас.
**Acceptance Criteria**:
- [ ] **Все существующие тесты зелёные без правок**
- [ ] Файлы без frontmatter — по-прежнему unmanaged
- [ ] `SPEC_STAGES`-экспорт сохранён (= имена стадий `lite`)

#### REQ-306: Выбор профиля
**Priority**: 🟠 P1
**Description**: Поле конфига `spec_profile` (дефолт `lite`) + флаг `--profile` на `plan --gated`
и семействе `spec`.
**Acceptance Criteria**:
- [ ] `--profile <name>` и `spec_profile` в config
- [ ] Неизвестный профиль → внятная ошибка (не трейсбек)

#### REQ-307: Обратная совместимость SpecMeta
**Priority**: 🔴 P0
**Description**: Существующие спеки со `spec_stage` из `lite` остаются валидны.
**Acceptance Criteria**:
- [ ] `spec_stage in ("requirements","design","tasks")` валиден под `lite`
- [ ] Апгрейд не требует миграции существующих файлов
