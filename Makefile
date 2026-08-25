.PHONY: install lint format typecheck test schemas verify infra-up infra-down migrate migrate-down verify-stack

export UV_CACHE_DIR ?= .uv-cache

install:
	uv sync --all-groups

lint:
	uv run ruff check src tests scripts

format:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

typecheck:
	uv run mypy src

test:
	uv run pytest --cov=vidgen --cov-report=term-missing

schemas:
	uv run python scripts/export_schemas.py

verify: lint typecheck test schemas

infra-up:
	docker compose up -d postgres redis azurite

infra-down:
	docker compose down

migrate:
	uv run alembic upgrade head

migrate-down:
	uv run alembic downgrade base

verify-stack:
	uv run python scripts/verify_stack.py
