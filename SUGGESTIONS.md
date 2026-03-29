# Предложения по доработке проекта executor

## 1. Текущие узкие места

### 1.1. Последовательное выполнение (главный bottleneck)

DAG-зависимости парсятся корректно, но выполнение **строго последовательное** — `task = ready_tasks[0]`. Если 10 независимых P0-задач по 30 мин каждая — это 300 мин вместо 30.

- `executor.py:1801` — всегда берёт первую ready-задачу
- `task.py:369` — фильтрация готовых задач есть, но parallel dispatch нет

### 1.2. "Тупой" retry без обучения на ошибках

- Фиксированная задержка 5 сек (нет exponential backoff)
- Нет классификации ошибок (transient vs permanent vs rate-limit)
- Контекст предыдущих попыток обрезается до 30KB (`executor.py:810`)
- Claude видит усечённый текст ошибки, но не структурированный анализ причины

### 1.3. Отсутствие трекинга токенов и стоимости

- Нет подсчёта input/output tokens
- Нет аккумуляции стоимости по задачам/попыткам
- HTTP callback (`executor.py:90-98`) не включает cost info

### 1.4. Хрупкость state файла

- Полная перезапись JSON на каждую попытку (`executor.py:495-521`)
- Нет WAL/журналирования — crash mid-write = потеря данных
- Вся история attempts хранится в памяти без pruning

### 1.5. Review — автоматический, без HITL

- Маркер-based (`REVIEW_PASSED`/`REVIEW_FIXED`/`REVIEW_FAILED`)
- Нет human approval step
- Review prompt не включает контекст задачи, чеклист, предыдущие ошибки

### 1.6. Тестирование — покрыто ~10%

- 41 тест — только config path resolution
- ZERO тестов на: retry, DAG, state recovery, hooks, review, prompt rendering, git ops

---

## 2. Заимствования из других проектов монорепы

### 2.1. Параллельное выполнение из Maestro (scope conflict prevention)

- **Проблема:** sequential execution при наличии DAG
- **Взять из Maestro:**
  - Topological sort + level assignment для определения параллельных задач
  - Scope conflict detection (glob-pattern matching) — предотвращение конфликтов файлов
  - Concurrency limit (`max_concurrent`) — контролируемый параллелизм
- **Реализация:** asyncio + semaphore для concurrent subprocess execution
- **Объём:** ~150-200 строк в новом модуле `parallel.py`
- **Не брать:** multi-process worktree decomposition — overkill для executor

### 2.2. Intelligent retry из hive (reflexion loop)

- **Проблема:** retry повторяет то же самое, Claude не учится на ошибках
- **Взять из hive:**
  - Structured feedback: 4 вердикта вместо binary (ACCEPT / RETRY / REPLAN / ESCALATE)
  - Error categorization: transient → backoff, permanent → fail fast, rate-limit → exponential backoff
  - Контекст между попытками: structured JSON (что пробовали, что не сработало, гипотеза)
- **Реализация:** заменить `range(attempts)` loop на state machine с вердиктами
- **Объём:** ~100 строк в `retry_strategy.py`
- **Не брать:** полный reflexion с LLM-judge — executor ориентирован на тесты как критерий успеха

### 2.3. Cost tracking из Maestro (per-task USD)

- **Проблема:** нулевая видимость стоимости
- **Взять из Maestro:**
  - Token counting через anthropic SDK (input_tokens, output_tokens из response)
  - Cost accumulator per task + per attempt в state
  - Budget enforcement: прекращение задачи при превышении порога
- **Реализация:** обёртка вокруг subprocess output parsing (Claude CLI выводит token usage)
- **Объём:** ~50 строк в существующем `record_attempt()`
- **Не брать:** полную SSE cost dashboard — достаточно summary в progress log

### 2.4. SQLite state из arbiter (crash-safe persistence)

- **Проблема:** JSON state file хрупок, нет WAL, нет incremental updates
- **Взять из arbiter:**
  - SQLite с WAL mode для атомарных записей
  - Таблицы: tasks, attempts, outcomes (вместо монолитного JSON)
  - Retry-on-lock backoff (50ms, 100ms, 200ms)
