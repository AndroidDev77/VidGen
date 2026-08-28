# VidGen

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

The complete system architecture and T01-T26 implementation roadmap are maintained in
[`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md). Contributors and coding agents should
read it together with `AGENTS.md` before planning the next roadmap task.

This repository implements roadmap tasks T01 through T20 and T23, so the pipeline now runs end to
end behind a customer-facing web application. The foundations include subtitle-first transcript
acquisition, restartable episode analysis, comedy script generation, measured narration,
deterministic storyboard timing, reviewed keyframe and video generation, deterministic captioned
rendering, an owner-scoped review UI, and cloud-neutral observability/cost controls:

T19 continuity references are complete and merged. The additive implementation defines evidence-linked
immutable character/location identity versions, interval-scoped temporary state, deterministic
source-frame ranking and reference compaction, shot-bundle identities, owner-scoped review
projections, and a `continuity_references_v1` T14 adapter. Projects without an approved bundle keep
the unchanged legacy image-generation behavior. T19 now also reuses the T14 provider boundary,
technical validation and immutable `AssetService` writes with T23 attempt/budget accounting; waits
durably for an idempotent human approval signal; and marks only affected T14/T15 outputs and the
T17 render stale before dispatching content-bound T16 regeneration commands. The deterministic
ten-shot acceptance fixture covers future-state isolation, anonymous identities, T14 injection,
targeted regeneration, historical preservation and retry accounting.

T20 semantic visual QA is complete. Generated keyframes and canonical clips are evaluated against
their approved storyboard intent, the exact approved T19 identity and location versions, T13
continuity state and deterministic media thresholds. The score is always recomputed by application
code from validated dimension values, a hard failure always blocks the shot, every actionable
finding cites an exact frame and timestamp, and every non-pass result carries structured repair
codes. T20 identifies repairs; T21 owns repair and fallback routing.

- Python monorepo, CI, and local infrastructure
- Versioned Pydantic contracts plus exported JSON Schema
- PostgreSQL data model and Alembic migration
- Content-addressed asset storage with provenance
- Deterministic fake AI and media providers for offline tests
- Owner-scoped resumable source-video uploads
- Deterministic FFmpeg media probing, audio extraction, scene detection, and frame extraction
- Restartable transcription, overlap reconciliation, and anonymous speaker diarization
- Embedded, sidecar, and OpenSubtitles acquisition with audio-transcription fallback
- Restartable plot compression and comedy script generation with editorial review and revision
- Provider-neutral narration (fake, OpenAI, and optional ElevenLabs), deterministic 48 kHz mono
  PCM normalization, bounded forced alignment, quality gates, and restartable relational checkpoints
- Restartable storyboard direction with an exact-integer deterministic retimer, versioned visual
  provider capability profiles, structured continuity state, and targeted per-segment repair
- T14 image-generation foundations: strict provider-neutral keyframe contracts, a deterministic
  versioned prompt compiler, bounded technical image validation, content-addressable provenance
  projections, a network-free fake provider, and an OpenAI Image API adapter pinned by default to
  `gpt-image-2-2026-04-21`. T14 review and CI are complete.
- A customer-facing React review application and the owner-scoped control-plane APIs behind it
- T20 semantic visual QA: deterministic sampling and media checks, a versioned weighted rubric,
  application-side score recomputation, bounded adjudication, evidence-linked findings, structured
  repair codes, T16 gating, T17 render eligibility, and review-UI panels

## T18 MVP review UI and control-plane APIs

T18 is the first customer-facing VidGen application. An owner creates a project, uploads a source
video, starts and monitors the workflow, reviews and edits the transcript and the recap script,
inspects the storyboard and individual shots, regenerates a single shot without rerunning its
siblings, previews the finished T17 render with selectable captions, approves it, and downloads the
final MP4, SRT, and WebVTT assets.

### Architecture

The web application never talks to a provider, a workflow, or the database. It calls one typed API
client, which calls the owner-scoped FastAPI control plane, which delegates to the review services.
Route handlers stay thin: HTTP validation lives in the schemas, owner authorization in
`apps/api/routes/_common.py`, concurrency and idempotency in `vidgen.review`, domain mutation in
`services.review`, and response projection in `vidgen.review.projections`. No route handler calls
an AI provider, a media provider, FFmpeg, or a Temporal activity.

### Frontend stack

`apps/web` is a workspace package built with React 18, TypeScript in strict mode, Vite, Fluent UI
v9, TanStack Query, React Router, Vitest with React Testing Library, Mock Service Worker for
deterministic API tests, and Playwright for browser acceptance tests. Canonical contracts are
reused from `@vidgen/contracts` rather than restated as loose frontend interfaces.

### Local development

```bash
# Terminal 1: the API, using the deterministic fake workflow controller.
make run-api

# Terminal 2: the web application on http://localhost:5173.
make run-web           # or: pnpm dev:web
```

`apps/web/.env.example` documents the two browser variables:

```text
VITE_VIDGEN_API_BASE_URL   # empty in local development: requests use the Vite dev proxy
VITE_VIDGEN_DEV_USER       # the local X-VidGen-User development identity
```

No server secret is ever exposed through a `VITE_` variable. T18 keeps the existing local
`Principal` and `X-VidGen-User` development identity; production authentication is out of scope.

The web container (`docker compose --profile web up --build web`) serves the built assets and
reverse-proxies the API under `/api` on the same origin, so the browser never makes a cross-origin
request and the API keeps CORS disabled. `VIDGEN_API_UPSTREAM` selects the API it proxies to.

### Application routes

```text
/                              redirect to /projects
/projects                      project list
/projects/new                  create a project and upload its source video
/projects/:projectId           dashboard, stage timeline, costs, and failures
/projects/:projectId/transcript  transcript review and editing
/projects/:projectId/script      script review, editing, and version selection
/projects/:projectId/storyboard  storyboard grid, timeline, and shot inspector
/projects/:projectId/review      final render preview, approval, and downloads
```

Selected tabs, the inspected shot ID, the transcript search term, and the caption toggle live in
the URL so a view can be shared or reloaded. Media data and stage payloads never enter history
state.

### Project creation and upload

The new-project flow collects a name, target recap duration, visual style, and humor intensity, and
never exposes provider credentials or model routing. The upload integrates the existing T05
multipart API: the browser validates the file type and size, hashes the file with a streaming
SHA-256 in a Web Worker (one bounded slice resident at a time), slices the `File` into configured
parts, uploads them with bounded concurrency, retries transient part failures with bounded backoff,
treats a conflicting duplicate part as an actionable failure, and finalizes only after every part
succeeds. Hashing and upload progress are reported separately, cancellation is supported, and a
resume is refused when the reselected file's fingerprint does not match. Source bytes are never
placed in application state, local storage, logs, or the query cache.

### Workflow control and progress

Starting a project reuses one stable workflow ID per project, refuses to start before the source
upload completes, and is idempotent, so a retried or duplicated request adopts the existing run
instead of creating a second workflow. Cancellation and compact query-visible status are exposed.
Only compact identifiers, hashes, statuses, and counts cross the Temporal boundary.

Progress reaches the browser through Server-Sent Events at
`GET /api/v1/projects/{projectId}/events`. Owner authorization happens before streaming starts; the
stream honours `Last-Event-ID`, deduplicates by sequence, sends heartbeat comments, uses bounded
payloads with no transcript or script text, and reads durable events in short transactions rather
than holding one open. The frontend reconnects with bounded exponential backoff and falls back to
polling after repeated failures. Events invalidate only the queries they affect.

The dashboard renders the fourteen known stages in repository order. A progress percentage is shown
only when the backend computed one from real shot counts; it is never guessed from a status name.

### Transcript and script editing

Transcript review offers a searchable segment list with times, speaker labels, confidence, keyboard
navigation, and per-segment saving. An edit preserves the original provider provenance, never
silently overwrites a newer version, returns the downstream invalidation set, and reruns nothing.

Script review is version-aware. A material change to an approved immutable version creates a new
compatible version rather than mutating it in place, selects the new version, preserves the
original rows, and invalidates only the required downstream lineage. Narration, storyboard, shots,
and render are never regenerated automatically.

### Storyboard, shot inspector, and single-shot regeneration

The storyboard grid presents the canonical shots in sequence with exact T13 timing, trim
information, camera and reference details, provider, model, attempt count, and cost. The T13 timing
manifest is the timing authority, and the timeline is read-only in T18. The shot inspector shows
the canonical definition, keyframe and video attempts, selected outputs, provider task IDs in
expandable technical details, child-workflow status, and regeneration history, and supports refresh,
targeted retry, cancellation, attempt selection, and regeneration.

Regenerating one shot verifies project and shot ownership, requires an idempotency key and an
expected row version, creates a new material regeneration identity, preserves the previously locked
attempt, leaves sibling shot workflows untouched, invalidates only that shot's render dependency,
and marks the previous verified render stale rather than deleting it. The exact invalidation set is
returned before confirmation.

### Final review, approval, and downloads

The final-review page loads only a completed, verified T17 render. It shows the player, render
status and version, expected and measured duration, verification result, caption controls, loudness
summary, warnings, selected shot count, approval state, and expandable technical provenance.
Captions come from the standalone WebVTT asset loaded into a `<track>` element, so the browser's own
selectable captions work. Approval verifies ownership, `If-Match`, an idempotency key, completion,
verification, freshness, and lineage, then persists the approving principal, the render ID, and a
timestamp, preserves previous approval records, and emits a bounded event. Approval never publishes
the video; T25 owns publication.

Signed download URLs are requested shortly before playback or download, cached briefly, refreshed
when they expire, and never written into permanent application state or browser storage.

### Owner authorization, concurrency, and idempotency

Every project, transcript, script, storyboard, shot, render, review, event, and asset lookup is
verified against the authenticated `Principal`. Cross-project and cross-owner identifiers return the
same `404` as a missing one, so a foreign resource's existence is never disclosed. Ownership is
never accepted from a request body.

Every mutable operation takes `If-Match: <row-version>` and `Idempotency-Key: <stable-client-key>`.
A missing `If-Match` returns a structured precondition error, a stale one returns `409` with the
current version, and row-version comparisons happen transactionally in `resource_versions`.
Repeating a key with an equivalent request replays the original result; reusing it with a different
request is a structured conflict. Duplicate browser submissions therefore cannot create a second
script revision, shot workflow, render job, or approval.

### T23 cost and failure visibility

The dashboard reads the existing T23 APIs for the warning and hard budgets, reserved, committed and
remaining amounts, cost by provider, model and operation, recent provider attempts, and recent
pipeline failures. Costs are rendered as exact decimal strings and are never parsed into a binary
float. No second cost system is introduced.

### Test commands

```bash
make verify                          # ruff, mypy --strict, pytest, schema export
uv run pytest -q                     # backend, including the T18 API and T20 visual-QA suites
pnpm lint                            # workspace lint
pnpm typecheck                       # workspace strict TypeScript
pnpm test                            # workspace unit tests
pnpm --filter @vidgen/web build      # production frontend build
pnpm --filter @vidgen/web test:e2e   # Playwright MVP acceptance run
```

## T16 per-shot workflows

T16 adds a deterministic child workflow for every canonical selected T13 shot. The parent resolves
the authoritative ordered shot IDs through an activity and fans them out with a configurable limit
of ten by default. Child IDs bind the project, storyboard and timing hashes, stable shot identity,
T14 configuration, T15 capability profile, pipeline versions, and attempt-policy version. An
unchanged input therefore reuses the same Temporal identity; a material change creates a new child
without overwriting the locked result.

Each child advances through `DEFINED`, `PROMPTING`, `KEYFRAME_GENERATING`, `KEYFRAME_QA`,
`ANIMATING`, `VIDEO_QA`, and `LOCKED`, with explicit `FAILED` and `CANCELLED` outcomes. Workflow
history contains compact IDs, hashes, states, durations, and failure codes only. Activities resolve
lineage, invoke or resume the existing T14/T15 services, persist checkpoints, and query the T23
ledger. Thus provider calls, media bytes, prompts, validation reports, storage, and database access
remain outside workflow code. T15 polling resumes the persisted remote task rather than submitting
another paid request.

Failures remain local to a child and locked siblings are retained. Retry policies classify
deterministic lineage, configuration, capability, hard budget, ambiguous submission, and
cancellation outcomes as non-retryable; transient work has bounded backoff. Parent cancellation
stops scheduling and requests cancellation of active children while durable outputs and remote task
identities remain intact. Compact parent and child queries expose counts, stages, attempts, output
references, failures, warnings, and cost totals. Idempotent per-shot status, retry, cancel, resume,
outputs, and regenerate commands are supported; regenerate requires a new material input identity.

Fake-provider development requires no paid credentials:

```bash
VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS=true \
  uv run python -m workers.temporal_worker.main
uv run python scripts/run_shot_workflow.py PROJECT_UUID \
  --storyboard-run-id STORYBOARD_RUN_UUID --provider fake --concurrency 10
uv run python scripts/inspect_shot_workflow.py PROJECT_UUID \
  --storyboard-run-id STORYBOARD_RUN_UUID
uv run python scripts/run_shot_workflow.py PROJECT_UUID \
  --storyboard-run-id STORYBOARD_RUN_UUID --shot-id SHOT_UUID \
  --child-workflow-id CHILD_WORKFLOW_ID --command retry
```

## T15 Runway image-to-video generation

T15 loads only the selected, complete T13 storyboard and the newest completed T14 run for that
exact storyboard version. It revalidates T10-T14 selection, project ownership, canonical artifact
hashes, dense timing, and a selected technically valid `FIRST_FRAME` for every requested shot.
Stale, cross-project, incomplete, invalid, or mismatched inputs fail with actionable lineage codes.

The provider-neutral `VideoGenerationProvider` boundary separates task submission, retrieval, and
cancellation from canonical orchestration. Production uses the official asynchronous Runway Python
SDK; tests and local development use a deterministic fake that creates small H.264 MP4 files with
FFmpeg and makes no network or paid calls. The versioned routing policy defaults to `gen4_turbo`.
It selects `gen4.5` only for configured hero shots when the project quality profile, remaining
budget, and capability registry permit the premium model. Current capability validation requires
image-to-video, an exact supported 2-10 second T13 generation duration, a supported output ratio,
an MP4 result, a bounded prompt, and a keyframe whose aspect ratio exactly matches the output.

Motion prompts are compiled without an LLM in stable action, pose, camera, timing, environment,
continuity, and restriction order. Runway receives the verified first keyframe as a bounded data
URI. Unsupported last-frame controls are never sent; strict T13 last-frame enforcement fails
rather than being silently ignored. Signed URLs, data URIs, output URLs, provider payloads, and
video bytes are excluded from contracts, database JSON, logs, telemetry, and Temporal history.

Before submission T15 creates or reuses a T23 provider attempt, estimates the configured per-second
price, reserves budget, and commits a pre-call checkpoint. It commits the remote task ID immediately
after submission and before polling. Restarts, activity retries, polling timeouts, and transient
retrieval failures resume that task and never submit a replacement. If the submission response is
lost before an ID is known, T15 records an ambiguous outcome and requires manual reconciliation;
the Runway API does not document an application idempotency key that can prove exactly-once task
creation.

Successful output is streamed into bounded temporary storage, hashed, and checked with FFprobe and
boundary FFmpeg decodes for container, codec, dimensions, frame rate, timebase, duration, streams,
frame count, finite metadata, size, and truncation. AssetService preserves the immutable provider
output with storyboard, timing, keyframe, task, attempt, prompt, capability, routing, configuration,
and validation provenance. T13 trim values then drive deterministic single-threaded H.264
re-encoding with zero-based timestamps; the canonical clip is probed again and stored as a child of
the original. T15 performs technical validation only, not semantic motion or identity QA.

Local commands are:

```bash
uv run python scripts/generate_shot_videos.py PROJECT_UUID --provider fake
RUNWAYML_API_SECRET=... uv run python scripts/generate_shot_videos.py PROJECT_UUID --provider runway
uv run python scripts/generate_shot_videos.py PROJECT_UUID --provider fake --shot-id SHOT_UUID
RUNWAYML_API_SECRET=... uv run python scripts/generate_shot_videos.py PROJECT_UUID --provider runway --model gen4_turbo
```

## T14 image generation

T14 consumes only the selected T13 storyboard and creates a required first keyframe plus a last
keyframe only when the storyboard asks for one. It does not create the T19 character/location
reference bibles and does not begin T15 animation or T16 child workflows. Prompt sections are
compiled in a stable order without another LLM; required identity, location, pose, action, camera,
and negative constraints are never discarded to fit a provider limit.

The production adapter uses `images.generate` without references and `images.edit` with verified
project-owned PNG, JPEG, or WebP references. The default is opaque PNG, medium quality, and
1536x864. Dimensions must be divisible by 16, at most 3:1, and within centralized pixel and edge
limits. Generated bytes are decoded and technically validated before immutable AssetService
persistence. Application identities bind prompts, references, provider configuration, and
validation configuration; unknown provider outcomes remain ambiguous rather than being blindly
retried. Provider calls reuse the T23 attempt, reservation, reconciliation, telemetry, and cost
ledger infrastructure.

Run deterministic local keyframe generation without provider credentials:

```bash
uv run python scripts/generate_keyframes.py PROJECT_UUID --provider fake
uv run python scripts/generate_keyframes.py PROJECT_UUID --provider fake --shot-id SHOT_UUID
```

For configured OpenAI generation:

```bash
VIDGEN_OPENAI_API_KEY=... \
  uv run python scripts/generate_keyframes.py PROJECT_UUID --provider openai
```

## Quick start

```bash
cp .env.example .env
uv sync --all-groups
make verify

# The web application and its workspace checks.
pnpm install
cp apps/web/.env.example apps/web/.env
make verify-web
```

With Docker installed:

```bash
docker compose up -d postgres redis azurite temporal
uv run alembic upgrade head
make verify-stack
```

The default application database is `postgresql+psycopg://vidgen:vidgen@localhost:5432/vidgen`.

## Packages

- `vidgen.contracts`: canonical inter-stage contracts
- `vidgen.db`: relational models and repositories
- `vidgen.storage`: content-addressed storage and asset service
- `vidgen.providers`: provider protocols and deterministic fakes
- `vidgen.review`: T18 row versions, idempotency, project events, and workflow control
- `services.review`: T18 transactional review mutations and downstream invalidation
- `apps/web`: the T18 React review application (`@vidgen/web`)

## Contract schemas

Run `make schemas` to export JSON Schema into `packages/contracts/schema`.

## Upload and deterministic media processing

Start the API:

```bash
make run-api
```

Upload an MP4 with streaming parts and exact byte/hash validation:

```bash
uv run python scripts/upload_video.py /absolute/path/to/episode.mp4
```

The command prints the project UUID and finalized source asset. The OpenAPI console at
`http://localhost:8000/docs` exposes every individual endpoint and request schema.

After finalization, process the source using the deterministic worker CLI:

```bash
uv run python scripts/process_media.py PROJECT_UUID
```

The command probes the source, extracts 16 kHz mono WAV audio, detects scenes, extracts one
representative PNG per scene, persists provenance, and finishes with project status `media_ready`.

## Transcription

Run the restartable pipeline with the deterministic provider:

```bash
uv run python scripts/transcribe_media.py PROJECT_UUID --provider fake
```

To use the configured OpenAI provider, set `VIDGEN_OPENAI_API_KEY` and run:

```bash
uv run python scripts/transcribe_media.py PROJECT_UUID --provider openai --language en
```

Audio is encoded into content-addressed FLAC chunks below the configured byte ceiling. Each
chunk is independently checkpointed, transcribed, and diarized. The canonical transcript removes
overlap deterministically, retains source timestamps and anonymous speaker labels, and requires at
least 98 percent voiced-audio coverage.

Overlap reconciliation requires timestamp agreement for text matches and preserves distinct-time
repeated dialogue when provider chunks disagree.

## Subtitle-first transcript acquisition

Configure `VIDGEN_OPENSUBTITLES_API_KEY` and optionally the matching username and password, then
run:

```bash
uv run python scripts/acquire_transcript.py PROJECT_UUID
```

The command checks embedded text subtitle tracks first, then sidecar assets, then performs an
OpenSubtitles movie-hash search with title/episode fallback. An adequate subtitle is normalized
into the same selected canonical transcript tables used by T07. If none passes validation, the
command falls back to the configured OpenAI transcription provider.

For deterministic local verification with no network or paid calls:

```bash
uv run python scripts/acquire_transcript.py PROJECT_UUID \
  --subtitle-provider fake --transcription-provider fake
```

Set `VIDGEN_SUBTITLE_SYNC_ENABLED=true` when the optional `ffsubsync` command is installed to
audio-align downloaded provider subtitles and include its offset and correlation in candidate
ranking. Embedded extraction stays FFmpeg-only; MKVToolNix is not required.

## Temporal orchestration and scene evidence (T08-T09)

`ProjectWorkflow` now owns the upload, deterministic media-processing, subtitle-first transcript
acquisition, and evidence stages. Workflow history carries only UUID-based versioned contracts;
all database, storage, FFmpeg, network, and provider work is delegated to idempotent activities.
Start the local Temporal development server with `make infra-up`, then run the worker with:

```bash
uv run python -m workers.temporal_worker.main
```

Scene evidence remains provider-neutral. It clips overlapping transcript segments to source scene
boundaries, retains anonymous speaker labels and subtitle/audio provenance, references frames and
contact-sheet assets by UUID, and derives package identity from canonical input hashes.

## Episode Analyst (T10)

## Observability and cost ledger (T23)

T23 adds structured JSON logging with recursive redaction, W3C trace propagation, bounded
Prometheus/OpenTelemetry instruments, canonical failure classification, and a reusable
`instrument_provider_attempt` context manager. Provider attempts, versioned pricing, transactional
reservations, immutable reconciliation events, project budgets, and failed-work projections are
persisted by migration `0007_observability_costs`.

Start the unchanged base stack with `make infra-up`. Start the optional local stack with:

```bash
docker compose --profile observability up -d
```

Prometheus is at <http://localhost:9090/targets> and Grafana is at <http://localhost:3000>.
Collector traces are available in `docker compose logs otel-collector`. Run deterministic fake
providers using the existing `--provider fake` commands; no Azure or paid-provider credentials are
needed. Generate a machine-readable report with:

```bash
uv run python scripts/cost_report.py PROJECT_UUID --json
```

The parent Temporal workflow runs Episode Analyst after the selected T09 evidence package is ready.
The activity maps each evidence scene independently, commits successful scene checkpoints, then
reduces only validated map results into canonical `EpisodeAnalysis` 1.0. Temporal history contains
only project/source IDs and the normal versioned stage envelope; provider payloads and canonical JSON
are persisted outside workflow history.

Deterministic validation resolves every source reference against the selected evidence package and
checks chronology, entity foreign keys, alias evidence, state events, relationships, mandatory beat
evidence, and the acyclic chronological beat dependency graph. Invalid scene output is repaired once
without rerunning valid scenes; invalid reduce output receives one targeted repair. A failed replacement
does not deselect the prior valid analysis.

Run the zero-cost deterministic provider locally:

```bash
uv run python scripts/analyze_episode.py PROJECT_UUID --provider fake
```

Run the configured OpenAI Responses adapter (the model remains configuration):

```bash
VIDGEN_OPENAI_API_KEY=... VIDGEN_ANALYSIS_MODEL=... \
  uv run python scripts/analyze_episode.py PROJECT_UUID --provider openai
```

## Compression and comedy script pipeline (T11)

T11 consumes the selected, validated T10 `EpisodeAnalysis` and produces a causally complete
`CompressedPlotPlan`, an original comedy `RecapScript` draft, a deterministic validation report, a
structured Comedy Editor review, up to two targeted revision passes, and a selected, versioned,
diffable `RecapScript` suitable for T12 narration generation.

The parent Temporal workflow runs script generation after Episode Analyst completes. Compression,
writing, and editing each run inside their own activity; only project/source IDs and the normal
versioned stage envelope cross workflow history, never plot plans, script text, or provider
responses. Deterministic validation (word count, beat coverage, references, structure, joke
annotations, near-verbatim transcript detection) never relies on an LLM to enforce a hard
constraint. A failed replacement attempt never deselects a project's prior approved script.

Run the zero-cost deterministic provider locally:

```bash
uv run python scripts/generate_script.py PROJECT_UUID --provider fake
```

Run the configured OpenAI Responses adapter (the model remains configuration):

```bash
VIDGEN_OPENAI_API_KEY=... \
  uv run python scripts/generate_script.py PROJECT_UUID --provider openai
```

## Storyboard generation and deterministic timing (T13)

T13 turns the approved T11 script and the measured T12 narration into a versioned `Storyboard`
plus a deterministic timing manifest that T14, T15, and T16 consume. It never calls a visual
generation provider; it only plans against that provider's declared capabilities.

### Architecture

Five concerns stay separated, and each has its own module under `services/storyboard/`:

| Concern | Module | Responsibility |
| --- | --- | --- |
| Creative proposal | `providers.py`, `openai_adapter.py`, `fake_provider.py` | A provider-neutral Storyboard Director proposes semantic shots. It never decides timing. |
| Deterministic timing | `retimer.py` | Owns final timing with exact integer microsecond arithmetic. |
| Validation | `validator.py` | Structural and referential rules only; no model decides whether output is acceptable. |
| Identity | `canonicalize.py`, `boundaries.py` | Stable IDs, canonical ordering, content hashes, and approved narration boundaries. |
| Orchestration | `pipeline.py`, `commands.py` | Authoritative input selection, checkpoints, repair, persistence, and asset provenance. |

Canonical contracts live in `src/vidgen/contracts/storyboard.py`, relational projections in
`src/vidgen/db/storyboard_models.py`, and lineage queries in
`src/vidgen/db/storyboard_repository.py`.

### Authoritative input selection and lineage rules

The pipeline refuses to combine outputs from different versions. Before any provider call it
verifies that every input belongs to the requested project and that:

- the T10 episode model is selected, complete, has a canonical asset, and no newer version exists;
- the T11 script is selected, approved, has a dense segment sequence, and was generated from that
  exact episode model;
- the T12 narration run is selected, complete, and was generated from that exact approved script
  ID and version;
- every required script segment has a complete narration segment with a normalized audio asset,
  a measured ffprobe duration, and persisted word timings;
- referenced T09 evidence still exists inside the project's selected evidence package.

Each rejection raises a structured `StoryboardLineageError` carrying a machine-readable code
(`narration_script_mismatch`, `episode_model_stale`, `script_unapproved`, and so on). These are
deterministic configuration failures and are never retried against a provider.

### Storyboard Director abstraction

`StoryboardDirector` is a Protocol taking a `StoryboardProviderRequest` and returning a
`StoryboardProviderResult`. No OpenAI SDK object crosses that boundary. The request carries a
stable idempotency key, upstream IDs and hashes, the exact measured duration, word timings,
approved boundaries, evidence and character/location references, incoming continuity, the
capability profile, contract and prompt versions, trace context, and the attempt number. The
result carries proposals, continuity expectations, provider/model identity, usage quantities, and
redacted metadata; credentials and raw payloads never appear in it.

Two adapters ship: a configured OpenAI Responses adapter using strict structured output (schema
`$defs` are inlined because strict mode rejects `$ref`), and a deterministic credential-free fake.
Model names and provider configuration are centralized in `services/storyboard/providers.py`.

### Capability profiles

`VisualProviderCapability` is a versioned, provider-neutral contract describing supported
generated durations, duration bounds and increments, aspect ratios and resolutions, maximum
characters and reference images per shot, camera-motion and transition support, image-to-video and
text-to-video support, continuity/seed support, the trimming policy, and a content-bound
`capability_hash`. Two profiles are configured: `runway-gen4-turbo` (continuous, 100 ms steps,
1-10 s) and `veo-3.1-fast` (discrete, {4, 6, 8} s). The timing solver reads only this contract, so
no single vendor's limits are baked into it.

### Deterministic retiming algorithm

Measured T12 durations are the timing authority; narration is never stretched and the approved
script is never changed. All canonical timing is exact integer microseconds, converted with
`Decimal(str(seconds))` so no binary floating-point drift can accumulate.

1. Build a boundary table from measured word timings, marking sentence, clause, and comedy-beat
   boundaries (the latter derived from T11 joke setup and punchline spans).
2. Map each proposal's word range onto those boundaries, snapping every interior boundary to a
   word end and the final shot to the exact measured duration.
3. Split any shot longer than the effective maximum at the approved boundary nearest the even
   division point, preferring sentence over clause over beat over plain word.
4. Merge any shot shorter than the configured minimum into a neighbour.
5. Repeat 3 and 4 to a fixed point; if it cannot converge, reject the plan.
6. Verify monotonic, gapless, non-overlapping, strictly positive intervals whose last shot ends at
   the exact measured narration duration.

Every deviation from the proposal is recorded as a `TimingAdjustment` (`boundary_snap`, `split`,
`merge`, `generation_round_up`, `trim`, `final_end_snap`, `residual_allocation`).

### Unsupported durations and trimming

When an exact usable duration is not a supported generated duration, the retimer requests the
smallest supported duration that covers it and records deterministic trim instructions under the
profile's trimming policy (`trim_end`, `trim_start`, or `trim_center`, with the odd microsecond
going to the tail). Where no interior boundary exists and a single narration word outlives the
provider maximum, the interval is divided evenly and the rounding residual is allocated one
microsecond at a time to the earliest shots. If no supported solution exists at all, the plan is
rejected rather than fudged.

