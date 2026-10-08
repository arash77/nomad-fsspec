default:
    @just --list

install:
    uv sync

test:
    uv run pytest -m "not live"

test-live:
    uv run pytest -m live

lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src

fmt:
    uv run ruff format .
    uv run ruff check --fix .

build:
    uv build
