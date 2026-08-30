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

Roadmap tasks T01 through T23 are implemented, T24 Azure infrastructure is implemented and
reviewed but not yet validated by a real staging deployment, and T25 YouTube publication is
implemented against a deterministic fake provider and a mocked production adapter. The per-task detail lives in
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
- Bicep infrastructure for a private-by-default Azure staging environment, with an Azure Blob
  storage backend selected by `VIDGEN_BLOB_BACKEND` and telemetry that exports to Application
  Insights (see [Azure deployment](#azure-deployment)).
- Restartable YouTube publication: OAuth 2.0 with PKCE, envelope-encrypted refresh credentials, a
  resumable upload that resumes from YouTube's own confirmed byte offset, captions, thumbnails, and
  an explicit visibility transition (see [Publishing to YouTube](#publishing-to-youtube)).
- A durable control plane (T18b): every asynchronous product command - reference builds and
  approvals, shot regeneration and retry, human-review continuation, transcript and script rebuilds,
  manual final QA, remediation, project continuation - is persisted before it is accepted and
  started by a worker that records the workflow it actually ran
  (see [The control plane](#the-control-plane-what-accepted-means)).
- Product-level narration voice selection, so a project created in the browser is startable without
  a manual database repair (see [Selecting a narration voice](#selecting-a-narration-voice)).

Two gaps still affect what you can observe locally, and both are called out again where they bite:

- **No metrics reach a local Grafana.** The API and the worker do initialize the T23 telemetry
  stack, but the bootstrap exports logs (stdout JSON) and traces only. Nothing exports metrics over
  OTLP, so the provisioned dashboard stays empty locally.
- **The control dispatcher is a separate process.** Without it running, commands are accepted and
  stay `pending` - truthfully, and visibly, but nothing progresses.

Confirmed gaps that are deliberately outside the implemented tasks - runtime telemetry wiring,
production authentication, publication metadata generation, audio post-production - are recorded
with evidence in [`docs/ROADMAP_GAPS.md`](docs/ROADMAP_GAPS.md).

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
| `infra/` | Bicep templates, deployment scripts and policy tests for the Azure environment |

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
| `make install` | `uv sync --all-groups --all-extras` (adds the optional Azure SDKs) |
| `make infra-up` | Start PostgreSQL, Redis, Azurite and Temporal |
| `make infra-validate` | Compile and policy-check the Bicep templates (no Azure credential) |
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

Four processes:

```bash
# Terminal 1
uv run python -m workers.temporal_worker.main

# Terminal 2 - the T18b control-command dispatcher
uv run python -m workers.control_dispatcher.main

# Terminal 3
make run-api

# Terminal 4
make run-web
```

`make run-worker` is the same command as Terminal 1, and `make run-control-dispatcher` is
Terminal 2.

The dispatcher is not optional. Every asynchronous product command - building continuity
references, approving them, regenerating a shot, retrying a repair, rebuilding after a transcript
or script revision, running final QA by hand, routing a T22 remediation, continuing a paused
project - is written to the `control_commands` table by the API and started by this process. The
API stays truthful without it (a command sits `pending` and says so), but nothing progresses.

Fake-provider mode:

- Uses the real PostgreSQL, Temporal, FFmpeg, rendering, persistence, QA and workflow code.
- Makes no paid provider request.
- Requires no OpenAI, Runway, Google, ElevenLabs or OpenSubtitles credential.
- Produces deterministic synthetic outputs, so a repeated run reuses work instead of redoing it.

### Selecting a narration voice

The narration stage resolves its voice from the project. Since T18b this is part of the product:
the setup screen shows a **Narration voice** picker, project creation accepts a voice, and
`POST /projects/{id}/workflow:start` refuses with `voice_profile_required` until one is selected -
before Temporal is involved and before any provider is called.

Through the API:

```bash
# The voices this deployment can actually narrate with. In fake-provider mode
# that is one deterministic, credential-free local voice.
curl -s -H "X-VidGen-User: local-user" \
  http://localhost:8000/api/v1/projects/$PROJECT_ID/voice-profiles

# Select one.
curl -s -X PUT -H "X-VidGen-User: local-user" -H 'Content-Type: application/json' \
  -d '{"voice_profile_id":"VOICE_UUID"}' \
  http://localhost:8000/api/v1/projects/$PROJECT_ID/voice-profile
```

A profile names a provider and an externally provisioned voice ID. It never carries a credential:
the deployment's provider credentials stay in configuration and are resolved by the worker.
Changing a project's voice produces a new T12 generation identity, so narration and everything
below it is rebuilt rather than reused; the transcript, the analysis and the script above it are
untouched.

#### The local bootstrap CLI

The CLI remains as an idempotent diagnostic and repair tool, for a project whose stored selection
no longer resolves:

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
deliberately through the API above.

### What a local fake-provider run does and does not reach

The workflow runs upload, media processing, transcript acquisition, evidence, episode analysis,
script generation, narration, storyboard, and the per-shot fan-out (keyframe, keyframe QA,
animation, video QA, repair) against real infrastructure with synthetic provider output.

It then renders. The workflow's render stage (T17b) resolves the project's authoritative inputs,
builds the canonical caption track and the immutable render manifest, runs FFmpeg, verifies the
output with ffprobe, and stores the final MP4, captions, manifest and reproducibility report
through `AssetService`. Final editorial QA inspects exactly that render, and a project that passes
its gate reaches `completed` with a downloadable MP4.

Rendering is real FFmpeg work: a ten-shot recap takes tens of seconds locally, and a full-length one
takes minutes. Nothing about it is a paid provider call.

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
| `VIDGEN_BLOB_BACKEND` | `filesystem`; a deployed environment sets `azure` |
| `AZURE_STORAGE_CONNECTION_STRING` | `UseDevelopmentStorage=true` for Azurite |

The Temporal worker additionally reads plain (unprefixed) variables: `TEMPORAL_ADDRESS`
(default `localhost:7233`), `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` (default `vidgen-projects`),
the Temporal Cloud settings `TEMPORAL_TLS_ENABLED` and `TEMPORAL_API_KEY` (both off by default, so
a local dev server connects in plaintext), and the concurrency and shutdown bounds
`TEMPORAL_ACTIVITY_THREADS` (4), `TEMPORAL_MAX_CONCURRENT_ACTIVITIES`,
`TEMPORAL_MAX_CONCURRENT_WORKFLOW_TASKS` (100) and `TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS` (30). The
defaults match `make infra-up`; the deployment-only variables are listed at the bottom of
`.env.example`.

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
(`GET /api/v1/projects/{id}/render`, `GET /api/v1/projects/{id}/render/download`,
`GET /api/v1/assets/{id}/download-url`).

`render:start` **queues** a render job; it does not render. The workflow's render stage picks it up,
and so does the out-of-band worker. The two commands are deliberately separate:

```bash
# Queue a render job for a project, and print only its id.
RENDER_JOB_ID="$(uv run python -m scripts.render_project "$PROJECT_ID" --output-id-only)"

# Execute that render job. This is the process that runs FFmpeg.
uv run python -m workers.render_job.main --render-job-id "$RENDER_JOB_ID"

# Or do both in one process, which is usually what you want locally.
uv run python -m scripts.render_project "$PROJECT_ID" --execute

# Inspect what the render currently binds, without touching a provider.
uv run python scripts/inspect_render.py "$PROJECT_ID"

# Run final editorial QA against the completed render.
uv run python scripts/run_final_editorial_qa.py "$PROJECT_ID" --provider fake

# Resolve the approved render to a signed download.
curl -H "X-VidGen-User: local-user" \
  "http://localhost:8000/api/v1/projects/$PROJECT_ID/render/download"
```

The worker is the same code path the deployed Azure Container Apps Job runs; it also accepts
`--from-env` (reading `VIDGEN_RENDER_JOB_ID`) and `--poll` for a bounded claim loop. It exits `0`
for a completed render or an idempotent reuse, `1` for a failure and `3` for a cancellation, and it
never creates a render job. Nothing in this path requires Azure or a paid provider credential.

## Publishing to YouTube

The publisher uploads an approved, current, T22-`PASS` render to a YouTube channel the user has
connected. Every upload starts **private**, never notifies subscribers, and always sets YouTube's
synthetic-media disclosure; making a video unlisted or public is a separate, explicit action.

### Fake mode (no Google project, no credential, no network)

```bash
uv run python scripts/connect_youtube.py --provider fake
uv run python scripts/publish_youtube.py PROJECT_UUID --provider fake
uv run python scripts/inspect_publication.py PROJECT_UUID
```

### Connecting a real channel

1. Enable the **YouTube Data API v3** in a Google Cloud project and create an OAuth client ID of
   type *Web application*.
2. Register the redirect URI byte for byte. Google permits plaintext only for loopback, so local
   development uses `http://localhost:8000/api/v1/youtube/oauth:callback`.
3. Set `VIDGEN_YOUTUBE_OAUTH_CLIENT_ID`, `VIDGEN_YOUTUBE_OAUTH_CLIENT_SECRET`,
   `VIDGEN_YOUTUBE_OAUTH_REDIRECT_URI` and a token-encryption key (see `.env.example`).
4. Run the local flow, which opens the consent screen, receives the callback on the loopback
   redirect, exchanges the code on the backend and seals the refresh credential:

```bash
uv run python scripts/connect_youtube.py --local
```

### Publishing

```bash
uv run python scripts/publish_youtube.py PROJECT_UUID \
  --provider youtube \
  --connection-id CONNECTION_UUID \
  --privacy private
```

Then, only after checking the private video:

```bash
uv run python scripts/publish_youtube.py PROJECT_UUID \
  --visibility unlisted --publication-id PUBLICATION_UUID
```

A project may publish only when its final render is selected and current, T17 verification passed,
a T18 approval exists for that exact render, T22 recorded a current `PASS` completion gate for it,
nothing was invalidated after rendering, and no shot repair is waiting for a human. A refusal names
the missing prerequisite; it never fabricates a render or bypasses the gate.

An interrupted upload resumes from the offset **YouTube confirms**, never from a local counter and
never from byte zero. When the outcome genuinely cannot be established - the final chunk's response
was lost and the session is gone - the publication stops at `HUMAN_REVIEW_REQUIRED` with the
session evidence preserved, rather than uploading a second copy.

The required scope set is `youtube.upload`, `youtube.force-ssl` and `youtube.readonly`.
`youtube.upload` alone cannot insert a caption track, which is why the second scope is not optional.
No YouTube Partner or CMS scope is ever requested.

**Production limitation.** The API still identifies callers with the development `X-VidGen-User`
header, so this is supported for local development and for an authorised user of a private staging
environment. Public multi-tenant YouTube OAuth is not production-ready here; real application
authentication is the blocker. Details in
[`infra/README.md`](infra/README.md#t25-youtube-publication).

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
uv run python -m scripts.render_project PROJECT_ID --execute
uv run python scripts/run_final_editorial_qa.py PROJECT_ID
uv run python scripts/publish_youtube.py PROJECT_ID --provider fake
```

Inspection-only commands that read persisted state without touching a provider:

```bash
uv run python scripts/inspect_references.py PROJECT_ID
uv run python scripts/inspect_shot_workflow.py PROJECT_ID --storyboard-run-id STORYBOARD_RUN_ID
uv run python scripts/inspect_visual_qa.py PROJECT_ID
uv run python scripts/inspect_render.py PROJECT_ID
uv run python scripts/inspect_publication.py PROJECT_ID
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

`pytest` collects `infra/tests` as well as `tests` (see `testpaths` in `pyproject.toml`). Those are
static policy checks over the compiled Bicep templates; the ones marked `bicep` need the Azure CLI's
Bicep tooling and skip without it. Neither needs an Azure subscription:

```bash
uv run pytest infra/tests -q
make infra-validate
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

**What actually reports today:** the API and the worker both call `initialize_telemetry`
(`src/vidgen/telemetry/bootstrap.py`) at startup, which configures redacted JSON logging on stdout
and, when an export target is configured, batched trace export — to Application Insights via
`VIDGEN_APPLICATIONINSIGHTS_CONNECTION_STRING` (needs the `azure` extra), or to
`VIDGEN_OTEL_EXPORTER_OTLP_ENDPOINT` (needs `opentelemetry-exporter-otlp`, which is not a
dependency). **Metrics are not exported**, so Prometheus scrapes an empty collector and the
provisioned Grafana dashboard stays blank locally; the Temporal UI is the useful local view. Cost
and budget data is available through the API (`GET /api/v1/projects/{id}/costs`) and
`scripts/cost_report.py`.

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
uv run python -m scripts.render_project "$PROJECT_ID" --execute   # a new render for the new inputs
```

A render job carries the identity of the inputs it was queued for. If those inputs move before it
runs, the executor refuses it with `input_identity_changed` rather than quietly rendering something
else; queue a new render job. Changed inputs always produce a new identity, so the new job is a
genuinely different render rather than a reuse of the old one.

**A render job that will not start.** The worker exits nonzero with a structured code instead of
rendering. `render_job_leased` means another worker holds the lease — wait for it to expire or
finish. `render_attempts_exhausted` means the job used its bounded attempt budget; queue a new one
once the underlying failure is fixed. A `lineage` classification (`script_not_approved`,
`visual_qa_missing`, `active_repair_run`, `stale_clip_duration`, and so on) is deterministic: it
will fail identically until the named upstream problem is resolved, and retrying is pointless.

**Browser cannot reach the API.** The web application shows network errors or an empty project
list. Check, in order: the API is up (<http://localhost:8000/healthz>); the Vite dev server is
running on 5173; `VITE_VIDGEN_API_BASE_URL` in `apps/web/.env` is **empty**, so the browser uses the
same-origin `/api` proxy. If the API listens somewhere other than `127.0.0.1:8000`, point the proxy
at it with `VIDGEN_API_PROXY_TARGET` when starting the dev server. If you deliberately point the
browser at a different origin, you must also set
`VIDGEN_CORS_ALLOWED_ORIGINS=http://localhost:5173` in `.env` and restart the API — CORS is off by
default.

## Azure deployment

`infra/` holds the infrastructure-as-code for a VidGen deployment. The complete operational
reference — architecture, resource inventory, network topology, parameters, RBAC, Key Vault secret
names, autoscaling rationale, observability, deployment and migration order, smoke testing,
rollback, backup and recovery, cost drivers and known limitations — is in
[`infra/README.md`](infra/README.md).

```text
infra/
  bicep/main.bicep            resource-group scoped composition
  bicep/modules/              identities, networking, private_dns, container_registry,
                              container_apps_environment, container_apps, container_jobs,
                              postgres, storage, redis, key_vault, monitoring,
                              role_assignments, shared types and app configuration
  bicep/environments/         staging.bicepparam (deployed) and
                              production.example.bicepparam (documented, undeployed)
  tests/                      compiled-template policy tests
  scripts/                    bootstrap_oidc, bootstrap_secrets, validate_infrastructure,
                              scan_secrets, deploy_staging, run_migrations, run_smoke_test,
                              verify_deployment, rollback_revision
  dashboards/                 an Azure Monitor workbook over the T23 signals
```

What it deploys: a VNet-integrated Container Apps environment with an internal load balancer; the
web, API and Temporal worker as Container Apps; finite Container Apps Jobs for migration,
out-of-band rendering, the smoke test and administration; PostgreSQL Flexible Server with VNet
injection; private Blob Storage; Azure Managed Redis; Key Vault with RBAC; one managed identity per
workload with least-privilege role assignments; private endpoints and private DNS for every linked
service; Log Analytics and Application Insights carrying the T23 telemetry; and GitHub Actions
deployment through Azure OIDC federation with no client secret anywhere.

Staging is private by default (`publicIngressEnabled` is `false`), and the API is never externally
reachable whatever that parameter is set to, because production authentication does not exist yet.
`.github/workflows/deploy-staging.yml` enforces the deployment order: build and push
commit-SHA-tagged images, resolve digests, validate and `what-if`, wait for the protected
environment approval, deploy infrastructure, run the migration job exactly once, activate the new
revisions, run the private smoke test, verify health and digests, and roll traffic back if the
smoke test fails. A failed migration never reaches revision activation, and rollback never
downgrades the database.

**T24 is not marked complete.** Its acceptance — a private staging deploy, migrations run once,
workers autoscaling — can only be demonstrated by a real deployment, and none has run. Every static
and CI check passes; `infra/README.md` lists what a first deployment needs.

Two deployment-facing settings are worth knowing locally, because both default to the local value:

- `VIDGEN_BLOB_BACKEND` selects the asset store: `filesystem` (local and every test) or `azure`
  (reached over a private endpoint with the workload's managed identity — no key, connection string
  or SAS token is ever configured). Any other value is rejected at startup.
- The Temporal worker reads `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`,
  `TEMPORAL_TLS_ENABLED` and `TEMPORAL_API_KEY`. TLS and the API key are off by default so a local
  `temporal server start-dev` connects in plaintext with no key; Temporal Cloud requires both.

Validate the templates locally, with no Azure credential and no deployment:

```bash
az bicep build --file infra/bicep/main.bicep
./infra/scripts/validate_infrastructure.sh
uv run pytest infra/tests -q
```

`make infra-validate` runs the second of those.

## The control plane: what "accepted" means

Every asynchronous product command is a durable row in `control_commands` before the API reports
that it was accepted. Nothing returns `queued`, `accepted` or a workflow ID unless one of the
following is already true:

- the work was persisted transactionally and a dispatcher can claim it;
- the target Temporal workflow or child workflow was successfully started;
- an identical completed command is being idempotently reused.

An in-memory flag, an idempotency response on its own, or a workflow ID computed from a request is
none of those, and the database refuses to store a `running`, `awaiting_review` or `completed`
command that does not name the workflow that was actually started.

### Lifecycle

```text
pending → claimed → dispatching → running → completed
                                     ↕            ↘ failed / cancelled
                              awaiting_review
```

A command is `pending` until a dispatcher claims it under a lease. It becomes `running` only after
the dispatcher has started or signalled the real workflow and written that workflow's identity
back. `awaiting_review` means it is durably waiting on a person - a reference approval, a shot
review - and will resume when that decision arrives. A failed command carries a structured code and
says whether it can be retried.

### Inspecting and steering commands

```bash
BASE=http://localhost:8000/api/v1/projects/$PROJECT_ID
H='X-VidGen-User: local-user'

curl -s -H "$H" $BASE/commands                       # every command, and the generation lineage
curl -s -H "$H" $BASE/commands/$COMMAND_ID           # one command, with progress and failure
curl -s -X POST -H "$H" $BASE/commands/$COMMAND_ID:cancel
curl -s -X POST -H "$H" $BASE/commands/$COMMAND_ID:retry
```

The project dashboard shows the same list, stops polling once nothing can change, and exposes the
retry that a failed command permits.

### Continuing a paused project

A project workflow now *completes* at every human pause: references awaiting approval, shots
awaiting review, final QA requiring review. Continuing it is a new immutable generation run rather
than a signal to a closed execution, so previous runs stay readable as history and nothing above
the entry stage is rerun.

```bash
curl -s -X POST -H "$H" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"entry_stage":"shot_generation","reason":"review_resolved"}' \
  $BASE/workflow:continue
```

### Which command each action creates

| Action | Command | What actually happens |
| --- | --- | --- |
| `POST /references:build`, `…/references:generate` | `reference_build`, `reference_generate` | Starts (or adopts) `ContinuityReferenceWorkflow` for the project's reference run |
| Approving a reference set | `reference_apply` | Signals the waiting T19 workflow, which binds the approved bundle and stales only the affected shots |
| `POST /shots/{id}:regenerate` | `shot_regenerate` | Starts a genuinely new `ShotWorkflow` child under the next regeneration identity; the locked child and its assets stay as history |
| `POST /shots/{id}:retry` | `shot_retry` | Resumes the live child, or starts an immutable recovery run when it has closed |
| A T20 approve or reject | `shot_review_continue` | Resumes or replaces the reviewed shot; a deterministic hard failure is never approved into a pass |
| A T21 `retry` or `restart_after_reference_correction` | `shot_retry` | Repairs the shot again, bounded by the existing T21 policy. `acknowledge` and `resolve` create no work and never claim the shot passed |
| A confirmed transcript edit | `transcript_revision` | A new generation run from `episode_analysis`; the source media and the transcript itself are reused |
| Selecting a revised script | `script_revision` | A new generation run from `narration` - the first stage that would spend money on the new script |
| `POST /final-qa:run` | `final_qa_run` | Runs T22 against the current selected render in its own workflow |
| `POST /final-qa/{id}:remediate` | `final_qa_remediation` | Executes the owning stage - a rerender, or a shot repair. A routing-only target is refused with `remediation_unsupported`, never accepted |
| `POST /workflow:continue` | `project_continue` | Opens a new generation run and starts the project workflow at its entry stage |

Every one of these produces a new render identity where the final bytes can change, requires a new
T22 run for that render, and invalidates T25 publication eligibility for the superseded render.
Nothing is ever republished automatically, and a prior YouTube upload is never deleted or mutated.

## Additional documentation

| Document | What it covers |
| --- | --- |
| [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) | Authoritative architecture, data model, stage specifications, and the T01-T26 roadmap |
| [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) | What each implemented roadmap task delivered, with its per-stage local commands |
| [`docs/ROADMAP_GAPS.md`](docs/ROADMAP_GAPS.md) | Confirmed gaps deliberately left outside T18b, with evidence and proposed follow-up tasks |
| [`infra/README.md`](infra/README.md) | The Azure deployment's operational reference |
| [`AGENTS.md`](AGENTS.md) | Contributor and coding-agent rules |
| `.env.example`, `apps/web/.env.example` | Every environment variable with its local default |
| `packages/contracts/schema` | Exported JSON Schema for the inter-stage contracts (`make schemas`) |
| <http://localhost:8000/docs> | Generated OpenAPI documentation for the running API |