Transition timing is explicit. A non-cut transition declares a duration and a handle; the handle is
extra *generated* material recorded in the timing manifest and counted in the trimmed region, so
usable shot intervals stay contiguous and no narration coverage is left unaccounted for.

### Continuity state

Continuity is structured, not prose: present characters, per-character appearance state, location
and sub-location, time of day, props and ownership, subject screen positions, screen direction,
emotional state, environment conditions, the previous shot reference, and unresolved warnings.
Every shot declares its incoming assumptions and its expected outgoing state, and consecutive
shots are checked for unexplained contradictions; a deliberate change is legitimate only when the
shot carries a matching continuity warning. Anonymous speakers keep their anonymity - T13 never
invents a character identity for them. T13 references logical characters and locations from the
episode model; T14 and T15 create their visual reference assets later.

### Targeted repair

Each segment is validated before canonical persistence. A repairable creative failure (coverage
gap, invalid overlap, impossible allocation, unsupported duration, excessive characters, too many
references, missing continuity, invalid character/location reference, missing evidence, provider
schema failure, continuity contradiction) sends only that segment and its structured diagnostics
back to the Director under a stable repair idempotency key. Successful segment checkpoints are
preserved, repair attempts are bounded and recorded with their input diagnostics and validation
result, and unrelated segments are never regenerated. Deterministic lineage or configuration
failures fail immediately with no provider retry.

