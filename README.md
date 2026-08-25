# VidGen

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

This foundation implements roadmap tasks T01 through T04:

- Python monorepo, CI, and local infrastructure
- Versioned Pydantic contracts plus exported JSON Schema
- PostgreSQL data model and Alembic migration
- Content-addressed asset storage with provenance
- Deterministic fake AI and media providers for offline tests

## Quick start

```bash
cp .env.example .env
uv sync --all-groups
make verify
```

With Docker installed:

```bash
docker compose up -d postgres redis azurite
uv run alembic upgrade head
make verify-stack
```

The default application database is `postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen`.

## Packages

- `vidgen.contracts`: canonical inter-stage contracts
- `vidgen.db`: relational models and repositories
- `vidgen.storage`: content-addressed storage and asset service
- `vidgen.providers`: provider protocols and deterministic fakes

## Contract schemas

Run `make schemas` to export JSON Schema into `packages/contracts/schema`.

