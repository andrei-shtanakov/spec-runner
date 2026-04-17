# Repository Guidelines

## Project Structure & Module Organization
Code lives under `src/spec_runner`, which exposes the `spec-runner`, `spec-task` (deprecated — use `spec-runner task`), and `spec-runner-init` entry points declared in `pyproject.toml`. Specs, prompts, and workflow notes are grouped under `spec/` (`requirements.md`, `design.md`, `tasks.md`, `FORMAT.md`, `WORKFLOW.md`, `prompts/`). Automation settings (retries, hooks, review agents, notifications, personas) live in `spec-runner.config.yaml` at the project root — the v2.0 location. Legacy `spec/executor.config.yaml` is still read with a deprecation warning. Runtime state/logs land in `spec/.executor-state.db`, `spec/.executor-logs/`, and `spec/.task-history.log`. The Maestro interop contract lives in `docs/state-schema.md`, `schemas/*.json`, and `tests/fixtures/maestro-interop/`. Distribution artifacts appear in `dist/`. Keep new assets alongside the module they serve, and prefer adding new executors/features as submodules in `src/spec_runner/`.

## Build, Test, and Development Commands
- `uv sync`: installs all dependencies (including dev group) into the virtual environment.
- `spec-runner run --task=TASK-001`: executes a specific task from `spec/tasks.md`; omit `--task` to run the next ready one, or pass `--all` to drain the ready queue.
- `spec-runner task list --status=todo`: inspects backlog status without running anything.
- `uv run pytest tests -v`: executes the Python test suite (add `-m "not slow"` to skip slow markers).
- `uv run ruff check .` / `uv run ruff format .` / `uv run mypy src`: enforce linting, formatting, and type safety; the executor triggers ruff + configured tests after each task when `hooks.post_done` stays enabled.
- `make test` / `make lint` / `make typecheck` / `make format`: Makefile wrappers for the above.

## Coding Style & Naming Conventions
Python 3.10+ with Ruff line length 100 (`pyproject.toml`) is the baseline. Prefer explicit imports, slug-style module names, and descriptive dataclasses for task metadata. Use typing annotations everywhere that touches executor state so mypy can run cleanly. Branches should follow the automated pattern `task/task-###-short-name`, mirroring task IDs defined in `spec/tasks.md`. Keep config keys lowercase-with-underscores to match the YAML files already in the repo.

## Testing Guidelines
Pytest is configured to look in `tests/`; group new cases by CLI module (e.g. `tests/test_execution.py`, `tests/test_gh_sync.py`). Mark potentially long-running scenarios with `@pytest.mark.slow` so contributors can opt in via `uv run pytest -m slow`. Aim to cover new command paths, error branches, and hook orchestration logic; mocking Claude CLI invocations keeps runs fast. Regression tests are required for bug fixes. Contract tests live in `tests/test_json_result_contract.py` — any change to the Maestro-facing `--json-result` format or SQLite state surface needs to regenerate goldens via `uv run pytest tests/test_json_result_contract.py --update-golden` and bump the major version.

## Commit & Pull Request Guidelines
Recent commits use short, imperative summaries (e.g. "fix: remove env var fallback for Telegram credentials", "feat(R-04): freeze Maestro interop contract"). Follow that style, reference the relevant TASK/REQ IDs when applicable, and keep changes scoped so automated review agents can reason about them. Before opening a PR, ensure `pytest -m "not slow"`, `ruff check`, and `mypy` succeed and describe: (1) the task or issue addressed, (2) how to reproduce/verify, and (3) any config impacts (e.g. updates to `spec-runner.config.yaml`). Screenshots or log snippets help reviewers confirm agent workflows remain stable.

## Agent Workflow Tips
Hooks live under `hooks.pre_start` / `hooks.post_done` in `spec-runner.config.yaml` (e.g. `create_git_branch`, `run_tests`, `run_lint`, `run_review`, `auto_commit`, `review_parallel`, `review_roles`) and drive CI-like behavior locally. Pause automation by toggling the relevant flags instead of editing scripts. Credentials (Telegram bot token, webhook URLs, persona model names) must be set explicitly in the config file — env-var fallbacks have been removed so that Maestro-managed worktrees have one canonical opt-in path. Notifications fire on `run_complete`, `task_failed`, and `state_degraded` events by default.