### T23 cost and telemetry integration

Every production Director call runs inside the existing `instrument_provider_attempt` context:
durable provider-attempt identity, pricing-catalog estimate, transactional budget reservation with
warning and hard-cap enforcement, trace propagation, and a durable pre-call checkpoint, then usage
recording, estimate/actual reconciliation, reservation release, bounded metrics, T23 failure
classification, and redaction of prompts, credentials, and provider metadata. Idempotent retries
reuse the same attempt and reservation, so they create no duplicate charges. No competing
telemetry or cost tables are introduced.

### Temporal integration

`ProjectWorkflow` runs the `storyboard` stage after `narration` succeeds. Workflow history carries
only the versioned stage envelope - project ID, source video ID, stage name, and idempotency key -
never narration audio, script text, episode-model payloads, evidence, storyboard JSON, or provider
responses. Project statuses move through `storyboard_queued`, `storyboard_directing`,
`storyboard_retiming`, `storyboard_validating`, `storyboard_repairing`, and finally
`storyboard_complete` or `storyboard_failed`.

### CLI

Run the zero-cost deterministic director locally (no credentials required):

```bash
uv run python scripts/generate_storyboard.py PROJECT_UUID --provider fake
```

Run the configured OpenAI Responses adapter (the model stays configuration):

```bash
VIDGEN_OPENAI_API_KEY=... VIDGEN_STORYBOARD_MODEL=gpt-5.6 \
  uv run python scripts/generate_storyboard.py PROJECT_UUID --provider openai \
  --capability-profile runway-gen4-turbo
```

