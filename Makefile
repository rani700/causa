.PHONY: install test lint sim exasol-up exasol-down

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src

sim:
	uv run causa sim run

exasol-up:
	docker compose -f docker/docker-compose.yml up -d

exasol-down:
	docker compose -f docker/docker-compose.yml down
