# Repository Guidelines

## Project Structure & Module Organization
Code lives under `src/spec_runner`, which exposes the `spec-runner`, `spec-task`, and `spec-runner-init` entry points declared in `pyproject.toml`. Specs, prompts, and workflow notes are grouped under `spec/` (`requirements.md`, `design.md`, `tasks.md`, `prompts/`, `WORKFLOW.md`). Automation settings such as retries, hooks, and review agents are defined in `executor.config.yaml`, while runtime logs/state land in `spec/.executor-*`. Distribution artifacts appear in `dist/`. Keep new assets alongside the module they serve, and prefer adding new executors/features as subpackages in `src/spec_runner/`.

## Build, Test, and Development Commands
- `uv sync`: installs all dependencies (including dev group) into the virtual environment.
- `spec-runner run --task=TASK-001`: executes the next ready task from `spec/tasks.md`; replace the ID to target a specific task.
- `spec-task list --status=todo`: inspects backlog status without running anything.
- `pytest tests -v`: executes the Python test suite (add `-m "not slow"` to skip slow markers).
- `ruff check .` and `mypy src`: enforce formatting/linting and type safety; the executor triggers both after each task if hooks stay enabled.

## Coding Style & Naming Conventions
Python 3.10+ with Ruff line length 100 (`pyproject.toml`) is the baseline. Prefer explicit imports, slug-style module names, and descriptive dataclasses for task metadata. Use typing annotations everywhere that touches executor state so mypy can run cleanly. Branches should follow the automated pattern `task/task-###-short-name`, mirroring task IDs defined in `spec/tasks.md`. Keep config keys lowercase-with-underscores to match the YAML files already in the repo.

## Testing Guidelines
Pytest is configured to look in `tests/`; group new cases by CLI (e.g., `tests/test_executor.py`). Mark potentially long-running scenarios with `@pytest.mark.slow` so contributors can opt in via `pytest -m slow`. Aim to cover new command paths, error branches, and hook orchestration logic; mocking Claude CLI invocations keeps runs fast. When modifying the runner, add regression tests before relying on manual `spec-runner run` smoke tests.

## Commit & Pull Request Guidelines
Recent commits use short, imperative summaries (“Fix config path…”). Follow that style, reference the relevant TASK/REQ IDs when applicable, and keep changes scoped so automated review agents can reason about them. Before opening a PR, ensure `pytest`, `ruff`, and `mypy` succeed and describe: (1) the task or issue addressed, (2) how to reproduce/verify, and (3) any config impacts (e.g., updates to `executor.config.yaml`). Screenshots or log snippets help reviewers confirm agent workflows remain stable.

## Agent Workflow Tips
Hooks defined in `executor.config.yaml` (`create_git_branch`, `run_tests`, `run_lint`, `run_review`) drive CI-like behavior locally. If you need to pause automation, toggle the relevant flags instead of editing scripts. Store secret-dependent values via environment variables inside the `environment` section so agents never persist credentials.