The command finds the selected episode model, approved script, and completed narration run, loads
the configured capability profile, runs or resumes the storyboard, and prints the storyboard run
ID, segment and shot counts, exact total measured duration, repair-attempt count, storyboard and
timing-manifest asset IDs, capability profile and hash, provider and model, cost summary, and final
status. Supply `--idempotency-key` to resume a specific run.

## T17 captions and deterministic rendering

T17 derives approved caption wording from T11 and exact word timing from the selected T12
narration, then binds the selected T13 timing manifest and every locked T16/T15 video asset into
one immutable, canonical render manifest. Integer microseconds prevent timing drift. Caption cues
are deterministically grouped at punctuation boundaries, reflowed to at most two lines, validated,
and serialized as standalone UTF-8 SRT and WebVTT (with optional escaped ASS). The standard MP4
uses a selectable `mov_text` subtitle stream.

FFmpeg commands are argument arrays constructed only from the persisted manifest. Inputs are
streamed into a contained attempt directory and hash-checked; shots are trimmed without stretching,
scaled/padded or scaled/cropped to 1920x1080, converted to square-pixel H.264 `yuv420p`, and joined
in canonical order. Hard cuts are the default; only manifest-declared crossfades are accepted.
Narration remains the time authority. Audio is resampled to stereo 48 kHz, peak-limited, measured as
structured JSON, and normalized toward -14 LUFS/-1.5 dBTP before AAC encoding. Verification checks
streams, frame rate, duration, selectable subtitles, and a complete decode. Stable manifests,
command-plan hashes, and normalized report fields—not encoder-dependent MP4 bytes—define
reproducibility. Assets retain ordered parents and lineage; completed identities are reusable without
new provider charges. Durable render/attempt/caption rows support resume and cancellation while T23
continues trace and bounded-metric propagation without fabricated provider costs.

