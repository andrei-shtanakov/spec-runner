.PHONY: test lint typecheck format e2e

test:
	uv run pytest tests/ -v -m "not slow"

e2e:
	uv run pytest tests/ -v -m slow

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src

format:
	uv run ruff format .
	uv run ruff check . --fix