- **Реализация:** заменить `ExecutorState` JSON на SQLite через `aiosqlite`
- **Объём:** ~200 строк в `state_db.py`
- **Не брать:** полную schema (decisions, agent_stats) — executor проще

### 2.5. Plan review из plannotator (HITL для критических задач)

- **Проблема:** review автоматический, нет human-in-the-loop
- **Взять из plannotator:**
  - Опциональный HITL gate для P0/critical задач
  - Annotation UI для review результатов (approve/deny/comment)
  - Блокирующий hook с timeout
- **Реализация:** optional `--hitl-review` flag → запуск plannotator перед commit
- **Объём:** ~30 строк интеграции (plannotator уже standalone)
- **Не брать:** полный annotation workflow — executor ориентирован на автоматизацию

### 2.6. Structured logging из atp-platform

- **Проблема:** text-based logs, нет structured events
- **Взять из atp-platform:**
  - structlog с contextual fields (task_id, attempt, duration, status)
  - JSON log format для machine parsing
  - Per-task metrics: tokens, cost, duration, retries
- **Объём:** ~40 строк замены print → structlog
- **Не брать:** OpenTelemetry tracing — overkill для CLI tool

### 2.7. ErrorOr / Result type из codebuff → Типизированные ошибки

- **Проблема:** exceptions используются повсюду; нет дисциплины обработки ошибок; retry logic полагается на generic Exception
- **Взять из codebuff:**
  - `ErrorOr<T> = Success<T> | Failure<E>` — discriminated union
  - Заставляет обрабатывать оба случая (success и failure) явно
  - `getErrorObject()` — structured extraction с code, message, stack
- **Python-реализация:**
  ```python
  @dataclass
  class Success(Generic[T]):
      value: T
      success: bool = True

  @dataclass
  class Failure:
      error: str
      code: ErrorCode | None = None
      success: bool = False

  Result = Success[T] | Failure
  ```
- **Применение:** `execute_task()` возвращает `Result[TaskOutcome]` вместо raise; retry logic проверяет `result.code` для classification
- **Объём:** ~30 строк (result.py) + рефакторинг execution.py и hooks.py
- **Не брать:** PromptResult — executor не interactive (кроме HITL review)

### 2.8. Generator-based task execution из codebuff → Multi-step задачи

- **Проблема:** задача = один вызов Claude CLI subprocess. Нет промежуточных проверок, нет программного контроля.
- **Взять из codebuff:**
  - `handleSteps` generator: yield STEP → проверить вывод → решить что делать дальше
  - Multi-step execution: generate code → run tests → fix if needed → review → commit
  - Программный контроль: человек пишет логику, LLM генерирует контент
- **Реализация:**
  ```python
  def execute_task_steps(task: Task) -> Generator[StepDirective, StepResult, None]:
      result = yield RunAgent(task.prompt)
      if "TASK_FAILED" in result.stdout:
          result = yield RunAgent(task.prompt, context=result.stderr)
      test_result = yield RunTests(task.validation_cmd)
      if test_result.exit_code != 0:
          yield RunAgent(f"Fix test failures: {test_result.stderr}")
      yield Complete()
  ```
- **Объём:** ~200 строк в `generator_executor.py`
- **Не брать:** STEP_ALL/GENERATE_N — executor не управляет LLM напрямую; достаточно STEP + CHECK

### 2.9. Propose pattern из codebuff → Preview + HITL перед commit

- **Проблема:** review автоматический (marker-based), нет preview изменений перед commit
- **Взять из codebuff:**
  - `propose_write_file`: показать diff без записи
  - Интеграция с HITL gate из plannotator: propose → human approve → commit
  - Каждый коммит показывается как "proposed change" перед push
- **Реализация:** расширить `--hitl-review` флаг:
  - После code review: показать summary of changes (diff stats + key files)
  - Ждать approve/deny
  - При approve → commit + merge; при deny → mark NEEDS_REVIEW
- **Объём:** ~60 строк в hooks.py (расширение существующего HITL)
- **Не брать:** per-file proposals — слишком granular для task runner

### 2.10. Multi-provider fallback из codebuff → CLI fallback chain

- **Проблема:** executor использует только Claude CLI. При rate limit или timeout — задача fails.
- **Взять из codebuff:**
  - Fallback chain: `agent_chain: [claude, codex, aider]` в config
  - При failure первого CLI → попробовать следующий
  - Health check: can I call this CLI? → next in chain