Local entry points are `uv run python scripts/render_project.py PROJECT_UUID` and
`uv run python scripts/inspect_render.py PROJECT_UUID` once a project has completed and locked T16
outputs. T21 and later roadmap stages remain unfinished.

The required synthetic acceptance test generates ten copyright-free clips and narration with
FFmpeg at test time, persists SRT/WebVTT, renders and fully decodes a 1920x1080 H.264/yuv420p MP4
with AAC 48 kHz audio and selectable `mov_text` subtitles, persists a reproducibility report, then
proves that a second identical identity performs zero FFmpeg executions. T17 persistence extends
the existing render-job placeholder and adds render-attempt and caption-track projections in
Alembic revision `0013_render`.

## T20 semantic visual QA

T20 answers one question per generated asset: does this keyframe or clip actually show what the
approved storyboard asked for, with the approved characters and location? It is a separate
identity per target - keyframe QA and video QA have their own attempts, evidence, scores and
outcomes - and it never regenerates anything.

### Pipeline stages

Each stage lives in its own module under `services/qa/`, and the boundaries are deliberate:

| Stage | Module | Responsibility |
| --- | --- | --- |
| Authoritative input selection | `contracts.py` | Load and prove the project, selected T13 storyboard and shot, selected T14/T15 attempt, T16 identity, approved T19 versions, references, state snapshots and bundle. Every rejection is structured and non-retryable. |
| Deterministic sampling | `sampler.py` | Exact integer/rational frame selection with a recorded reason per sample. |
| Deterministic checks | `deterministic.py` | Probe, complete decode, geometry, duration, timestamps, black/freeze/flicker/exposure, plus frame-level text, face-region and style measurements. |
| Identity comparison | `identity.py` | Build the expectations for the exact approved T19 identity version. |
| Continuity comparison | `continuity.py` | Build the T13 incoming/outgoing and T19 state expectations, and the previous *passing* shot baseline. |
| Visual agent | `visual_agent.py`, `openai_adapter.py`, `fake_visual_agent.py` | The provider-neutral evaluation boundary and its two adapters. |
| Rubric and thresholds | `rubric.py` | Versioned weights, thresholds, deterministic limits and the repair-code taxonomy. |
| Score recomputation | `scoring.py` | Rebuild every weighted contribution, derive the outcome and the routing recommendation. |
| Adjudication | `adjudication.py` | The bounded second opinion and its confidence policy. |
| Evidence | `evidence.py` | Frame evidence and the contact-sheet mapping. |
| Orchestration | `pipeline.py`, `commands.py` | Restartable execution, persistence, T23 accounting and the T16 gates. |

