---
spec_stage: design
status: approved
version: 2
generated_by: claude@claude-opus-4-8
generated_at: 2026-07-05
source_prompt_version: sha256:pending
validation: pass
approved_by: andrei-shtanakov
approved_at: '2026-07-06T01:09:00Z'
---

# C1: STAGES → профиль — Technical Design

## Design Principles

### DESIGN-301: Модель профиля
```
StageDef  { name, template, marker_prefix, prompt_text, validator_key, upstream: [name] }
StageProfile { name, stages: [StageDef] }   # порядок = список
```
Профиль `lite` собирается из текущих значений (`prompt.py` карты, `validate.py` валидаторы) —
единый источник вместо трёх разрозненных словарей. Трасса: REQ-301.

### DESIGN-302: Сохранить модульные символы как «lite»
`spec.py::STAGES` и `__init__::SPEC_STAGES` остаются, но выводятся из `lite.stage_names()` —
обратная совместимость экспорта (REQ-305). Пометить deprecated в докстринге, не удалять сейчас.

### DESIGN-303: Прокидывание профиля минимальными сигнатурами
Функции `downstream_stages`/`resolve_next_stage`/`mark_downstream_stale` получают параметр
`stages: Sequence[str] = STAGES` (дефолт = lite) — правки вызовов точечные. Полный `StageProfile`
переносится через `ExecutorConfig.spec_profile`, чтобы не тащить его сквозь все слои руками.
Трасса: REQ-302, REQ-306.

### DESIGN-304: Реестр валидаторов
`validate_spec_stage` заменяет if/elif на `VALIDATORS[stagedef.validator_key](path)`.
Ключи: `requirements`→`validate_requirements`, `design`→`validate_design`, `tasks`→парсер
`task.py`. Профиль ссылается на ключи, не на функции (сериализуемость). Трасса: REQ-304.

### DESIGN-305: Сборка промпта из StageDef
`prompt.py` берёт `template`/`marker_prefix`/`prompt_text` из `StageDef`. Маркеры генерятся из
`marker_prefix` (`{PFX}_READY`/`{PFX}_END`), для `lite` префиксы = `SPEC_REQUIREMENTS`/`SPEC_DESIGN`/
`SPEC_TASKS` — байт-в-байт как сейчас. Трасса: REQ-303.

### DESIGN-306: Конфиг и CLI
`ExecutorConfig.spec_profile: str = "lite"`; загрузчик резолвит имя → `StageProfile` (бандл +
опц. пользовательский путь). CLI `plan --gated --profile` и `spec ... --profile` пробрасывают в
config. Неизвестное имя → `ConfigError` с перечнем доступных. Трасса: REQ-306.

## Профиль `lite` (встроенный, дефолт)
```yaml
profile: lite
stages:
  - {name: requirements, template: requirements.template.md, marker_prefix: SPEC_REQUIREMENTS, validator: requirements, upstream: []}
  - {name: design,       template: design.template.md,       marker_prefix: SPEC_DESIGN,       validator: design,       upstream: [requirements]}
  - {name: tasks,        template: tasks.template.md,        marker_prefix: SPEC_TASKS,        validator: tasks,        upstream: [design]}
```

## Точки изменения (файлы)
| Файл | Что меняется |
|---|---|
| `spec.py` | `StageDef`/`StageProfile`, `lite`; параметризация 4 функций; `STAGES` = `lite.names()` |
| `prompt.py` | template/marker/prompt берутся из `StageDef` |
| `validate.py` | реестр `VALIDATORS`; диспатч по `validator_key` |
| `config.py` | поле `spec_profile` + резолв в `StageProfile` |
| `cli_plan.py` / `cli.py` | флаг `--profile` |
| `profiles/lite.yaml` (new, bundled) | данные профиля по умолчанию |

## Migration / Compatibility
`lite` — строгий дефолт. Существующие тесты и спеки не трогаются (REQ-305, REQ-307).
Никаких изменений в исполнении задач (`stages.py` вне scope).