- **Реализация:**
  ```yaml
  # executor.config.yaml
  agent_chain:
    - cli: claude
      args: ["--model", "opus"]
    - cli: codex
      args: ["--model", "gpt-5"]
    - cli: aider
      args: ["--model", "sonnet"]
  ```
- **Объём:** ~60 строк в runner.py (fallback loop)
- **Не брать:** OpenRouter API routing — executor работает с CLI subprocess

### 2.11. Streaming events из codebuff → Live TUI updates

- **Проблема:** TUI dashboard (tui.py) читает state из SQLite периодически. Нет real-time обновлений.
- **Взять из codebuff:**
  - Event streaming: subprocess stdout → event parser → TUI update
  - Event types: step_started, tool_called, test_passed, error
  - Nested events с parent IDs для multi-task view
- **Реализация:** subprocess stdout → pipe → event bus → TUI subscriber
- **Объём:** ~100 строк (event protocol + TUI integration)
- **Не брать:** SSE/WebSocket — TUI не нуждается в HTTP transport

---

## 3. Quick wins (высокий импакт, низкие усилия)

| # | Что сделать | Усилия | Импакт |
|---|------------|--------|--------|
| 1 | Структурированные коды ошибок (SYNTAX, TIMEOUT, DEPENDENCY, RATE_LIMIT) | 2ч | Умный retry, лучшая диагностика |
| 2 | Улучшить retry context — показать Claude что именно пробовали и почему не сработало | 3ч | Меньше повторных ошибок, экономия токенов |
| 3 | Token counting из stdout Claude CLI | 2ч | Видимость стоимости |
| 4 | Включить task description + checklist в review prompt | 1ч | Качество review |
| 5 | 10 базовых тестов на retry/DAG/state | 4ч | Защита от регрессий |

---

## 4. Что НЕ брать

| Паттерн | Источник | Причина отказа |
|---------|----------|---------------|
| Multi-process worktrees | Maestro | executor — single-directory tool, worktrees усложнят git flow |
| Goal-driven graph generation | hive | executor работает с готовыми spec-файлами, не генерирует планы |
| MCP tools ecosystem | hive, klaw.sh | executor вызывает CLI-агентов, не управляет tools напрямую |
| Container isolation | nanoclaw | executor доверяет local environment, sandbox не нужен |
| Multi-channel gateway | openclaw | executor — CLI tool, не messaging platform |
| Policy engine routing | arbiter | executor знает какой CLI использовать из config |
| vtable extensibility | nullclaw | Python + config-driven подход проще и достаточен |

---

## 5. TUI Kanбан-дашборд (оригинальная идея)

### Концепция

Kanban-доска в терминале — колонки соответствуют жизненному циклу задач executor'а. Нигде в монорепе такого нет (hive — graph view, klaw.sh — status cards, manbot — web dashboard). TUI-канбан для spec-runner — оригинальный подход.

### Макет