### Rubric and thresholds

| Dimension | Weight |
| --- | ---: |
| Character identity | 25 |
| Character count | 10 |
| Location | 10 |
| Wardrobe and state | 10 |
| Action and motion | 15 |
| Composition | 10 |
| Anatomy and artifacts | 10 |
| Continuity and style | 10 |

The total is exactly 100, and the contract refuses any other total. Utility and normal shots pass
at 85 or higher; hero shots pass at 90 or higher. A score from 75 up to the applicable threshold
recommends `TARGETED_REPAIR`; below 75 the recommendation is a structural family
(`NEW_SEED`, `COMPOSITION_SPLIT` or `PROMPT_SIMPLIFICATION`) chosen from the worst-scoring
dimension. A genuinely non-applicable dimension redistributes its weight proportionally across the
applicable ones, so an absent dimension can never hand the shot free credit.

### Score recomputation and hard failures

The provider contract has no overall-score field at all. `services/qa/scoring.py` rebuilds every
dimension's `raw_score * effective_weight / 100`, sums the applicable contributions, and the
`VisualQAScore` contract re-validates that arithmetic. A hard failure - deterministic or
evidenced - forces `FAIL` regardless of the number, and both the contract and a database check
constraint refuse to record a hard failure as anything else. A provider may *propose* a hard
failure, but only a code in the bounded taxonomy that a dimension actually evidenced can block a
shot; an unevidenced proposal becomes a warning and triggers adjudication instead.

