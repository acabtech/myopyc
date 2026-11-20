.PHONY: install run test test-fast test-all lint format build clean

PYTHON ?= uv run python

install:
	uv sync --extra dev --extra hardware

run:
	uv run myopyc gui --simulate 1

record-demo:
	uv run myopyc record --seconds 5 --simulate 2

test:
	uv run pytest

# Fast lane: no coverage instrumentation, no process-bound crash tests.
# Use for tight local loops; `make test` remains the authoritative gate.
test-fast:
	uv run pytest -m "not slow" --no-cov

test-all:
	uv run pytest
	uv run pytest -m slow --no-cov

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/myopyc tests

format:
	uv run ruff check --fix .
	uv run ruff format .

build:
	uv build

clean:
	rm -rf dist build .pytest_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