```
┌─ BLOCKED (2) ──┬─ TODO (5) ─────┬─ IN PROGRESS ──┬─ DONE (8) ─────┬─ FAILED (1) ──┐
│                │                │                │                │               │
│ TASK-012 🔴P0  │ TASK-003 🔴P0  │ TASK-007 ⬜P1  │ TASK-001 ✅    │ TASK-009 ❌   │
│ Auth service   │ API endpoints  │ ▓▓▓▓░░ 67%     │ 2m31s · $0.12  │ 3 attempts    │
│ ← TASK-007     │                │ attempt 2/3    │                │ TIMEOUT       │
│                │ TASK-004 ⬜P1  │                │ TASK-002 ✅    │               │
│ TASK-015 ⬜P2  │ DB migrations  │                │ 1m05s · $0.04  │               │
│ Tests          │                │                │                │               │
│ ← TASK-012     │ TASK-006 🟡P2  │                │ ...            │               │
│                │ Docs update    │                │                │               │
├────────────────┴────────────────┴────────────────┴────────────────┴───────────────┤
│ Total: 16 tasks │ Tokens: 45.2K in / 12.8K out │ Cost: $0.84 │ Elapsed: 14m32s  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Карточка задачи показывает

- **BLOCKED**: task ID, приоритет, название, кто блокирует (← TASK-XXX)
- **TODO**: task ID, приоритет, название
- **IN PROGRESS**: progress bar, текущая попытка (attempt 2/3), elapsed time
- **DONE**: время выполнения, стоимость
- **FAILED**: количество попыток, тип ошибки (TIMEOUT / SYNTAX / DEPENDENCY)

### Footer — агрегированные метрики

- Total tasks + breakdown по статусам
- Token usage (input/output)
- Accumulated cost ($)
- Elapsed wall-clock time

### Технический стек

- **Textual** (Python) — тот же стек что hive (TextualUI); rich-виджеты, async, mouse support
- **Интеграция**: читает `ExecutorState` (JSON/SQLite) + подписка на события через file watcher или event bus
- **Объём**: ~300-400 строк в новом модуле `tui_dashboard.py`
- **Запуск**: `spec-runner dashboard` или `spec-runner run --all --tui`

### Возможные расширения (не для MVP)

- Клик на карточку → детали задачи (prompt, output, errors)
- Горячие клавиши: `r` retry failed, `s` skip, `p` pause
- DAG-граф зависимостей (как `spec-task graph`, но live)
- Фильтр по приоритету / тегам

### Почему это хорошо для executor

1. executor — CLI tool, пользователь уже в терминале
2. Задачи имеют естественные Kanban-состояния
3. При parallel execution (Phase 2) визуализация прогресса критична
4. Textual даёт mouse + keyboard, не требует браузера
5. Дифференцирует от конкурентов (все делают web dashboards)

---

## 6. Приоритетный roadmap (revised)

### Phase 0: Foundation — декомпозиция и тесты (2 дня)

**Почему это первое:** executor.py — 2313 строк монолит. Добавлять модули
(parallel.py, retry_strategy.py, state_db.py) к монолиту без тестов — путь к
"работает, но боюсь трогать". Декомпозиция + тесты делают все последующие фазы
безопасными и быстрыми.

**Декомпозиция executor.py → модули:**

| Модуль | Содержимое | ~строк |
|--------|-----------|--------|
| `config.py` | `ExecutorConfig`, загрузка YAML, path resolution | 250 |
| `state.py` | `ExecutorState`, `TaskState`, `TaskAttempt`, persistence | 200 |
| `prompt.py` | `build_task_prompt()`, template rendering, context formatting | 300 |
| `hooks.py` | pre/post hooks, git operations (branch, commit, merge) | 400 |
| `runner.py` | subprocess execution, output parsing, TASK_COMPLETE/FAILED detection | 300 |
| `executor.py` | CLI + оркестрация (main loop, retry coordination) | ~500 |

**Тесты (40-50 штук, параллельно с декомпозицией):**
- config: path resolution, YAML merge, CLI override precedence
- state: save/load/recovery, attempt recording, consecutive failure tracking
- prompt: template rendering, error context truncation, checklist formatting
- hooks: execution sequence, git branch naming, failure handling
- runner: TASK_COMPLETE/FAILED detection, timeout, output parsing
- task: DAG resolution, dependency promotion, priority ordering
- retry: attempt counting, error forwarding, API error detection

### Phase 1: Reliability (1-2 дня)
- SQLite state вместо JSON (теперь безопасно — state.py выделен, тесты есть)
- Structured error codes (SYNTAX, TIMEOUT, DEPENDENCY, RATE_LIMIT)
- Улучшенный retry context (structured JSON: что пробовали, почему не сработало)

### Phase 2: Performance (2-3 дня)
- Parallel execution independent задач (asyncio + semaphore в runner.py)
- Token/cost tracking (парсинг stdout Claude CLI)
- Budget enforcement (per-task и глобальный лимиты)

### Phase 3: Visibility (по необходимости)
- Structured logging (structlog, ~40 строк — quick win, делать сразу)
- Streaming events для live TUI updates (из codebuff)
- TUI Kanban dashboard — **только после Phase 2**, когда параллельное выполнение
  делает визуализацию прогресса реально нужной

### Phase 4: Quality (1-2 дня)
- HITL review integration (из plannotator) + propose pattern (из codebuff)
- Review prompt с контекстом задачи, чеклистом, предыдущими ошибками
- ErrorOr / Result type для structured error handling (из codebuff)

### Phase 5: Intelligence (2-3 дня)
- Generator-based task execution — multi-step с промежуточными проверками (из codebuff)
- Multi-provider fallback chain (из codebuff)

---

## 7. Что НЕ менять в Phase 0

Декомпозиция — **строго рефакторинг**, без изменения поведения:
- Не менять формат state JSON
- Не менять CLI интерфейс и аргументы
- Не менять формат spec/tasks.md
- Не менять exit codes и stdout output
- Все существующие тесты должны проходить

---

## 8. Заимствования из research-проектов (BMAD, GSD, OpenSpec, ralphex, spec-kit, spec-workflow)

> Анализ 6 проектов из `research/` в контексте executor. Сравнение архитектур,
> выявление конкретных идей для заимствования, оценка ROI.

### 8.1. Structured Agent Personas (из BMAD-METHOD)

**Идея:** Разные фазы executor (planning, execution, review) используют разные
"персоны" — специализированные system prompts + контекст + модели.

**Вариант A — Persona как prompt template (рекомендуется первым):**

```yaml
# executor.config.yaml
personas:
  architect:
    system_prompt: "You are a senior architect..."
    model: "opus"
    focus: ["design.md", "requirements.md"]
    used_in: [spec_design, spec_requirements]
  implementer:
    system_prompt: "You are a focused implementer..."
    model: "sonnet"
    focus: ["tasks.md", "src/"]
    used_in: [task_execution]
  reviewer:
    system_prompt: "You are a code reviewer..."
    model: "sonnet"
    focus: ["diff", "tests/"]
    used_in: [code_review]
  qa:
    system_prompt: "You are a QA engineer..."
    model: "haiku"
    focus: ["tests/", "spec/requirements.md"]
    used_in: [post_validation]