### Deterministic checks and warning thresholds

Deterministic measurement runs before any paid request, so a corrupt, mis-sized, black, frozen or
wrong-length asset is rejected without spending money. Defaults (all versioned in `rubric.py`):
more than two black frames warns and an effectively black clip hard-fails; freeze ratio above 35%
warns unless the storyboard explicitly expects stillness; duplicate-frame ratio above 50% warns;
mean inter-frame luma delta above 26 warns as flicker; unintended readable text at or above 0.80
detector confidence hard-fails; face-track continuity below 0.75 warns; style distance above 0.35
warns; duration drift over 200 ms hard-fails and drift over 150 ms warns.

The text, face-region and style measurements are documented Pillow-based proxies rather than
trained detectors. They produce a measurement; the rubric decides what it means, and a deployment
can configure a stronger representation without changing the pipeline or the thresholds.

### Sampling

Video sampling covers the first and final decodable frames, the midpoint, evenly spaced coverage,
T13 clause, action, camera-change and transition boundaries, the required action window,
measured high- and low-motion frames, frames a deterministic warning flagged, face-track
checkpoints and an OCR frame. Timestamps are exact integers in microseconds, clamped to the
measured duration, deduplicated, and ordered chronologically; each sample records why it was
selected and both its requested and actual decoded timestamp. The final sample backs off by the
configured margin or two measured frame intervals, whichever is larger, so it always lands on a
real frame. Sampling is deterministic: identical inputs and configuration always produce identical
timestamps.

### Evidence and repair codes

