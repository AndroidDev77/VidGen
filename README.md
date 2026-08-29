# VidGen

VidGen turns a long-form source video into an animated comedy recap. It ingests an episode,
acquires a transcript, analyzes the episode, writes and narrates a comedy script, directs a
deterministically timed storyboard, generates and QA-gates every shot, assembles a captioned
render, and inspects that render as a whole before a project can be called complete.

This README is the working guide: install it, run it locally, test it, and fix it when it breaks.

- Architecture and the full T01-T26 roadmap: [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md)
- What each roadmap task delivered: [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md)
- Contributor rules: [`AGENTS.md`](AGENTS.md)

## What VidGen does

A project moves through one durable Temporal workflow. Each stage is restartable, addresses its
work by an idempotency key, and records provenance for everything it produces:

1. **Upload** an owner-scoped, resumable source video.
2. **Media processing** probes the container, extracts audio, detects scenes, and extracts frames.
3. **Transcript acquisition** prefers embedded or sidecar subtitles, then OpenSubtitles, and falls
   back to audio transcription with speaker diarization.
4. **Evidence** assembles per-scene evidence packages from the transcript and frames.
5. **Episode analysis** produces the restartable structural read of the episode.
6. **Script generation** compresses the plot and writes, reviews and revises a comedy script.
7. **Narration** generates measured audio per script segment, normalizes it to 48 kHz mono PCM,
   force-aligns it, and gates it on quality.
8. **Storyboard** direction retimes shots to exact integer microseconds against the configured
   visual-provider capability profile.
9. **Per-shot generation** fans out one child workflow per shot: keyframe, semantic keyframe QA,
   image-to-video animation, semantic video QA, and bounded repair or fallback routing.
10. **Render** assembles the approved shots, narration and captions into the delivery.
11. **Final editorial QA** inspects the assembled render as a whole and owns the completion gate.

AI reasoning stays behind provider interfaces, orchestration stays deterministic, and every
generated asset is content-addressed with its parents, hashes and provider request IDs.

## Current capabilities

Roadmap tasks T01 through T23 are implemented. The per-task detail lives in
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md); in short:

- Versioned Pydantic contracts with exported JSON Schema, a PostgreSQL data model with Alembic
  migrations, and content-addressed asset storage with provenance.
- Deterministic fake AI and media providers for every paid provider, so the whole pipeline can run
  offline with no credential.
- Owner-scoped resumable uploads and deterministic FFmpeg probing, audio extraction, scene
  detection and frame extraction.
- Restartable transcription, subtitle-first transcript acquisition, episode analysis, comedy script
  generation and provider-neutral narration.
- Deterministic storyboard retiming, keyframe generation, Runway image-to-video generation and
  per-shot child workflows.
- Semantic visual QA with a versioned rubric, bounded repair and fallback routing including a Google
  Veo alternate provider and a free 2.5D parallax fallback.
- Deterministic captioned rendering, final editorial QA with a workflow-enforced completion gate,
  a customer-facing React review application, and a cost ledger with budget caps.

Two gaps affect what you can run locally today, and both are called out again where they bite:

- **No supported command executes a render.** `POST /render:start` and
  `scripts/render_project.py` create or queue the render job row; the T17 pipeline that would
  execute it (`services/renderer/pipeline.py`) is driven only from tests. A local run therefore
  stops before the final MP4, and final editorial QA has no current render to inspect.
- **Telemetry is not wired into the running services.** The T23 helpers exist in
  `src/vidgen/telemetry`, but neither `apps/api/main.py` nor the Temporal worker calls them, so a
  local Grafana shows no application metrics.

## Architecture summary

| Path | What lives there |
| --- | --- |
| `src/vidgen/contracts` | Versioned Pydantic contracts exchanged between stages |
| `src/vidgen/db` | SQLAlchemy models, repositories and projections |
| `src/vidgen/storage` | Content-addressed blob storage and the asset service |
| `src/vidgen/review` | Row versions, idempotency, project events and workflow control |
| `src/vidgen/telemetry` | Structured logging, tracing, metrics and cost instrumentation |
| `services/*` | One package per pipeline stage, each with a provider boundary and a fake |
| `packages/workflows` | Temporal workflow and activity definitions |
| `packages/providers` | Provider protocols and the deterministic fakes |
| `packages/contracts`, `packages/ts-contracts` | Exported JSON Schema and its TypeScript mirror |
| `workers/temporal_worker` | The Temporal worker process and its production handlers |
| `apps/api` | The FastAPI control plane (`/api/v1`) |
| `apps/web` | The React review application (`@vidgen/web`) |
| `scripts/` | Per-stage CLIs for running and inspecting one stage at a time |
| `migrations/` | Alembic migrations |