```

Реализация: расширить `prompt.py` — на этапе `build_prompt()` подставлять persona
из конфига в зависимости от фазы. **~100-150 строк, 1-2 дня.**

**Вариант B — Persona как отдельный subprocess (отложить):**

Каждая persona — отдельный вызов Claude CLI с изолированным контекстом.
Делать только если prompt templates недостаточно. **~300-500 строк, 3-5 дней.**

**Выгоды:**

| Выгода | Механизм |
|--------|----------|
| Качество design.md | Architect-persona фокусируется на архитектуре, не отвлекаясь на код |
| Экономия токенов | Каждая persona получает только релевантный контекст |
| Разные модели под роли | Architect → Opus, implementer → Sonnet, QA → Haiku |
| Специализированный review | 5 review-агентов (как в ralphex) лучше одного универсального |
| Traceability | Каждая persona оставляет свой артефакт |

**Проблемы:**

| Проблема | Серьёзность | Митигация |
|----------|-------------|-----------|
| Потеря контекста между personas | Высокая | design.md должен быть достаточно детальным для передачи решений |
| Prompt engineering × N | Средняя | Начать с 3 persona (architect/implementer/reviewer), расширять по необходимости |
| Увеличение latency (2-4 вызова вместо 1) | Средняя | Вариант A не добавляет вызовов — только меняет prompt |
| Рассогласование между personas | Высокая | Нужна верификация consistency между фазами (checklist cross-check) |
| Over-engineering для простых задач | Средняя | Маршрутизация по сложности: P3 задачи → один prompt без persona pipeline |

### 8.2. Step-File Architecture (из BMAD-METHOD)

**Идея:** Вынести execution pipeline в декларативный конфиг вместо hardcoded
последовательности в `cli.py` + `hooks.py`.

```yaml
# spec/workflow.yaml
workflow:
  - step: validate
    action: validate_config_and_tasks
    skip_if: "--skip-validation"
  - step: plan_review
    action: human_approval
    prompt: "Review the task plan before execution?"
    skip_if: "--no-approval"
  - step: execute
    action: execute_task
    persona: implementer
  - step: test
    action: run_command
    command: "{commands.test}"
    on_failure: retry_task
  - step: lint
    action: run_command
    command: "{commands.lint}"
    on_failure: auto_fix
  - step: review
    action: code_review
    persona: reviewer
    skip_if: "--no-review"
  - step: hitl_gate
    action: human_approval
    prompt: "Approve changes?"
    skip_if: "--auto-approve"
  - step: commit
    action: git_commit
