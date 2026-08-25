# VidGen contributor instructions

VidGen is a restartable media workflow. Keep AI reasoning behind provider interfaces and keep orchestration deterministic.

## Required practices

- Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic, pytest, Ruff, and mypy.
- All inter-stage payloads are versioned Pydantic contracts in `packages/contracts`.
- Every worker and provider call must accept an idempotency key.
- Persist generated asset provenance, content hashes, provider request IDs, and parent asset IDs.
- Never make paid provider calls in tests. Use deterministic fakes from `packages/providers`.
- Database migrations must upgrade from an empty database and downgrade cleanly.
- Use UTC timestamps and UUID primary keys.

## Before committing

Run `make verify`. If Docker is available, also run `make verify-stack`.