Three processes make up a full local system: the **API** (`apps/api`), the **web application**
(`apps/web`), and the **Temporal worker** (`workers/temporal_worker`). Docker Compose supplies
PostgreSQL, Redis, Azurite and Temporal.

## Prerequisites

| Tool | Tested version | Verify with |
| --- | --- | --- |
| Docker Desktop / Docker Engine with Compose v2 | Any release with `docker compose` | `docker --version && docker compose version` |
| Python | 3.12 (`requires-python = ">=3.12"`, images use `python:3.12-slim`) | `python3 --version` |
| `uv` | 0.11.33 (pinned in CI and in the Dockerfile) | `uv --version` |
| Node.js | 22 LTS or newer (CI runs Node 24) | `node --version` |
| pnpm | 11.19.0 (pinned by `packageManager` in `package.json`) | `pnpm --version` |
| FFmpeg | 6 or newer (verified against 6.1.1) | `ffmpeg -version` |
| ffprobe | ships with FFmpeg; must be the same build | `ffprobe -version` |
| Git | 2.40 or newer | `git --version` |

All eight at once:

```bash
docker --version && docker compose version
python3 --version
uv --version
node --version
pnpm --version
ffmpeg -version | head -1
ffprobe -version | head -1
git --version
```

`uv` manages the Python toolchain itself, so a system Python older than 3.12 is fine as long as
`uv` can fetch 3.12. FFmpeg and ffprobe are not optional: media processing, narration
normalization, rendering and QA all shell out to them.

## Quick local setup

One time, from a clean checkout:

```bash
cp .env.example .env
cp apps/web/.env.example apps/web/.env

uv sync --all-groups
pnpm install

make infra-up
make migrate
```

`make infra-up` starts four containers; the Temporal dev server also serves its web UI:

| Service | Address | Purpose |
| --- | --- | --- |
| PostgreSQL 16 | `localhost:5432` | Canonical relational state (`vidgen` / `vidgen` / `vidgen`) |
| Redis 7 | `localhost:6379` | Cache, rate limiting and ephemeral semaphores |
| Azurite | `localhost:10000` | Azure Blob emulator |
| Temporal (dev server) | `localhost:7233` | Durable workflow execution |
| Temporal UI | <http://localhost:8233> | Workflow history and queries |

`make migrate` runs `uv run alembic upgrade head` against
`postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen`.

Confirm the stack is healthy — this checks PostgreSQL and runs Alembic up, down and up again
against the local (disposable) database:

```bash
make verify-stack
```

### Make targets

| Target | What it does |
| --- | --- |
| `make install` | `uv sync --all-groups` |
| `make infra-up` | Start PostgreSQL, Redis, Azurite and Temporal |
| `make infra-logs` | Follow the infrastructure container logs |
| `make migrate` | `alembic upgrade head` |
| `make run-worker` | Run the Temporal worker |
| `make run-api` | Run the API on port 8000 with reload |
| `make run-web` | Run the Vite dev server on port 5173 |
| `make observability-up` | Start the collector, Prometheus and Grafana |
| `make observability-logs` | Follow the collector logs |
| `make verify` | Lint, typecheck, test and export schemas |
| `make verify-web` | Lint, typecheck, test and build the web application |
| `make verify-stack` | PostgreSQL reachability plus an Alembic up/down/up check |
| `make infra-down` | Stop the containers, keeping the volumes |
| `make local-reset` | **Destructive.** Delete the local database, blob store and uploads |

## UI-only development mode

The default `.env` sets `VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER=true`. In that mode the API
does not talk to Temporal at all: `workflow:start` records a deterministic fake run so the review
UI has something to render. **No pipeline stage executes.** This is the right mode for frontend and
control-plane work, and the wrong mode for anything that must actually produce media.

Two terminals:

```bash
# Terminal 1
make run-api

# Terminal 2
make run-web
```

| Surface | URL |
| --- | --- |
| Web application | <http://localhost:5173> |
| API documentation (Swagger UI) | <http://localhost:8000/docs> |
| API health | <http://localhost:8000/healthz> |