```

**Реализация:** Новый модуль `workflow.py` (~200-300 строк), рефакторинг
`cli.py` и `hooks.py`. **3-5 дней.**

**Выгоды:**

| Выгода | Механизм |
|--------|----------|
| Customizable pipeline | Пользователь добавляет/убирает шаги через YAML |
| Visible execution contract | workflow.yaml — документация pipeline (сейчас разбросан по 4 файлам) |
| Checkpoint / HITL gates | Human approval в любой точке |
| Skip/conditional steps | Быстрый путь для простых задач |
| Retry per step | Retry только failed step, не перезапуск всей задачи |
| Observability | Каждый step — отдельное событие в логе |

**Проблемы:**

| Проблема | Серьёзность | Митигация |
|----------|-------------|-----------|
| Рефакторинг hooks.py | Высокая | Миграция implicit hooks → explicit steps без потери обратной совместимости |
| State complexity | Средняя | Трекинг task × step × retry в SQLite; расширение schema |
| Overhead для простых случаев | Средняя | Sensible defaults: без workflow.yaml → текущее поведение |
| Параллелизм + steps = комбинаторный взрыв | Высокая | 3 задачи × 7 steps = 21 step-states; parallel.py усложняется |
| YAML-driven control flow | Средняя | Ограничить on_failure до 3-4 actions, не делать Turing-complete DSL |

### 8.3. Fresh Context Per Task (из get-shit-done)

**Идея:** Каждая задача исполняется в отдельном subprocess с минимальным
контекстом (200K window чисто на реализацию). Решает проблему "context rot" —
деградации качества при длинных сессиях.

Executor уже запускает Claude CLI как subprocess, но передаёт accumulated
контекст ошибок. GSD идёт дальше: каждая задача получает только свой PLAN.md +
PROJECT.md + STATE.md, без истории предыдущих задач.

**Что взять:**
- **STATE.md** — persistent cross-session state для возобновления
- **Deviation rules** — auto-fix протокол (3 попытки) vs "спросить пользователя"
- **Wave execution** — группировка задач по зависимостям в волны

**Проблема:** executor уже forwarding error context между attempts — это полезно
для retry. Полная изоляция потеряет этот контекст. Баланс: изолировать между
*задачами*, но сохранять контекст между *попытками* одной задачи.

### 8.4. Parallel Review Agents + Pause/Resume (из ralphex)

**ralphex — ближайший конкурент executor.** Оба гоняют Claude CLI по плану
с чекбоксами, retry, code review, git integration.

**Где ralphex сильнее (стоит заимствовать):**

| Фича | Что даёт | Оценка усилий |
|------|---------|---------------|
| 5 параллельных review-агентов (quality, implementation, testing, simplification, docs) | Глубже review, ловит разные классы багов | 2-3 дня (расширение hooks.py) |
| External review loop (Codex) с stalemate detection | Второе мнение + защита от бесконечного loop | 1 день |
| Pause/resume (Ctrl+\) — редактирование плана mid-run | Гибкость при длинных runs | 1 день (signal handler) |
| Worktree isolation для параллельных планов | Безопасный параллелизм | 2 дня |
| Docker isolation | Безопасное автономное исполнение | 2-3 дня |
| Notifications (Telegram, Slack, Email) | Удобство при long-running tasks | 1 день |
| Idle/session timeouts | Защита от зависаний | 0.5 дня |

**Где executor сильнее (не терять):**

- DAG с зависимостями (ralphex — только последовательные задачи)
- Structured task model (TASK-XXX с приоритетами, milestone, traceability)
- MCP server для интеграции с Claude Code
- Plugin system с hooks
- Cost tracking с budget enforcement

### 8.5. Delta Specs (из OpenSpec)

**Идея:** Для brownfield-задач не нужна полная спека — достаточно дельты
(ADDED/MODIFIED/REMOVED). При архивации дельты мёржатся в основные спеки.

**Применимость к executor:** ограничена. Executor работает с `tasks.md` как
source of truth, не с поведенческими спеками. Но идея delta-specs полезна для
инкрементальных фаз (`--spec-prefix=phase2-`): phase2-tasks.md мог бы содержать
только дельту к phase1.

### 8.6. Constitution + Extension Ecosystem (из spec-kit)

**Идея:** Immutable project principles (constitution.md) проверяемые на каждом
шаге + формальный API для community extensions.

**Что взять:**
- **Constitution как guardrail:** добавить `spec/constitution.md` — набор
  инвариантов ("never delete migrations", "all endpoints must have auth").
  Включать в prompt на каждом шаге.
- **Extension API:** формализовать plugin system (уже есть `spec/plugins/`)
  до уровня, позволяющего community extensions.

**Объём:** constitution — 1 день (prompt.py), extension API — 2-3 дня.

### 8.7. Role-Based Agent Chain (из spec-workflow)

**Идея:** Pipeline из специализированных агентов (BA → Architect → Team Lead →
Dev Manager → Dev), каждый читает output предыдущего.

**Пересечение с §8.1 (personas):** это по сути вариант B (persona как subprocess).
spec-workflow доводит до 5 ролей с формальными шаблонами input/output.

**Что взять:**
- **Context chaining** — каждый агент получает только output предыдущего
- **Copy-paste ready tasks** — team-lead генерирует задачи с exact file paths
  и insertion points (полезно для `spec-runner plan --full`)

---

## 9. Сводная таблица: research-проекты vs executor

| | **executor** | **BMAD** | **GSD** | **OpenSpec** | **ralphex** | **spec-kit** | **spec-workflow** |
|---|---|---|---|---|---|---|---|
| **Фокус** | Task execution | Методология полного цикла | Context engineering | Delta-спеки (brownfield) | Автономный plan executor | Экосистема extensions | Agent chain |
| **Спеки** | Markdown tasks.md | YAML skills + steps | XML tasks + PLAN.md | Delta specs (A/M/R) | Markdown + checkboxes | Constitution → Spec → Plan | FR (BDD) → Design → Tasks |
| **Параллелизм** | DAG + asyncio | Нет | Waves | Параллельные changes | Worktree isolation | Задачи [P] | Нет |
| **Review** | Code review (1 agent) | Party Mode | Нет | Нет | 5 agents + external | Extensions | Build+test only |
| **HITL** | --hitl-review | Checkpoints | Checkpoints (3 типа) | Fluid | Ctrl+\ pause | Clarification phase | Approval gates |
| **Расширяемость** | Plugins + templates | Modules + skills | Конфигурируемые агенты | Schemas + 20 tools | Custom agents + Docker | 50+ extensions | Skill files |
| **Уникальная сила** | DAG + structured tasks + MCP | 32 skills, 9 personas | Fresh context, waves | Delta merge | 5-phase review pipeline | Community ecosystem | Surgical code insertion |

### Приоритет заимствований

| # | Что | Откуда | ROI | Рекомендация |
|---|-----|--------|-----|-------------|
| 1 | Personas как prompt templates | BMAD | Высокий | **Делать первым.** Минимальные изменения, ощутимый результат для quality + cost |
| 2 | Human approval gates | BMAD + ralphex | Высокий | **Делать.** Один `--approve-before-commit` flag решает главный HITL use case |
| 3 | Parallel review agents (5 ролей) | ralphex | Высокий | **Делать.** Расширение hooks.py, глубже review |
| 4 | Pause/resume mid-run | ralphex | Средний-высокий | **Делать.** Signal handler + plan re-read, 1 день |
| 5 | Configurable pipeline (workflow.yaml) | BMAD | Средний | **Делать частично:** вынести pipeline в конфиг, но без полного step-state tracking |
| 6 | Constitution guardrails | spec-kit | Средний | **Делать.** constitution.md в prompt — 1 день |
| 7 | Idle/session timeouts | ralphex | Средний | **Делать.** Защита от зависаний, 0.5 дня |
| 8 | Notifications (Telegram/Slack) | ralphex | Низкий-средний | **По необходимости.** Полезно для long-running tasks |
| 9 | Personas как subprocess (isolated) | BMAD + spec-workflow | Средний | **Отложить.** Только если prompt templates недостаточно |
| 10 | Docker isolation | ralphex | Низкий | **Отложить.** executor доверяет local environment |
| 11 | Delta specs | OpenSpec | Низкий | **Не делать.** Executor не работает с поведенческими спеками |
