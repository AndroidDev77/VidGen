.PHONY: install lint format typecheck test schemas verify infra-validate infra-up infra-down infra-logs observability-up observability-logs migrate migrate-down verify-stack run-worker run-control-dispatcher run-api run-web web-install web-lint web-typecheck web-test web-build web-e2e verify-web local-reset local-up local-start local-logs local-status local-down local-stack-reset

export UV_CACHE_DIR ?= .uv-cache

install:
	uv sync --all-groups --all-extras

lint:
	uv run ruff check src apps services packages tests scripts migrations infra workers

format:
	uv run ruff format src apps services packages tests scripts migrations infra workers
	uv run ruff check --fix src apps services packages tests scripts migrations infra workers

typecheck:
	uv run mypy --strict src apps services packages scripts

test:
	uv run pytest --cov=vidgen --cov-report=term-missing

schemas:
	uv run python scripts/export_schemas.py

verify: lint typecheck test schemas

infra-validate:
	./infra/scripts/validate_infrastructure.sh

infra-up:
	docker compose up -d postgres redis azurite temporal

infra-down:
	docker compose down

infra-logs:
	docker compose logs -f postgres redis azurite temporal

observability-up:
	docker compose --profile observability up -d

observability-logs:
	docker compose --profile observability logs -f otel-collector

migrate:
	uv run alembic upgrade head

migrate-down:
	uv run alembic downgrade base

verify-stack:
	VIDGEN_DATABASE_URL=postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen VIDGEN_ALLOW_DESTRUCTIVE_MIGRATION_TEST=1 uv run python scripts/verify_stack.py

run-worker:
	uv run python -m workers.temporal_worker.main

# The T18b control-command dispatcher. Without it every asynchronous product
# command - reference builds, shot regeneration, revisions, manual final QA,
# remediation, project continuation - stays durably queued and never starts its
# workflow. Run it alongside the Temporal worker.
run-control-dispatcher:
	uv run python -m workers.control_dispatcher.main

run-api:
	uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

run-web:
	pnpm --filter @vidgen/web dev

web-install:
	pnpm install

web-lint:
	pnpm --filter @vidgen/web lint

web-typecheck:
	pnpm --filter @vidgen/web typecheck

web-test:
	pnpm --filter @vidgen/web test

web-build:
	pnpm --filter @vidgen/web build

web-e2e:
	pnpm --filter @vidgen/web test:e2e

verify-web: web-lint web-typecheck web-test web-build

# DESTRUCTIVE. Never run automatically. Removes the disposable local data only:
#   - the "postgres-data" Docker volume (the whole local PostgreSQL database)
#   - the "azurite-data" Docker volume (the local Azurite blob emulator store)
#   - VIDGEN_UPLOAD_ROOT (.local-data/uploads: resumable source-video uploads)
#   - VIDGEN_BLOB_ROOT (.local-data/blobs: every content-addressed asset)
# Nothing outside those four locations is touched. Re-run "make infra-up" and
# "make migrate" afterwards to rebuild an empty local environment.
local-reset:
	@printf 'This deletes the local PostgreSQL volume, the Azurite volume, .local-data/uploads and .local-data/blobs. Type "reset" to continue: ' && read answer && [ "$$answer" = reset ]
	docker compose down --volumes
	rm -rf .local-data/uploads .local-data/blobs
	@echo "Local PostgreSQL, Azurite, upload and blob data removed."

# Full containerized local stack (API, worker, control dispatcher, web and
# infrastructure) running against real providers. See scripts/local-stack.sh.
local-up:
	./scripts/local-stack.sh up

local-start:
	./scripts/local-stack.sh start

local-logs:
	./scripts/local-stack.sh logs

local-status:
	./scripts/local-stack.sh status

local-down:
	./scripts/local-stack.sh down

local-stack-reset:
	./scripts/local-stack.sh reset