The Vite dev server proxies `/api` to `http://127.0.0.1:8000` (override with the
`VIDGEN_API_PROXY_TARGET` environment variable), so the browser stays same-origin and the API keeps
CORS disabled. The development identity is sent as the `X-VidGen-User` header
(`VITE_VIDGEN_DEV_USER`, default `local-user`); T18 deliberately implements no production
authentication.

### Fake workflow controller vs. fake AI providers

These are two independent switches and confusing them is the most common local mistake:

| Setting | What it fakes | What still runs |
| --- | --- | --- |
| `VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER=true` | The **orchestrator**. The API invents workflow status instead of starting a Temporal workflow. | Nothing. No stage, no activity, no media. |
| `VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS=true` | The **paid AI providers**. Deterministic synthetic images, video, narration, transcription and QA. | Everything else: Temporal, PostgreSQL, FFmpeg, persistence, QA logic and the workflow code. |

A full local pipeline wants the first `false` and the second `true`.

## Full local pipeline with fake providers

Set these in `.env`:

```text
VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER=false
VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS=true
VIDGEN_IMAGE_PROVIDER_NAME=fake
VIDGEN_VIDEO_PROVIDER_NAME=fake
VIDGEN_REPAIR_ALTERNATE_PROVIDER=none
```

Leave `VIDGEN_OPENAI_API_KEY` and `VIDGEN_RUNWAY_API_SECRET` empty. A set credential always wins
over the fake provider (see the warning under
[Full local pipeline with production providers](#full-local-pipeline-with-production-providers)).

`VIDGEN_IMAGE_PROVIDER_NAME` and `VIDGEN_VIDEO_PROVIDER_NAME` do not choose the provider — the
credentials do. They are the provider names bound into each shot's child-workflow identity, so
setting them to `fake` keeps the workflow IDs that the review UI addresses consistent with what the
worker actually ran.

Three processes:

```bash
# Terminal 1
uv run python -m workers.temporal_worker.main

# Terminal 2
make run-api

# Terminal 3
make run-web
```

`make run-worker` is the same command as Terminal 1.

Fake-provider mode:

- Uses the real PostgreSQL, Temporal, FFmpeg, rendering, persistence, QA and workflow code.
- Makes no paid provider request.
- Requires no OpenAI, Runway, Google, ElevenLabs or OpenSubtitles credential.
- Produces deterministic synthetic outputs, so a repeated run reuses work instead of redoing it.

### Local voice-profile bootstrap

The narration stage resolves its voice from `project.settings["voice_profile_id"]`, and project
creation stores empty settings. A freshly created project therefore fails narration with
`project settings require a valid voice_profile_id` until a profile is selected.

```bash
uv run python scripts/create_local_voice_profile.py PROJECT_UUID --provider fake
```

The command verifies the project exists, creates or reuses one deterministic project-scoped fake
voice profile, selects it in the project's settings, and prints its ID:

```text
project_id=e99109b0-cf64-4f1d-a3d5-35efe1ab39ec
voice_profile_id=4838964e-ad0f-5f10-9b7c-f0d24224e1b1
provider=fake
action=created
```

`action` reports what happened: `created` (a new profile), `assigned` (an existing local profile
re-selected), `unchanged` (the project already resolves to a usable profile — including a
production one, which is never overwritten), or `repaired` (the selected ID did not resolve to a
usable profile, so the local fake one replaced it). Re-running the command is safe: it is
idempotent, needs no paid credential, and the profile ID is derived from the project ID, so it is
stable across runs and across a recreated database.

The command refuses any `--provider` other than `fake`; a production voice profile must be selected
deliberately. Keep the command for inspection and repair even if project creation later assigns a
default fake profile automatically in local fake-provider mode.

### What a local fake-provider run does and does not reach

The workflow runs upload, media processing, transcript acquisition, evidence, episode analysis,
script generation, narration, storyboard, and the per-shot fan-out (keyframe, keyframe QA,
animation, video QA, repair) against real infrastructure with synthetic provider output.

It does **not** reach a finished delivery. No supported command executes the T17 render:
`POST /projects/{id}/render:start` and `scripts/render_project.py` only create the render job row,
and nothing consumes it. Because final editorial QA inspects an assembled render, T22 has nothing
current to inspect and the project cannot reach `completed` locally. Do not expect a downloadable
MP4 from a local run today.

## Full local pipeline with production providers

Production mode is the same three processes with real credentials. Set the keys in `.env`; values
are never committed.

| Variable | Stages that use it | Required? |
| --- | --- | --- |
| `VIDGEN_OPENAI_API_KEY` | Transcription and diarization, episode analysis, script compression/writing/editing, narration and forced alignment, storyboard direction, keyframe image generation, semantic visual QA (T20), final editorial QA (T22) | Required for a production run |
| `VIDGEN_RUNWAY_API_SECRET` | Image-to-video shot animation (T15/T16) | Required for a production run |
| `VIDGEN_GOOGLE_CLOUD_PROJECT`, `VIDGEN_GOOGLE_ACCESS_TOKEN`, `VIDGEN_VEO_MODEL`, `VIDGEN_VEO_LOCATION` | The Google Veo alternate-provider repair attempt (T21) | Optional; only when `VIDGEN_REPAIR_ALTERNATE_PROVIDER=veo` |
| `VIDGEN_ELEVENLABS_API_KEY` | `scripts/generate_narration.py --provider elevenlabs` only | Optional; the worker's narration stage uses OpenAI or the fake provider |
| `VIDGEN_OPENSUBTITLES_API_KEY`, `VIDGEN_OPENSUBTITLES_USERNAME`, `VIDGEN_OPENSUBTITLES_PASSWORD` | External subtitle search during transcript acquisition | Optional; without it, acquisition uses embedded/sidecar subtitles or audio transcription |

Enablement flags, all safe to leave at their defaults:

```text
VIDGEN_REPAIR_ALTERNATE_PROVIDER=none        # set to "veo" to enable the Google Veo repair route
VIDGEN_SUBTITLE_SYNC_ENABLED=false           # external subtitle search stays off unless enabled
VIDGEN_FINAL_QA_ADJUDICATION_ENABLED=true    # the bounded T22 second opinion
VIDGEN_REPAIR_ALLOW_PARALLAX_FALLBACK=true   # the free deterministic 2.5D fallback
```

There is no VoiceStudio integration in this repository. The optional third-party voice provider is
ElevenLabs, and only through `scripts/generate_narration.py`.

> **Warning — a set credential outranks the fake provider.** The worker chooses its provider by
> credential presence, not by `VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS`. If `VIDGEN_OPENAI_API_KEY` or
> `VIDGEN_RUNWAY_API_SECRET` is set, the worker uses OpenAI or Runway **even with
> `VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS=true`**, and those calls cost money. To stay offline, leave
> both empty. `VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS` only decides what happens when a credential is
> absent: `true` falls back to the deterministic fake, `false` fails the stage loudly.

Every project carries a T23 hard budget cap, and repairs can additionally be capped per shot with
`VIDGEN_REPAIR_PER_SHOT_COST_LIMIT`. Set a project budget before a production run.

## Required environment variables

`.env` (loaded by `apps/api/settings.py`, prefix `VIDGEN_`) — the ones that must be right locally:

| Variable | Local value |
| --- | --- |
| `VIDGEN_ENV` | `local` |
| `VIDGEN_DATABASE_URL` | `postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen` |
| `VIDGEN_REDIS_URL` | `redis://localhost:6379/0` |
| `VIDGEN_BLOB_BACKEND` / `VIDGEN_BLOB_ROOT` | `filesystem` / `.local-data/blobs` |
| `VIDGEN_UPLOAD_ROOT` | `.local-data/uploads` |
| `VIDGEN_SIGNING_SECRET` | any non-empty local secret |
| `VIDGEN_TEMPORAL_TARGET_HOST` / `VIDGEN_TEMPORAL_NAMESPACE` | `localhost:7233` / `default` |
| `VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER` | `true` for UI-only, `false` for the real pipeline |
| `VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS` | `true` for an offline pipeline |
| `VIDGEN_IMAGE_PROVIDER_NAME` / `VIDGEN_VIDEO_PROVIDER_NAME` | `fake` / `fake` offline, `openai` / `runway` in production |
| `VIDGEN_CORS_ALLOWED_ORIGINS` | empty; the Vite proxy keeps the browser same-origin |
| `AZURE_STORAGE_CONNECTION_STRING` | `UseDevelopmentStorage=true` for Azurite |

The Temporal worker additionally reads plain (unprefixed) variables: `TEMPORAL_ADDRESS`
(default `localhost:7233`), `TEMPORAL_TASK_QUEUE` (default `vidgen-projects`) and
`TEMPORAL_ACTIVITY_THREADS` (default `4`). The defaults match `make infra-up`.

`apps/web/.env`:

| Variable | Local value |
| --- | --- |
| `VITE_VIDGEN_API_BASE_URL` | empty, so requests go through the Vite dev proxy |
| `VITE_VIDGEN_DEV_USER` | `local-user` |

`.env.example` and `apps/web/.env.example` document every variable, including the optional ones.

## Local service URLs

| Surface | URL |
| --- | --- |
| Web application | <http://localhost:5173> |
| API | <http://localhost:8000> |
| API documentation | <http://localhost:8000/docs> |
| API health | <http://localhost:8000/healthz> |
| Temporal gRPC | `localhost:7233` |
| Temporal UI | <http://localhost:8233> |
| PostgreSQL | `localhost:5432` (`vidgen` / `vidgen` / `vidgen`) |
| Redis | `localhost:6379` |
| Azurite blob | <http://localhost:10000> |
| Grafana (observability profile) | <http://localhost:3000> |
| Prometheus (observability profile) | <http://localhost:9090> |

## Creating and processing a project

Everything below is also available in the web application; the HTTP calls are shown so the flow is
reproducible from a terminal. `X-VidGen-User` is the local development identity.

The fastest path — `scripts/upload_video.py` creates a project and performs the whole resumable
upload against a running API, printing both JSON bodies:

```bash
uv run python scripts/upload_video.py sample.mp4 --name "Local run"
```

The steps below do the same thing one call at a time.

**1. Create the project.**

```bash
curl -sS -X POST http://localhost:8000/api/v1/projects \
  -H 'Content-Type: application/json' \
  -H 'X-VidGen-User: local-user' \
  -d '{"name":"Local run","target_duration_seconds":120,"visual_style":"flat editorial cartoon","humor_intensity":5}'
```

Keep the returned `id` as `PROJECT_ID`.

**2. Create the local fake voice profile.**

```bash
uv run python scripts/create_local_voice_profile.py "$PROJECT_ID" --provider fake
```

**3. Make synthetic source media** (no real episode needed):

```bash
ffmpeg -f lavfi -i "testsrc=size=640x360:rate=30:duration=20" \
       -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=20" \
       -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest sample.mp4
```

**4. Upload it.** Parts are numbered from `0`, and every part except the last must be exactly
`part_size` bytes.

```bash
SIZE=$(stat -c%s sample.mp4)
SHA=$(sha256sum sample.mp4 | cut -d' ' -f1)

UPLOAD_ID=$(curl -sS -X POST "http://localhost:8000/api/v1/projects/$PROJECT_ID/uploads" \
  -H 'Content-Type: application/json' -H 'X-VidGen-User: local-user' \
  -d "{\"filename\":\"sample.mp4\",\"media_type\":\"video/mp4\",\"expected_size\":$SIZE,\"expected_sha256\":\"$SHA\",\"part_size\":8388608}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -sS -X PUT "http://localhost:8000/api/v1/uploads/$UPLOAD_ID/parts/0" \
  -H 'X-VidGen-User: local-user' -H 'Content-Type: application/octet-stream' \
  --data-binary @sample.mp4

curl -sS -X POST "http://localhost:8000/api/v1/uploads/$UPLOAD_ID/complete" \
  -H 'X-VidGen-User: local-user'
```

**5. Start the workflow.** An `Idempotency-Key` header is required; a retried request adopts the
existing run rather than starting a second workflow.

```bash
curl -sS -X POST "http://localhost:8000/api/v1/projects/$PROJECT_ID/workflow:start" \
  -H 'Content-Type: application/json' -H 'X-VidGen-User: local-user' \
  -H "Idempotency-Key: start-$PROJECT_ID" -d '{}'
```

With the fake workflow controller this only records a synthetic run. With
`VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER=false` and the worker running, this starts the real
`ProjectWorkflow`.

**6. Watch it.**

```bash
curl -sS "http://localhost:8000/api/v1/projects/$PROJECT_ID/workflow" -H 'X-VidGen-User: local-user'
curl -sS "http://localhost:8000/api/v1/projects/$PROJECT_ID/events"   -H 'X-VidGen-User: local-user'

uv run python scripts/project_workflow.py inspect "$PROJECT_ID"
```

The Temporal UI at <http://localhost:8233> shows the workflow history, the per-shot child
workflows, and any activity failure with its stack.

**7. Review, render and download.** The review application drives transcript and script edits,
storyboard and shot review, `render:start`, approval and asset downloads
(`GET /api/v1/projects/{id}/render`, `GET /api/v1/assets/{id}/download-url`). As noted above, `render:start`
only queues the render job locally — nothing executes it — so the download step is not reachable in
a local run today.

## Running individual pipeline stages

Each stage has a CLI in `scripts/`, all reading `VIDGEN_DATABASE_URL` and `VIDGEN_BLOB_ROOT` from
the environment. They are the fastest way to work on one stage without the workflow:

```bash
uv run python scripts/process_media.py PROJECT_ID
uv run python scripts/acquire_transcript.py PROJECT_ID
uv run python scripts/transcribe_media.py PROJECT_ID
uv run python scripts/build_references.py PROJECT_ID
uv run python scripts/analyze_episode.py PROJECT_ID
uv run python scripts/generate_script.py PROJECT_ID
uv run python scripts/generate_narration.py PROJECT_ID --provider fake --voice-profile-id VOICE_ID
uv run python scripts/generate_storyboard.py PROJECT_ID
uv run python scripts/generate_keyframes.py PROJECT_ID
uv run python scripts/generate_shot_videos.py PROJECT_ID
uv run python scripts/run_shot_workflow.py PROJECT_ID
uv run python scripts/run_visual_qa.py PROJECT_ID
uv run python scripts/run_visual_repair.py PROJECT_ID
uv run python scripts/render_project.py PROJECT_ID
uv run python scripts/run_final_editorial_qa.py PROJECT_ID
```

Inspection-only commands that read persisted state without touching a provider:

```bash
uv run python scripts/inspect_references.py PROJECT_ID
uv run python scripts/inspect_shot_workflow.py PROJECT_ID --storyboard-run-id STORYBOARD_RUN_ID
uv run python scripts/inspect_visual_qa.py PROJECT_ID
uv run python scripts/inspect_render.py PROJECT_ID
uv run python scripts/cost_report.py PROJECT_ID
```

Every stage CLI that talks to a provider defaults to `--provider fake`, so the commands above are
offline and free until you pass `--provider openai` or `--provider runway`.

Every command takes `--help`; the exact flags for each stage, and the per-stage local commands and
their caveats, are documented in
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

## Running tests

```bash
make verify        # ruff, mypy --strict, pytest with coverage, JSON Schema export
make verify-web    # eslint, tsc, vitest, and the web production build
```

Individually:

```bash
make lint
make typecheck
make test
make schemas

make web-lint
make web-typecheck
make web-test
make web-build
make web-e2e       # Playwright; run `pnpm --filter @vidgen/web exec playwright install chromium` first
```

A single test file or test:

```bash
uv run pytest tests/test_local_voice_profile.py -q
uv run pytest -k voice_profile -q
```

Tests never make a paid provider call: they run against SQLite, synthetic media and the
deterministic fakes. Two things they do need locally:

- **FFmpeg and ffprobe**, for the media, narration, render and QA suites.
- **Network access to `temporal.download`**, for the handful of suites that start Temporal's
  time-skipping test server (`tests/test_workflow_runtime.py`, `tests/test_t22_workflow.py`,
  `tests/test_shot_workflow_t16.py`, `tests/test_repair_workflow_t21.py`). Behind a proxy that
  blocks it, those tests fail with `Failed starting test server`; deselect them with
  `uv run pytest -q --deselect tests/test_workflow_runtime.py` (and so on) to run the rest.

The migration check that CI runs, against the local disposable database:

```bash
make verify-stack
```

## Observability

```bash
make observability-up
```

| Surface | URL |
| --- | --- |
| Grafana | <http://localhost:3000> (default login `admin` / `admin`) |
| Prometheus | <http://localhost:9090> |
| Temporal UI | <http://localhost:8233> |

The OpenTelemetry Collector listens on `4317` (gRPC) and `4318` (HTTP) and exposes a Prometheus
endpoint on `8889`, which Prometheus scrapes. Collector logs:

```bash
docker compose --profile observability logs -f otel-collector
```

`make observability-logs` runs the same command. Prometheus alert rules are in
`observability/alerts.yml`, and the provisioned dashboard is
`observability/grafana/dashboards/vidgen-operations.json`.

**What actually reports today:** the Temporal UI is the useful local view. The T23 telemetry
helpers in `src/vidgen/telemetry` are not yet initialized by `apps/api/main.py` or by the Temporal
worker, and `VIDGEN_OTEL_EXPORTER_OTLP_ENDPOINT` is read but not wired to an exporter, so the
Grafana dashboard stays empty locally. Cost and budget data is available through the API
(`GET /api/v1/projects/{id}/costs`) and `scripts/cost_report.py`.

## Stopping and resetting the environment

Stop the three processes with Ctrl+C, then stop the containers — this keeps the PostgreSQL and
Azurite volumes, so your projects survive:

```bash
make infra-down
```

### Destructive reset

`make local-reset` **permanently deletes local data**. It is never run by another target, and it
prompts for confirmation before doing anything. It removes exactly:

- the `postgres-data` Docker volume — the entire local PostgreSQL database,
- the `azurite-data` Docker volume — the local Azurite blob store,
- `.local-data/uploads` (`VIDGEN_UPLOAD_ROOT`) — resumable source-video uploads,
- `.local-data/blobs` (`VIDGEN_BLOB_ROOT`) — every content-addressed asset.

Nothing outside those four locations is touched. Use it only for disposable local data.

```bash
make local-reset
# then rebuild an empty environment:
make infra-up
make migrate
```

## Troubleshooting

**Docker unavailable.** `make infra-up` fails with `Cannot connect to the Docker daemon`. Start
Docker Desktop, or on Linux `sudo systemctl start docker`. Verify with `docker info`. If
`docker compose` is not recognized, you are on Compose v1; install the Compose v2 plugin.

**PostgreSQL not ready.** `make migrate` fails with `connection refused` or
`the database system is starting up`. The container needs a few seconds after `make infra-up`:

```bash
docker compose ps postgres
until docker compose exec -T postgres pg_isready -U vidgen -d vidgen; do sleep 2; done
```

**Alembic migration failure.** Read the actual error first: `uv run alembic upgrade head`. A
`relation already exists` on a half-migrated local database is fastest to fix with `make local-reset`
followed by `make infra-up && make migrate`. Confirm which revision you are on with
`uv run alembic current`.

**More than one Alembic head.** `uv run alembic upgrade head` fails with
`Multiple head revisions are present`. List them:

```bash
uv run alembic heads
uv run alembic history --verbose | head -40
```

Two branches were created in parallel. Merge them rather than deleting a migration:

```bash
uv run alembic merge -m "merge heads" HEAD_A HEAD_B
uv run alembic upgrade head
```

**Temporal worker not connected.** `workflow:start` succeeds but the project never leaves
`ingesting`, and the Temporal UI shows the workflow with no worker polling. Check that the worker
process is running and on the right queue — it defaults to `vidgen-projects` at `localhost:7233`,
matching `make infra-up`:

```bash
docker compose ps temporal
uv run python -m workers.temporal_worker.main   # watch for a connection error on startup
```

A worker that exits immediately with `provider is not configured` is the fake-provider problem
below.

**Fake workflow controller accidentally enabled.** The UI shows progress and completed stages, but
no worker activity appears in the Temporal UI and no assets are written. Check:

```bash
grep VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER .env
```

It must be `false` for a real run. Restart the API after changing it — settings are cached per
process.

**Fake providers not enabled.** The worker fails a stage with
`... provider is not configured` (script generation, narration, image generation, video generation,
visual QA or final QA). With no credential set, the worker needs explicit permission to use the
fakes:

```text
VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS=true
```

Restart the worker after changing it. The reverse symptom — an unexpected provider bill or a
`401` from OpenAI or Runway — means a credential is set and outranks the fake provider; empty
`VIDGEN_OPENAI_API_KEY` and `VIDGEN_RUNWAY_API_SECRET`.

**Missing voice profile.** The narration activity fails with
`project settings require a valid voice_profile_id`. Run the bootstrap and restart the stage:

```bash
uv run python scripts/create_local_voice_profile.py "$PROJECT_ID" --provider fake
```

If it prints `action=unchanged` and narration still fails, the project points at a profile that
exists but belongs to another project or provider; inspect
`SELECT settings FROM projects WHERE id = '...'` and clear the key to let the bootstrap repair it.

**Missing FFmpeg or ffprobe.** Media processing, narration normalization, rendering and several
test suites fail with `No such file or directory: 'ffmpeg'` or `'ffprobe'`. Install both from the
same build (`brew install ffmpeg`, `sudo apt-get install ffmpeg`), then confirm:

```bash
ffmpeg -version | head -1 && ffprobe -version | head -1
```

**Port conflicts.** `make infra-up` fails with `port is already allocated`, or `make run-api` with
`address already in use`. Find the holder and stop it:

```bash
lsof -i :5432 -i :6379 -i :7233 -i :8000 -i :8233 -i :10000 -i :5173
```

A previous VidGen stack is the usual culprit: `make infra-down`. A system PostgreSQL on 5432 is the
other; stop it, or remap the container port in `docker-compose.yml` and update
`VIDGEN_DATABASE_URL`.

**Node or pnpm version mismatch.** `pnpm install` reports that the project's `packageManager`
(`pnpm@11.19.0`) does not match your pnpm, or Vite fails on unsupported syntax. Use Corepack:

```bash
corepack enable
corepack prepare pnpm@11.19.0 --activate
node --version   # 22 or newer
```

Then `rm -rf node_modules apps/web/node_modules && pnpm install`.

**Missing provider credentials.** In production mode a stage fails with a provider authentication
error or `... provider is not configured`. Confirm the key is in `.env` (not only in your shell),
that the API and worker were restarted after the change, and that the key matches the stage — see
the table in [Full local pipeline with production providers](#full-local-pipeline-with-production-providers).
To keep working offline instead, clear the credentials and set
`VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS=true`.

**Stale render or QA lineage.** Final editorial QA fails with a stale-lineage error, or a render
refuses to start, after an upstream edit. This is deliberate: T22 rejects a report for a render that
is no longer current before buying any analysis, and edited transcripts, scripts, storyboards or
shots mark downstream outputs stale. Regenerate in order — the affected shots, then the render, then
final QA:

```bash
uv run python scripts/inspect_render.py "$PROJECT_ID"     # what the render currently binds
uv run python scripts/inspect_shot_workflow.py "$PROJECT_ID" --storyboard-run-id "$STORYBOARD_RUN_ID"
```

**Browser cannot reach the API.** The web application shows network errors or an empty project
list. Check, in order: the API is up (<http://localhost:8000/healthz>); the Vite dev server is
running on 5173; `VITE_VIDGEN_API_BASE_URL` in `apps/web/.env` is **empty**, so the browser uses the
same-origin `/api` proxy. If the API listens somewhere other than `127.0.0.1:8000`, point the proxy
at it with `VIDGEN_API_PROXY_TARGET` when starting the dev server. If you deliberately point the browser at a different origin, you must also
set `VIDGEN_CORS_ALLOWED_ORIGINS=http://localhost:5173` in `.env` and restart the API — CORS is off
by default.

## Azure deployment

There is no deployment automation in this repository yet: adding Azure infrastructure is roadmap
task T24, and no `infra/` directory exists. What does exist:

- The two images CI builds on every run: the API/worker image (root `Dockerfile`, `python:3.12-slim`
  plus FFmpeg, served by uvicorn on 8000) and the web image (`apps/web/Dockerfile`, nginx serving
  the built assets and reverse-proxying `/api` to `VIDGEN_API_UPSTREAM`).
- The target topology — Front Door and WAF, the React app, FastAPI on Container Apps, Temporal
  Cloud, PostgreSQL Flexible Server, Blob Storage, Managed Redis, Service Bus, Key Vault through
  managed identity, and Container Apps Jobs for isolated FFmpeg renders — specified under
  "Recommended Azure deployment" in [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md).

Until T24 lands, treat that section of the design document as the deployment plan, not as
something you can apply.

## Additional documentation

| Document | What it covers |
| --- | --- |
| [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) | Authoritative architecture, data model, stage specifications, and the T01-T26 roadmap |
| [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) | What each implemented roadmap task delivered, with its per-stage local commands |
| [`AGENTS.md`](AGENTS.md) | Contributor and coding-agent rules |
| `.env.example`, `apps/web/.env.example` | Every environment variable with its local default |
| `packages/contracts/schema` | Exported JSON Schema for the inter-stage contracts (`make schemas`) |
| <http://localhost:8000/docs> | Generated OpenAPI documentation for the running API |
