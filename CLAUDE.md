# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build, Test, Lint

| Command | Purpose |
|---------|---------|
| `make verify` | Lint (ruff) + typecheck (mypy) + test (pytest) + export schemas |
| `make lint` | Ruff check on src/, apps/, services/, packages/, tests/, scripts/, migrations/, infra/ |
| `make format` | Ruff format and fix |
| `make typecheck` | Mypy strict on src/, apps/, services/, packages/, scripts/ |
| `make test` | Pytest with coverage (`-n auto` for parallel) |
| `make schemas` | Export Pydantic contracts to JSON Schema |
| `make verify-web` | Lint, typecheck, test, build the React app |
| `make web-e2e` | Playwright acceptance tests |

Run a single test: `pytest tests/test_foo.py::test_bar -x`

Before committing: run `make verify`. With Docker: also run `make verify-stack`.

## Local Development

```bash
# One-time setup
cp .env.example .env && cp apps/web/.env.example apps/web/.env
uv sync --all-groups && pnpm install
make infra-up && make migrate

# Run services (separate terminals)
make run-api              # FastAPI on :8000
make run-web              # React on :5173
make run-worker           # Temporal worker
make run-control-dispatcher

# Or full containerized stack
make local-up
```

Infrastructure: PostgreSQL 16, Redis 7, Azurite (Azure Blob emulator), Temporal dev server (UI at :8233).

After rebuilding containers, restart web too (nginx caches API container IP).

## Architecture

VidGen is a restartable media pipeline that turns long-form source video into animated comedy recaps, orchestrated by Temporal workflows.

### Pipeline stages (sequential in ProjectWorkflow)

Upload → Media processing → Transcript (T07B) → Evidence → Episode analysis (T10) → Script (T11) → Narration (T12) → Storyboard (T13) → Shot fan-out (T16, one ShotWorkflow child per shot: T14 keyframe → T20 QA → T15 animation → T20 QA → T21 repair) → Render (T17b) → Final editorial QA (T22)

### Directory structure

- `src/vidgen/` — Core library: contracts, DB models, storage, review, telemetry, costs, providers
- `services/` — One package per pipeline stage (analysis, script, narration, storyboard, image_generation, animation, qa, render_execution, control_plane, etc.)
- `packages/workflows/` — Temporal workflow definitions (project.py, shot.py, activities.py)
- `packages/providers/` — Provider protocols and deterministic fakes
- `apps/api/` — FastAPI control plane (`/api/v1` routes)
- `apps/web/` — React review UI
- `workers/temporal_worker/` — Activity implementations (production_handlers.py)
- `workers/control_dispatcher/` — Async command processing

### Key patterns

**Contracts**: All inter-stage payloads are versioned Pydantic v2 models in `src/vidgen/contracts/`. Every model has `schema_version`. Run `make schemas` after changes to export JSON Schema + TypeScript.

**Provider abstraction**: Every external service has a Protocol in `packages/providers/`, a real implementation in `services/*/provider.py`, and a deterministic fake in `services/*/fake_provider.py`. Tests always use fakes — no paid calls.

**Settings hierarchy**: Deployment defaults in `apps/api/settings.py` (`APISettings`), per-project overrides in `ProjectGenerationSettings` (`src/vidgen/contracts/generation.py`). Projects inherit deployment defaults when unset. The `effective_*` functions in `services/generation/settings.py` merge them.

**Warn-only codes**: Each pipeline stage has configurable validation codes that can be demoted to warnings. Defined as eligible sets in contracts, configured at deployment and project level, applied by validators at runtime.

**Identity hashing**: Shot workflow IDs are derived from deterministic material hashes (project, storyboard, shot, configuration, pipeline versions). See `services/review/shot_identity.py` and `packages/workflows/shot_policy.py`.

**Row versioning**: DB rows use integer `version` for optimistic concurrency. API uses `ETag`/`If-Match` headers.

**Content-addressed blobs**: Assets are stored by SHA-256, never overwritten. Full provenance (parent IDs, provider request IDs) is persisted.

### Invariants

1. Blob objects are content-addressed by SHA-256 and never overwritten
2. Every activity is idempotent for (operation, input_hash, contract_version, provider_config_version)
3. A shot child workflow owns exactly one shot_id
4. The render manifest is the sole input to FFmpeg — it never queries "latest" assets
5. Agent responses are rejected unless they pass JSON Schema, Pydantic, FK, timing, and semantic validation

## Contributor Rules (from AGENTS.md)

- Read `docs/TECHNICAL_DESIGN.md` before planning changes
- All inter-stage payloads are versioned Pydantic contracts
- Every worker/provider call accepts an idempotency key
- Persist content hashes, provider request IDs, parent asset IDs
- No paid calls in tests — use deterministic fakes
- Migrations must upgrade from empty DB and downgrade cleanly
- All timestamps UTC, all primary keys UUID
