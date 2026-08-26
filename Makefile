.PHONY: install lint format typecheck test schemas verify infra-up infra-down migrate migrate-down verify-stack run-api

export UV_CACHE_DIR ?= .uv-cache

install:
	uv sync --all-groups

lint:
	uv run ruff check src apps services packages tests scripts migrations

format:
	uv run ruff format src apps services packages tests scripts migrations
	uv run ruff check --fix src apps services packages tests scripts migrations

typecheck:
	uv run mypy --strict src apps services packages scripts

test:
	uv run pytest --cov=vidgen --cov-report=term-missing

schemas:
	uv run python scripts/export_schemas.py

verify: lint typecheck test schemas

infra-up:
	docker compose up -d postgres redis azurite temporal

infra-down:
	docker compose down

migrate:
	uv run alembic upgrade head

migrate-down:
	uv run alembic downgrade base

verify-stack:
	VIDGEN_DATABASE_URL=postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen VIDGEN_ALLOW_DESTRUCTIVE_MIGRATION_TEST=1 uv run python scripts/verify_stack.py

run-api:
	uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
