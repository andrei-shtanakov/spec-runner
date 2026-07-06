---
spec_stage: tasks
status: approved
version: 2
generated_by: claude@claude-opus-4-8
generated_at: 2026-07-05
source_prompt_version: sha256:pending
validation: pass
approved_by: andrei-shtanakov
approved_at: '2026-07-06T01:09:08Z'
---

# C1: STAGES → профиль — Tasks

> Priority: 🔴 P0 · 🟠 P1 | Status: ⬜ TODO · 🔄 IN PROGRESS · ✅ DONE · ⏸️ BLOCKED
> Инвариант всей работы: **существующие тесты остаются зелёными на каждом шаге** (REQ-305).

---

## Milestone 1: Модель профиля

### TASK-301: StageDef / StageProfile + bundled `lite`
🔴 P0 | ⬜ TODO | Est: 3-4h

**Checklist:**
- [ ] `spec.py`: dataclasses `StageDef`, `StageProfile` (+ `names()`)
- [ ] `profiles/lite.yaml` (bundled) = текущая цепочка
- [ ] Загрузчик профиля из `importlib.resources`
- [ ] `STAGES`/`SPEC_STAGES` выводятся из `lite` (обратная совместимость экспорта)
- [ ] Юнит-тесты: `lite.names() == ("requirements","design","tasks")`

**Traces to:** [REQ-301], [DESIGN-301], [DESIGN-302]
**Depends on:** -
**Blocks:** [TASK-302], [TASK-303], [TASK-304], [TASK-305]

---

## Milestone 2: Прокинуть профиль

### TASK-302: Параметризовать функции spec.py
🔴 P0 | ⬜ TODO | Est: 2-3h

**Checklist:**
- [ ] `downstream_stages`/`resolve_next_stage`/`mark_downstream_stale` берут `stages` (дефолт lite)
- [ ] Проверка `spec_stage` (spec.py:114) — против стадий профиля
- [ ] Существующие тесты spec.py зелёные без правок

**Traces to:** [REQ-302], [DESIGN-303]
**Depends on:** [TASK-301]
**Blocks:** [TASK-306]

---

### TASK-303: prompt.py из StageDef
🔴 P0 | ⬜ TODO | Est: 2-3h

**Checklist:**
- [ ] template/marker_prefix/prompt_text читаются из `StageDef`
- [ ] Маркеры `lite` (`SPEC_REQUIREMENTS_READY` и т.д.) байт-в-байт прежние
- [ ] Golden-тест сгенерированного промпта для `lite`

**Traces to:** [REQ-303], [DESIGN-305]
**Depends on:** [TASK-301]
**Blocks:** [TASK-306]

---

### TASK-304: validate.py — реестр валидаторов
🔴 P0 | ⬜ TODO | Est: 2h

**Checklist:**
- [ ] `VALIDATORS = {requirements, design, tasks}` (реюз существующих)
- [ ] `validate_spec_stage` диспатчит по `validator_key`
- [ ] Вердикты для `lite` не изменились (тесты validate зелёные)

**Traces to:** [REQ-304], [DESIGN-304]
**Depends on:** [TASK-301]
**Blocks:** [TASK-306]

---

## Milestone 3: Конфиг + CLI

### TASK-305: spec_profile в config + резолв
🟠 P1 | ⬜ TODO | Est: 2h

**Checklist:**
- [ ] `ExecutorConfig.spec_profile: str = "lite"`
- [ ] Резолв имени → `StageProfile`; неизвестное → `ConfigError` со списком доступных
- [ ] Тест дефолта и ошибки

**Traces to:** [REQ-306], [DESIGN-306]
**Depends on:** [TASK-301]
**Blocks:** [TASK-307]

---

### TASK-306: флаг --profile в CLI
🟠 P1 | ⬜ TODO | Est: 1-2h

**Checklist:**
- [ ] `plan --gated --profile` и `spec ... --profile` → config
- [ ] Дефолт без флага = `lite`
- [ ] CLI-тест

**Traces to:** [REQ-306], [DESIGN-306]
**Depends on:** [TASK-302], [TASK-303], [TASK-304]
**Blocks:** [TASK-307]

---

## Milestone 4: Верификация

### TASK-307: verification — zero behaviour change
🔴 P0 | ⬜ TODO | Est: 2h

**Description:**
Доказать инвариант REQ-305: дефолтный `lite` идентичен старому поведению.

**Checklist:**
- [ ] Полный прогон существующего тест-сьюта — всё зелёное **без правок тестов**
- [ ] E2E `plan --gated` без `--profile` даёт тот же результат, что до C1 (golden-сравнение
      сгенерированных requirements/design/tasks на фикстуре)
- [ ] Негатив: `--profile nonexistent` → внятная ошибка, не трейсбек
- [ ] Проверка экспорта `SPEC_STAGES` неизменным

**Traces to:** [REQ-305], [REQ-307]
**Depends on:** [TASK-305], [TASK-306]
**Blocks:** -