Every actionable finding carries a sample ID, the source-relative and shot-relative timestamps, the
frame SHA-256, the contact-sheet tile, an optional bounding box, the compared approved reference,
and a bounded explanation. A finding without evidence cannot become a hard failure unless it is a
whole-file deterministic failure such as a decode failure. The repair-code taxonomy is bounded and
every code maps to a failure category, severity, suggested T21 repair family, evidence requirement,
retryability classification and whether it is a hard failure.

### First pass, adjudication and human review

The first pass uses the configured *Luna* role and adjudication the *Terra* role. The separation is
a policy separation: an independent attempt, a different prompt, and a higher confidence bar.
Adjudication is requested when identity or continuity confidence is below 0.70, when the
deterministic report and the agent materially disagree, when a hard failure is proposed without
resolvable evidence, when the score is near the threshold, when the approved evidence is itself
ambiguous, or when a prior QA result for the same target disagrees. Terra decides only at or above
0.80 confidence; below it the outcome is `REVIEW`. Adjudication is bounded and cannot loop, and a
hard failure is never softened by low confidence. Both results persist.

Human review resolves ambiguous `REVIEW` outcomes only. It is owner-scoped, requires `If-Match`
and an `Idempotency-Key`, records the reviewer, decision, bounded reason and timestamp, emits a
project event, never changes the generated asset, never rewrites the automated result, and can
never clear a hard failure or a deterministic corruption.

### T16, T17 and T18 integration

The T16 child workflow now runs `DEFINED -> PROMPTING -> KEYFRAME_GENERATING -> KEYFRAME_QA ->
ANIMATING -> VIDEO_QA -> LOCKED`. A keyframe must pass before animation begins and a clip must pass
before the shot locks; a blocked or review-required shot stops the child with structured repair
codes and is never regenerated automatically. Only compact IDs and codes enter Temporal history,
completed QA results are reused after a worker restart, and a failed shot never touches a sibling.

T17 render eligibility requires a passing canonical video-QA result for every shot of a project
governed by the policy - a project is governed once any visual-QA run exists for it, so legacy
projects and their historical renders are unaffected. A hard failure blocks outright and a `REVIEW`
result blocks automatic completion until it is resolved. The selection helper
`visual_qa_provenance` (in `services.renderer.selection`) turns a selection into the applicable QA
result IDs, run IDs and policy version for a manifest builder to record under `provenance`; because
manifest identity excludes `provenance`, adding it leaves an existing render's identity unchanged.
This repository has no production manifest builder yet — `RenderManifest` is assembled by the T17
render tests — so the helper is currently exercised there rather than from a shipping code path.

The T18 shot inspector gains a visual-QA panel: keyframe and video outcomes, the recomputed score
and applicable threshold, a hard-failure indicator, the dimension scorecard, confidence,
deterministic diagnostics, repair codes, first-pass and adjudication status, human-review status,
evidence frames with exact timestamps beside the compared approved references, and provider, model,
cost and version details. The recommended repair family is shown as information only - there is no
button that regenerates anything. Visual-QA status also appears in the storyboard grid, the project
dashboard, the final-review eligibility banner and the project event stream.

### Cost and observability

Every production visual-agent and adjudication call goes through the existing T23 infrastructure:
a provider attempt is created or reused, cost is estimated, budget is reserved transactionally
before a durable pre-call checkpoint, usage and actual cost are reconciled afterwards, failures are
classified with the T23 taxonomy, and metrics stay bounded (rubric version, outcome, repair code -
never a project ID, shot ID, asset ID, prompt or signed URL). An idempotent retry creates no
duplicate attempt, reservation or ledger entry.

### Local commands

```bash
# Deterministic, offline, no paid credential required.
uv run python scripts/run_visual_qa.py PROJECT_UUID --provider fake

# One shot, one target, resumed by its idempotency key.
uv run python scripts/run_visual_qa.py PROJECT_UUID --provider fake \
    --shot-id SHOT_UUID --video-only --idempotency-key my-key

# The configured production provider.
VIDGEN_OPENAI_API_KEY=... uv run python scripts/run_visual_qa.py PROJECT_UUID --provider openai

# Inspect persisted results without loading media or provider payloads.
uv run python scripts/inspect_visual_qa.py PROJECT_UUID --json
```

Relevant configuration: `VIDGEN_OPENAI_API_KEY`, `VIDGEN_VISUAL_QA_FIRST_PASS_MODEL` and
`VIDGEN_VISUAL_QA_ADJUDICATOR_MODEL` (both default to the model this repository already has
configured and verified for its other agent roles), plus the usual `VIDGEN_BLOB_ROOT`,
`VIDGEN_BLOB_SIGNING_SECRET` and `VIDGEN_DATABASE_URL`.

### Troubleshooting

- `missing_canonical_video` or `missing_keyframe`: the shot has no *selected* T14/T15 attempt. Run
  T14/T15 first; T20 never generates an asset.
- `incompatible_reference_version`: the shot-reference bundle names an identity version or asset
  that is not approved. Approve the T19 reference set, which creates a new bundle.
- `asset_hash_mismatch`: the stored asset no longer matches the generation record. Regenerate the
  asset through T14/T15 rather than editing the row.
- `mixed_lineage`: the persisted canonical shot contract does not validate. The storyboard is stale;
  re-run T13.
- `identity_conflict`: an idempotency key was reused for different material inputs. Use a new key.
- A `DECODE_FAILURE` with no samples means deterministic validation proved the asset unusable, so
  no paid request was made. Fix the upstream generation.
- FFmpeg and ffprobe must be on `PATH`; every T20 media test synthesises its fixtures with them.

T20 persistence adds `visual_qa_runs`, `visual_qa_samples`, `visual_qa_attempts`,
`visual_qa_results`, `visual_qa_evidence` and `visual_qa_human_reviews` in Alembic revision
`0016_visual_qa`, the repository's single current head.
