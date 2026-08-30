# VidGen implementation status

This document is the historical record of what each roadmap task delivered. It holds the
detailed T01-T23 implementation summaries that used to live in the root `README.md`, preserved
so that the README can stay a working guide for installing, running, testing and troubleshooting
VidGen.

- The authoritative architecture and the full T01-T26 roadmap are in
  [`docs/TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md).
- Installation, local startup, tests and troubleshooting are in the root
  [`README.md`](../README.md).
- Contributor rules are in [`AGENTS.md`](../AGENTS.md).

Roadmap tasks T01 through T23 are implemented, and T24 Azure infrastructure is implemented and
reviewed but not yet validated by a real deployment. Sections below are ordered by roadmap task, and
the local commands each section documents are the per-stage commands the README's "Running
individual pipeline stages" section points at.

## Status overview

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

The complete system architecture and T01-T26 implementation roadmap are maintained in
[`docs/TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md). Contributors and coding agents should
read it together with [`AGENTS.md`](../AGENTS.md) before planning the next roadmap task.

This repository implements roadmap tasks T01 through T23, and the T24 Azure infrastructure is
implemented and reviewed but not yet validated by a real staging deployment. The pipeline runs end to
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

T21 repair and fallback routing is complete. A failed T20 video-QA result is classified into one of
five strict categories, repaired with a minimal validated prompt delta, and driven through a bounded
policy: at most two same-provider repair generations, then one Google Veo alternate-provider
generation, then a free deterministic 2.5D parallax render when the shot is eligible, and otherwise
`HUMAN_REVIEW_REQUIRED`. Every repaired or fallback output is revalidated by T20 before it can be
selected, the complete attempt lineage and its costs are persisted, and only the failed shot is
touched.

T24 Azure infrastructure is implemented and reviewed. `infra/bicep` describes a reproducible,
private-by-default staging environment: a VNet-integrated Container Apps environment with an
internal load balancer, the web, API and Temporal worker as Container Apps, finite Container Apps
Jobs for migration, rendering, smoke testing and administration, PostgreSQL Flexible Server with
VNet injection, private Blob Storage, Azure Managed Redis, Key Vault with RBAC authorisation, one
managed identity per workload with least-privilege role assignments, private endpoints and private
DNS for every linked service, Log Analytics and Application Insights carrying the existing T23
telemetry, and GitHub Actions deployment through Azure OIDC federation with no client secret
anywhere. Public ingress requires changing a single parameter; it is false in staging and in the
production template. Schema changes are applied exactly once by a dedicated migration job, before
any new revision receives traffic, and rollback restores an application revision without ever
downgrading the database. See [`infra/README.md`](../infra/README.md).

T24 is **not** marked complete on the roadmap: its acceptance - a private staging deploy, migrations
run once, workers autoscaling - can only be demonstrated by a real deployment, and no Azure
subscription has been available. Every static and CI check passes. `infra/README.md` lists what a
first deployment needs.

T22 final editorial QA is complete. The assembled T17 delivery - not the individual shots - is
inspected as a whole: its lineage is proved current, its media, audio and captions are measured
deterministically, and only then is a bounded editorial analysis bought. The completion gate is
recomputed from validated findings rather than asserted, so a project cannot reach its final
completed state without a current `PASS` for the render it actually holds, and no human action can
override a measured failure or a stale lineage.

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
- T21 repair and fallback routing: bounded failure classification, minimal validated prompt repair,
  two same-provider repairs, one Google Veo alternate-provider attempt, a free deterministic 2.5D
  parallax fallback, T20 revalidation before any selection, complete attempt lineage and costs
- T22 final editorial QA: restartable phases over the assembled render, stale-lineage rejection
  before any paid request, deterministic media/audio/caption measurement, bounded Luna/Terra
  editorial analysis, evidence-linked findings, structured remediation routing, an immutable report
  and a workflow-enforced completion gate
- T24 Azure infrastructure: Bicep modules for a private staging environment, digest-pinned images,
  one managed identity per workload, Key Vault references, a single-writer migration job, a private
  in-network smoke test with fake providers only, revision rollback, and OIDC-authenticated CI

## Foundations (T01-T04): packages and layout

- `vidgen.contracts`: canonical inter-stage contracts
- `vidgen.db`: relational models and repositories
- `vidgen.storage`: content-addressed storage and asset service
- `vidgen.providers`: provider protocols and deterministic fakes
- `vidgen.review`: T18 row versions, idempotency, project events, and workflow control
- `services.review`: T18 transactional review mutations and downstream invalidation
- `apps/web`: the T18 React review application (`@vidgen/web`)

## Foundations (T01-T04): contract schemas

Run `make schemas` to export JSON Schema into `packages/contracts/schema`.

## T05-T06 upload and deterministic media processing

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

## T07 transcription

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

## T07 subtitle-first transcript acquisition

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

## T08-T09 Temporal orchestration and scene evidence

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

## T10 Episode Analyst

This section was left empty when T10 landed. The implementation lives in `services/analysis`
(`pipeline.py`, `evidence_builder.py`, `contact_sheet.py`, the OpenAI adapter and the
deterministic fake), is exercised by `tests/test_episode_analysis_pipeline.py` and
`tests/test_episode_analysis_validation.py`, and is specified in
[`docs/TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md).

## T11 compression and comedy script pipeline

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

## T12 narration

Provider-neutral narration (fake, OpenAI, and optional ElevenLabs), deterministic 48 kHz mono PCM
normalization, bounded forced alignment, quality gates, and restartable relational checkpoints.
The stage resolves its voice from `project.settings["voice_profile_id"]`; see the README's
"Full local pipeline with fake providers" section for the local bootstrap of that profile.

## T13 storyboard generation and deterministic timing

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

T17 is the rendering *library*. Executing a render against real project inputs is T17b's
responsibility; see [T17b render execution and integration](#t17b-render-execution-and-integration)
for the commands, the lifecycle and the entry points.

The required synthetic acceptance test generates ten copyright-free clips and narration with
FFmpeg at test time, persists SRT/WebVTT, renders and fully decodes a 1920x1080 H.264/yuv420p MP4
with AAC 48 kHz audio and selectable `mov_text` subtitles, persists a reproducibility report, then
proves that a second identical identity performs zero FFmpeg executions. T17 persistence extends
the existing render-job placeholder and adds render-attempt and caption-track projections in
Alembic revision `0013_render`.

## T17b render execution and integration

### Why T17b exists

T17's roadmap row asked for a deterministic captioning and rendering library, and T17 delivered
exactly that: strict contracts, deterministic caption reflow, canonical manifests and identities,
bounded FFmpeg argument plans, media verification, immutable asset provenance and restartable
relational projections. All of it is tested. **T17 remains correctly marked complete for that
scope.**

What no roadmap row owned was the boundary between that library and the application. The seam was
real and visible in the code:

- `RenderManifest` was constructed only by tests. No production code assembled one.
- T18's `render:start` could insert a render-job row, and nothing consumed it.
- The parent Temporal workflow had no render stage at all; it went from shot fan-out straight to
  T22, which then had no current render to inspect.
- The documented worker entry point `workers/render_job/main.py` did not exist.
- The T24 Container Apps Job ran `scripts.render_project --from-env`, which created a render-job row
  and exited - so a green job execution said nothing about whether a render had happened.

T17b is the corrective task that owns that boundary. It does not rewrite T17's renderer, T18's
review UI, T20's visual QA, T21's repair pipeline, T22's editorial QA, T23's telemetry or T24's
Azure foundation.

### The canonical execution service

`services/render_execution` is one application service, and every entry point calls it:

| Module | Responsibility |
| --- | --- |
| `inputs.py` | Resolve and validate the authoritative inputs, and compute the canonical input hash |
| `manifest_builder.py` | Build the canonical caption track and the immutable `RenderManifest` |
| `claims.py` | Transactional claiming, leases, heartbeats, checkpoints and cancellation |
| `ffmpeg.py` | A cancellable, heartbeating FFmpeg executor with bounded diagnostics |
| `executor.py` | The lifecycle: claim, prepare, render, verify, persist, complete |
| `commands.py` | `queue_render_job` and `execute_render_job`, plus the current-render lookups |

There is deliberately no second implementation. The local CLI, the Temporal render activity, the
out-of-band worker and the Azure Container Apps Job all reach FFmpeg through
`execute_render_job(session, blob_store, render_job_id, ...)`.

### Authoritative input selection

Input resolution reuses `services.renderer.selection.select_authoritative_inputs` - T17's existing,
tested definition of the T11-T16 lineage - and layers the T17b concerns on top: the narration bed,
the caption configuration, the render settings, audio beds, T21 repair state, T19 reference
provenance, and a deterministic hash over all of it.

The executor rejects, always non-retryably: cross-project assets, missing or unapproved assets,
stale upstream versions, incompatible lineage, shots without a passing T20 video-QA result, active
or failed or review-blocked T21 repairs, a locked T21 repair whose selected output is not the clip
being rendered, missing narration, missing storyboard timing, unsupported render settings, a job
already held by another active executor, and a job whose stored input identity no longer matches the
resolved inputs.

The resolved selection and its hash are persisted on the render job **before** any FFmpeg work
begins. Identical inputs and configuration produce the same hash, the same manifest and the same
render identity; a materially different input produces a new identity and therefore needs a new
render job.

No upstream stage produces music or sound-effect assets yet. The resolution path for them exists and
is bounded by the canonical timeline, so a bed that would overrun the picture is refused rather than
silently trimmed - but today every render is narration-only.

### The production manifest

`manifest_builder.py` converts the resolved selection into T17's existing contract without weakening
any of its validation. Timing is taken as integer microseconds straight from T13's canonical timing
manifest, so a long timeline cannot accumulate binary floating-point drift. Storage is resolved
through `AssetService`: the manifest references assets by ID and content hash, and a temporary
signed URL is never part of stable identity.

### Caption production

The deliverable caption track is built by T17's `build_caption_track` from the approved script's
words and T12's measured narration timings, through the same projection T22 uses to reconstruct
captions independently. The SRT, WebVTT and (for burn-in modes) ASS assets are persisted through
`AssetService` **before** the manifest is built, because the manifest references them by hash. Their
idempotency keys are derived from the input identity, so an interrupted run reuses the caption
assets it already produced rather than writing a second copy. T22's caption reconstruction remains a
verification of this track, never its source.

### The render-job lifecycle

```
render_queued -> render_claiming -> render_preparing -> render_manifest_ready
              -> render_rendering -> render_verifying -> render_persisting -> render_complete
                                                       \-> render_failed | render_cancelled
```

A completed render job references its canonical manifest, caption assets, final MP4, verification
report, input hash, output SHA-256, exact measured duration, stream metadata, renderer and FFmpeg
versions, completion timestamp and trace identity. The job is marked complete only after FFmpeg
exited successfully, verification passed, every output is stored through `AssetService`, all
database references are committed, and the final asset reads back through normal asset storage. The
table's own constraints enforce the asset references and the measurements.

### Claims, leases and recovery

Concurrency safety is a conditional `UPDATE`, not a process-local lock: exactly one concurrent
caller wins, a live lease belongs to its holder, a heartbeat extends it during a long encode, a
stale lease is reclaimable after its timeout, attempts are bounded, and a completed job is reused
rather than re-executed. A worker that dies leaves the job reclaimable rather than permanently
locked.

A run that persisted every output but died before completing is recovered without re-encoding, when
- and only when - the stored outputs belong to the same manifest identity and are still readable. A
run that died earlier resumes from its durable checkpoint: the caption assets and the manifest
identity are reused, and no duplicate asset rows are created.

### Temporal integration

The parent workflow gains a render stage between the shot fan-out and T22, on the dedicated `render`
task queue named by the design, so a CPU-bound encode never competes with provider-bound activities
for the project worker's bounded concurrency. The message is IDs only: manifests, captions, media
bytes, FFmpeg output and diagnostics are not representable in `RenderActivityInput` or
`RenderActivityResult`. The activity queues or resumes the project's render job itself, so an
activity retry re-enters the same job rather than forking a second one. T22 does not start unless
the render completed and its final asset is persisted.

### T18 integration

`render:start` queues a job whose input identity is already stamped, so a worker can execute it
without anyone constructing a manifest by hand. When the project cannot currently render, the job is
still queued with the structured refusal recorded on it, because the review UI needs to show why a
render is blocked. The render projection carries real progress, checkpoint, attempt count,
cancellation and failure state, plus a `downloadable` flag that is true only for a complete,
verified, non-stale render with a stored final asset.
`GET /api/v1/projects/{id}/render/download` resolves that asset through the existing signed-download
mechanism; an incomplete, failed or stale render is a `409`, never a download.

### T22 integration

T22 resolves the project's current render through the one shared lookup
(`services.render_execution.commands.current_render_job`), so the dashboard and final QA can never
disagree about which render is current. A completed T17b render is selected on completion, and T22
evaluates exactly that render, by asset and by hash. The completion gate is bound to the render
asset it was decided for, so a rerender invalidates an older decision rather than inheriting it.

### T24 Container Apps Job

The job now runs `python -m workers.render_job.main --from-env`, with `VIDGEN_RENDER_JOB_ID`
supplied per execution by `az containerapp job start --env-vars`. It executes an existing render job
and its exit status is the render result. Managed identity, Key Vault secrets and the existing
storage and database access are unchanged, and no static cloud credential is introduced.
`VIDGEN_RENDER_WORK_ROOT` points at the writable `/tmp/vidgen` the image creates for its non-root
user. Automatic dispatch of that job is deliberately absent: the in-workflow Temporal activity is
the intended production path, and the job exists for the finite out-of-band case.

### Local commands

```bash
RENDER_JOB_ID="$(uv run python -m scripts.render_project PROJECT_UUID --output-id-only)"
uv run python -m workers.render_job.main --render-job-id "$RENDER_JOB_ID"
uv run python scripts/inspect_render.py PROJECT_UUID
uv run python scripts/run_final_editorial_qa.py PROJECT_UUID --provider fake
```

`uv run python -m scripts.render_project PROJECT_UUID --execute` queues and executes in one process.
The worker also accepts `--from-env` and a bounded `--poll` loop, handles `SIGTERM`/`SIGINT` by
requesting cancellation, and exits `0` for a completed render or an idempotent reuse, `1` for a
failure, `2` for a usage error and `3` for a cancellation. None of this requires Azure or a paid
provider credential.

### Observability

T17b records bounded telemetry through the existing T23 stack: structured log events for queue,
claim, manifest, captions, FFmpeg start, verification, persistence, completion, failure,
cancellation and lease recovery; the render real-time factor and verification outcome as metrics;
and per-phase wall-clock timings plus tool versions on the render-attempt row. FFmpeg is local
compute, not a paid provider call, so no provider charge is fabricated. Signed URLs, credentials,
caption text, script text, media bytes and unbounded FFmpeg output are never logged.

### Known limitations

- Music and sound-effect beds resolve and validate, but no upstream stage produces them, so every
  render today is narration-only.
- A failure between FFmpeg finishing and the outputs committing re-renders on retry. The output is
  deterministic and content-addressed, so no duplicate asset is created, but the CPU is spent again.
  Recovery without re-encoding only applies once the outputs are committed.
- Crossfade transitions remain refused by T17's `validate_transitions`; hard cuts only.
- The Container Apps Job is not dispatched automatically; see above.

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

## T19 continuity references

See the T19 paragraph in the status overview above. The implementation lives in
`services/continuity` and `packages/workflows/continuity.py`, with acceptance coverage in
`tests/test_t19_acceptance.py` and `tests/test_continuity_t19.py`.

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
T17b's production manifest builder is the shipping caller: every render it assembles records the
applicable QA result IDs, run IDs and policy version under `provenance`.

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
`0016_visual_qa`.

## T21 repair and fallback routing

T20 decides whether a shot passes. T21 decides what to do about a shot that did not, and it does
so within a policy that cannot spend unbounded money. It consumes one failed T20 result, classifies
why the shot failed, and drives the safest, least expensive valid recovery path to a terminal
state: a revalidated, selected output, or `HUMAN_REVIEW_REQUIRED`.

T21 never reinterprets a T20 decision. The score, the applicable threshold and the hard-failure
flag are read from the persisted T20 report and copied through unchanged; the only thing derived
from them is *how much* has to change.

### Architecture

Each concern lives in its own module, and the boundaries are deliberate:

| Module | Responsibility |
| --- | --- |
| `services/qa/repair_classifier.py` | Why the shot failed, from T20 evidence alone |
| `services/qa/repair_planner.py` | The minimal prompt delta, and its deterministic validation |
| `services/qa/repair_policy.py` | The bounded route order and its attempt ceilings |
| `services/qa/repair.py` | Restartable orchestration, persistence, spend and revalidation |
| `services/animation/veo.py` | Every Veo model, capability and price, in exactly one place |
| `services/animation/veo_adapter.py` | The provider-neutral alternate-provider boundary |
| `services/animation/veo_fake.py` | A deterministic, credential-free stand-in |
| `services/renderer/parallax_manifest.py` | Deterministic 2.5D planning and eligibility |
| `services/renderer/parallax.py` | The FFmpeg execution of one plan |

### Failure classification

Every failed shot is classified into exactly one of five strict categories - `prompt_issue`,
`reference_issue`, `seed_issue`, `provider_issue`, `impossible_shot` - with a decisive diagnostic
code drawn from a bounded taxonomy. The classifier distinguishes wrong character identity, wrong
character count, wrong location, wrong wardrobe or character state, missing or incorrect action,
weak motion, composition failure, anatomy or visual-artifact failure, continuity failure, style
mismatch, prompt overconstraint, prompt ambiguity, reference conflict, provider safety rejection,
provider timeout or service failure, unsupported provider capability, impossible duration or motion
request, and corrupt or incomplete downloaded media.

Every T20 repair code maps to exactly one diagnostic; an unmapped code raises rather than being
silently ignored, so the two taxonomies cannot drift apart unnoticed. Deterministic input, lineage,
configuration and media-processing failures are marked `deterministic_only`, and the router is
forbidden from spending money on them.

### Score-aware severity

The bands mirror T20 exactly:

| T20 score | Severity |
| --- | --- |
| at or above the applicable threshold (85 normal, 90 hero) | not repaired; T20 already passed it |
| 75 to below the threshold | targeted repair |
| below 75 | structural repair - simplification, a new seed, reduced composition complexity |

A hard failure always fails regardless of score, so it always earns at least a targeted repair, and
a hard failure in a categorical dimension - the wrong person, the wrong place, the wrong number of
people - is always structural.

### The bounded repair policy

```
T20 QA failure
-> classify failure
-> same-provider repair 1        -> T20 QA
-> same-provider repair 2        -> T20 QA
-> one alternate-provider attempt -> T20 QA
-> deterministic 2.5D fallback when eligible -> T20 QA
-> otherwise HUMAN_REVIEW_REQUIRED
```

The original failed generation is recorded as attempt ordinal `0` and is **not** one of the two
repair attempts. One shot therefore costs at most one original generation, two same-provider repair
generations, one alternate-provider generation, and one deterministic fallback render, which costs
no provider charge at all. There is no loop back and no unbounded retry: `repair_attempts` caps its
ordinal in the database, and the route order is a total function of the attempts already spent.

Network polling, media download, normalization and storage retries are not routes. When a durable
provider operation already exists the router returns `resume_provider_operation`, which consumes no
attempt and starts no new paid generation. An attempt interrupted between persisting its clip and
running T20 on it is revalidated on resume rather than regenerated.

### Minimal prompt repair

A repaired prompt is a delta, never a rewrite. `extract_constraints` derives the ordered, stably
identified constraints the original prompt asserted - required identities, character count,
appearance and state, location, action, camera framing and movement, shot timing, continuity,
approved T19 reference bindings, safety limits, provider capability limits, negative constraints and
style - and marks which of them may move. Only the clauses a diagnostic actually implicates are
touched; every other constraint is carried through and listed by name.

Some constraints are enforced by the request rather than by prose. Shot timing, the capability
profile and the reference bundle are carried by the request's duration parameter, its capability
validation and its attached reference assets, so they are preserved and recorded but not written
into the prompt text.

The persisted `PromptDelta` records added, removed and rewritten clauses, preserved and touched
constraint IDs, the repair reason and codes, the source QA finding IDs, the before and after prompt
hashes, the seed change, and the planner version. `validate_delta` runs for *every* planner before
any provider request: a delta that drops a required constraint, touches an immutable one, edits a
clause its diagnostic never mentioned, or whose hash does not match the rendered prompt is rejected
and costs nothing.

`DeterministicRepairPlanner` is the default and the test planner; the same classification always
produces the same delta. `LanguageModelRepairPlanner` is the production option: a configured model
may *propose* a `RepairPlannerProposal`, and its proposal goes through exactly the same
deterministic validation. There is no path from model prose to a provider request, and the bounded
brief the model receives contains only the editable clauses.

### Reference failures

T21 never mutates an approved T19 character or location reference. When T20 evidence shows the
approved reference itself is missing, stale, incompatible or in conflict, the shot is classified as
a `reference_issue`, the evidence explaining the conflict is preserved, no replacement is
fabricated, and the run is routed to `upstream_reference_correction` and `HUMAN_REVIEW_REQUIRED`.
Money is never spent regenerating against a reference already known to be invalid. A generation that
merely *failed to follow* a valid reference is a prompt issue and still earns its targeted repairs.

### Attempt lineage

Every attempt links to its immediate predecessor and to the root generation, and records the shot,
the attempt ordinal and kind, the provider and model, the provider task or operation ID, the prompt
hash and delta, the seed, the reference asset IDs and hashes, the capability-profile hash, the
repair classification and code, the source QA result, the output assets, the output QA result, the
estimated and actual cost, the status, the failure classification, trace context, and the created,
started and completed timestamps.

Attempt identities hash the ordinal together with every material input, so two attempts cannot
collide. Only one attempt per shot may be `selected`, the database enforces it with a partial unique
index, and selection is impossible without the attempt's own passing T20 result. Previous attempts
stay in place as immutable historical records.

### Google Veo as the alternate provider

Every Veo fact lives in `services/animation/veo.py`: the supported model IDs, the fast and quality
variants, durations, aspect ratios, resolutions, image-to-video, first- and last-frame control,
reference-image support, native-audio behaviour, regional availability, request limits, polling
behaviour and pricing identifiers. No other module names a Veo model.

Capabilities are versioned and every attempt persists the profile hash it was generated under.
Capability enforcement reads the profile, never the family: `veo-3.0-fast-generate-001` has no
last-frame or reference-image control, and handing it one raises before any paid call rather than
being silently dropped. The cost-controlled default is `veo-3.1-fast-generate-001`. Veo offers a
small set of whole-second durations, so a shot whose canonical length is not one of them is
generated at the smallest covering duration and trimmed by the same deterministic T15 trimmer.

The adapter drives Vertex AI's long-running operation correctly. The operation name is persisted in
the same transaction as the pre-call checkpoint, before the first poll, so an interrupted worker
resumes the operation it already paid for. A transport failure that leaves the submission outcome
unknown raises `VeoSubmissionAmbiguous`, which is routed to human review and **never** resubmitted.
Output is streamed to temporary storage in bounded chunks, validated with ffprobe, normalized and
trimmed through the existing T15 media infrastructure, and stored through `AssetService` with full
provider and model provenance. Credentials, prompts, signed URLs and raw provider payloads are never
logged or persisted.

Veo 3 models generate native audio. T17 remains responsible for combining final visuals with
approved narration, so T21 requests silent video wherever the model allows it.

The application and the deterministic test suite work with no Google credentials, and no test or CI
run makes a paid Veo call.

### Deterministic 2.5D parallax fallback

When every paid attempt is spent, a shot whose purpose can be conveyed through controlled
still-image motion falls back to a deterministic 2.5D render of its approved keyframe. It needs no
provider, no credential and no network.

Layer transforms are derived from a stable render identity over the shot, the source asset and its
hash, the geometry and the canonical duration, so the same shot always produces the same camera move
and two shots never accidentally produce the same one. A second plane is added only when a real mask
or depth map exists; without depth separation the render stays an honest single-plane camera move.
The move is large and its easing is linear on purpose: a gentle eased move is nearly static at both
ends, and T20's deterministic freeze check counts exactly those frames.

The renderer invokes FFmpeg and FFprobe through argument arrays, never a shell command string, and
never loads a whole video into memory. Output is repository-standard H.264 `yuv420p` MP4 at the
shot's exact canonical duration, pinned by the same trimmer T15 applies to provider output. A
versioned manifest recording every input asset, every transformation, the filter graph, both
argument arrays and both tool versions is persisted as its own asset.

Eligibility is decided deterministically and conservatively. A shot is rejected when it needs
motion a 2.5D render cannot truthfully represent - essential complex character interaction or a
mandatory physical action - and when its source keyframe already carries a T20 hard identity or
location failure. Parallax is never used to conceal an invalid source image.

### Budget and cost limits

Every paid repair generation goes through the existing T23 infrastructure: a durable provider
attempt is created or reused, cost is estimated from the active pricing catalog, budget is reserved
transactionally, the pre-call checkpoint is persisted, and trace context is propagated. After
completion the provider operation identity and actual usage are recorded, the reservation is
reconciled and any unused amount released, failures are classified with the T23 taxonomy, and
bounded, redacted metrics are emitted.

There is no hard-coded numeric repair budget. The project's configured T23 hard cap is the money
limit, with optional configured per-shot and per-repair-run limits on top. If the next attempt would
exceed an applicable hard limit the provider is not called at all: the shot is routed to
`HUMAN_REVIEW_REQUIRED` with a structured budget reason. Idempotent retries create no duplicate
provider attempts, reservations, reconciliation events, ledger entries or charges.

### Temporal integration

A failed T20 video QA result starts or resumes T21 for that shot through the `run_shot_repair`
activity. The child workflow adds `REPAIR_PLANNING`, `REPAIRING`, `ALTERNATE_PROVIDER`,
`FALLBACK_RENDERING`, `REVALIDATING`, `HUMAN_REVIEW_REQUIRED` and `REPAIR_FAILED` alongside the
existing states, and only reaches `LOCKED` when the selected output has passed its own T20
evaluation.

Messages are compact, versioned and ID-only in both directions. Prompts, QA evidence, video bytes,
provider responses, fallback manifests and image payloads are not representable in the contracts the
workflow passes. Activity idempotency keys are stable and stage-isolated, existing provider
operations are resumed rather than resubmitted, cancellation is honoured between paid attempts, and
a repair touches only the failed shot: siblings keep their locked results and their passing QA.

Concurrent workers cannot advance the same repair run twice. `repair_runs.advance_token` is taken
with a conditional update, so the loser is told to re-read rather than driving the same route again.

T17 consumes only the selected passing animation output.

### API and dashboard

The owner-scoped control plane exposes the repair runs for a project and for a shot, and a detail
projection with the failure classification, the T20 score and hard-failure reason, the repair code,
the full attempt lineage with provider and model per attempt, prompt changes as a structured delta,
output asset and QA-result links, estimated and actual cost, the current budget state, the fallback
render and the human-review reason. Projections carry no prompts, provider payloads or signed URLs.

`POST .../repairs/{id}:act` records one owner decision: `retry` resumes a durable technical
operation (never a new paid generation), `cancel` stops the run before the next paid attempt,
`acknowledge` and `resolve` record a human decision on a review state, and
`restart_after_reference_correction` reopens a run whose upstream reference has been corrected. No
action can mark a hard-failing visual as passed - selection requires a new valid T20 result, and only
a repair attempt can produce one. The T18 shot inspector renders all of it in a repair panel beside
the existing visual-QA panel.

### Local commands

Fake mode is fully deterministic and needs no credential:

```bash
uv run python scripts/run_visual_repair.py PROJECT_UUID SHOT_UUID     --provider fake --alternate-provider fake
```

Supported flags are `--provider`, `--alternate-provider`, `--idempotency-key`,
`--max-same-provider-repairs`, `--allow-parallax-fallback` / `--no-parallax-fallback`, `--resume`,
`--per-shot-repair-cost-limit`, `--width`, `--height`, `--qa-provider` and `--json`. The command
prints the repair-run ID, the root animation-attempt ID, the current attempt and route, the failure
classification and repair code, the provider and model, the provider operation ID where one applies,
the QA score and decision, the selected output asset ID, the attempt lineage, a cost summary and the
final status.

The production command uses the configured providers:

```bash
RUNWAYML_API_SECRET=... VIDGEN_GOOGLE_CLOUD_PROJECT=... VIDGEN_GOOGLE_ACCESS_TOKEN=... VIDGEN_OPENAI_API_KEY=... VIDGEN_DATABASE_URL=... VIDGEN_BLOB_ROOT=... VIDGEN_BLOB_SIGNING_SECRET=... uv run python scripts/run_visual_repair.py PROJECT_UUID SHOT_UUID     --provider runway --alternate-provider veo --qa-provider openai
```

Relevant settings are `VIDGEN_REPAIR_ALTERNATE_PROVIDER` (`none` by default, `veo` to enable the
single bounded alternate-provider attempt once Google credentials are configured),
`VIDGEN_REPAIR_MAX_SAME_PROVIDER_REPAIRS`, `VIDGEN_REPAIR_ALLOW_PARALLAX_FALLBACK`,
`VIDGEN_REPAIR_PER_SHOT_COST_LIMIT`, `VIDGEN_VEO_MODEL` and `VIDGEN_VEO_LOCATION`.

### Known limitations

- The Veo capability profile and pricing are verified against Google's published documentation on a
  recorded date. Google has changed Veo model IDs and per-second prices more than once; re-check the
  official documentation before changing a value, because a stale entry silently mis-estimates every
  alternate-provider repair.
- Without a mask or depth map the fallback is a single-plane camera move rather than true parallax.
  The repository does not yet generate depth maps.
- The deterministic planner's repair clauses are English and tuned for the current prompt compiler;
  a project in another language should configure the language-model planner, whose output still
  passes the same deterministic validation.
- Routing to `HUMAN_REVIEW_REQUIRED` on a budget denial happens even when the free deterministic
  fallback is still available, because the policy requires a structured budget stop.
- The alternate-provider route is off by default. An unconfigured deployment keeps its
  same-provider repairs and the free 2.5D fallback rather than failing every repair on a missing
  Google credential; set `VIDGEN_REPAIR_ALTERNATE_PROVIDER=veo` to turn it on.
- The Veo adapter reads inline output only. T21 never sets `storageUri`, so a Cloud Storage handle
  in a completed operation is refused rather than fetched with a client this adapter does not have.
- FFmpeg and ffprobe must be on `PATH`; every T21 media test synthesises its fixtures with them.

T21 persistence adds `repair_runs`, `repair_attempts`, `repair_decisions`,
`repair_fallback_renders` and `repair_veo_operations` in Alembic revision `0017_repair_fallback`.

## T22 final editorial QA

T20 asks whether one shot is right. T22 asks whether the *finished recap* is right, and it is the
last gate before a project may complete. It inspects the canonical T17 delivery as a whole, so it
sees the class of defect no shot-level result can: a concatenation seam, a caption file that lost
its second half, an audio track that drifted two seconds from the picture, a story beat that never
made it onto the screen.

The design rests on one rule: **deterministic truth outranks semantic opinion.** A decode failure,
a stale lineage, a missing caption cue or a drifting mix is a measured fact. No provider score, no
averaged dimension and no human decision can turn one into a `PASS`.

### Architecture and phase boundaries

T22 runs as six restartable phases, each with its own checkpoint and stable idempotency identity:

```text
INPUT_VALIDATION → DETERMINISTIC_MEDIA_QA → CAPTION_QA → EDITORIAL_ANALYSIS
                 → ADJUDICATION (when necessary) → COMPLETION_GATE
```

Separation is enforced by module, not by convention:

| Module | Responsibility |
| --- | --- |
| `services/qa/final_inputs.py` | Authoritative input selection and stale-lineage rejection |
| `services/qa/final_deterministic.py` | Measured media checks on the assembled render |
| `services/qa/final_audio.py` | Measured checks on the delivered final mix |
| `services/qa/final_captions.py` | Canonical manifest and delivered caption-asset checks |
| `services/qa/final_evidence.py` | Deterministic sampling, contact sheet and evidence assembly |
| `services/qa/final_editorial_provider.py` | The provider-neutral editorial interface and registry |
| `services/qa/final_fake_provider.py` | The deterministic fake used by every test and by CI |
| `services/qa/final_openai_adapter.py` | The configured production structured-output adapter |
| `services/qa/final_gate.py` | Finding recomputation, remediation routing and the gate |
| `services/qa/final_editorial.py` | Restartable orchestration and persistence |
| `services/qa/final_human_review.py` | Bounded human adjudication of uncertain findings |

A completed phase is reused whenever its inputs are unchanged. Because the final-QA identity binds
every material input, a phase recorded against a run was computed from exactly those inputs, so a
resumed run reattaches to its checkpoints instead of re-deriving them - and never re-stores an
artefact it already wrote.

### Canonical inputs and lineage rules

T22 does not re-derive the render. It loads the current selected T17 render and its manifest, then
proves the manifest still describes the project's current state. The run is rejected - before any
paid provider request, and non-retryably - when:

- an asset belongs to another project,
- any required upstream output is missing,
- the render was produced from a superseded selected animation, script, narration or storyboard,
- a selected shot has no passing T20 video-QA result,
- a T21 repair run is active, failed or `HUMAN_REVIEW_REQUIRED`,
- the render does not contain the selected passing T21 attempt for a repaired shot,
- a declared narration, storyboard, caption or render hash does not match the database,
- shot ordering or an asset reference differs from the render manifest,
- the final asset is empty or inaccessible.

The answer to any of these is a new render, never a second evaluation of the same stale one.

### Deterministic render checks

FFmpeg and ffprobe are invoked exclusively through subprocess argument arrays; no command string is
built from a manifest, an asset name or any other externally supplied value. The checks cover
existence and size, container and codec, full video and audio decode, expected streams, resolution,
pixel format, frame rate and time base, finite positive duration, video/audio/timeline duration
agreement, A/V drift, shot coverage without gaps or overlaps, transition handles, unexpected black
and excessive frozen intervals, corrupt sections, first and final frame decode, monotonic
timestamps, start offset, non-finite measurements, and delivery byte rate and bitrate.

These operate on the *assembled* output, so they detect damage introduced during concatenation,
filtering, trimming, captioning, mixing or encoding. T20's shot-level scoring is neither reused nor
altered; what is reused are the repository's existing measurement utilities.

### Audio checks and thresholds

Loudness and true peak come from `loudnorm`; peak, RMS and clipped-sample counts from `astats`;
silence from `silencedetect`. Narration coverage is checked by intersecting the approved T12 word
timings with the measured non-silent intervals of the delivered mix, so an omitted, duplicated or
drifting segment is located by an exact global timestamp range rather than inferred from a score.

Defaults: integrated loudness -14 LUFS ±1.5 LU, true peak ceiling -1.0 dBTP, clipping ratio
≤0.0005, leading silence ≤1.5 s, trailing silence ≤2.5 s, internal silence inside narration ≤2.0 s,
narration timing tolerance 250 ms, minimum narration headroom over music and effects 6 dB, and a
48 kHz stereo delivery profile. A mix with nothing in it reports non-finite loudness; that is a
measurement outcome, not a crash, and narration coverage does the blocking.

### Caption checks and thresholds

Two things are validated and never confused: the canonical caption track the manifest declared, and
the selectable caption files that were actually delivered. A cue that is correct in the manifest but
missing from the delivered SRT is a blocking failure, because the viewer sees the delivered file.

Checks cover narration coverage, text fidelity against the approved script projection, monotonic
ordering, nonnegative starts, positive durations, in-bounds cues, unintended overlaps, timing
alignment with T12 word timings, missing and duplicated cues, delivered asset hashes, line count
(≤2) and line length (≤42 characters), reading speed (≤21 cps), deterministic reflow against the
declared caption identity, punctuation preservation, valid UTF-8, successful parsing, burned-in safe
area, and delivered language metadata. Caption QA never rewrites the approved script; it reports the
mismatch and names the repair target.

Only the selectable formats are parsed for cues. A burned-in ASS asset is a rendering input rather
than a file a viewer selects, so it is verified by hash and by the safe-area check but never fed to
a cue parser. The delivered cues are carried in an unvalidated record rather than a `CaptionTrack`:
that contract rejects overlapping and out-of-duration cues, which are exactly the defects caption
QA exists to report.

### Editorial dimensions, findings and evidence

After deterministic checks permit it, a bounded structured analysis evaluates the assembled recap
across 21 dimensions: story-beat coverage, narrative structure, scene completeness, character
identity and state continuity, location continuity, prop and wardrobe continuity, visual
contradictions, shot-to-shot continuity, transition coherence, narration-to-visual and
caption-to-narration agreement, comprehensibility, setup and payoff, narrative jumps, repetition,
pacing, dead air, ending completeness, and contradiction of the approved script or approved source
evidence.

T22 evaluates the assembled recap; it does not rescore individual shots as if it were T20. Findings
are classified `blocking`, `review_required`, `warning` or `informational`, and severity is
structural rather than derived from an average - **a high dimension score can never conceal a
blocking finding.** Every finding carries a stable ID, category, severity, blocking flag,
confidence, structured issue code, summary, exact global timestamp range, affected shot, script,
narration and caption-cue references, evidence, expected and observed behaviour, a remediation
target and its provenance. Findings are truncated most-severe-first against a strict bound, and
unrestricted model prose is never stored.

### Luna, Terra and human review

Luna performs the first pass on the configured inexpensive vision model. Terra adjudicates
borderline, contradictory or low-confidence findings on the stronger model, and **may only decide at
confidence ≥ 0.80**; below that the disagreement becomes `REVIEW`, which is the correct outcome for
a genuinely uncertain editorial question. A provider score is never canonical: a reply that answers
a different attempt, scores a dimension it was not asked about, cites an unsampled frame or a shot
outside the render, or leaves an actionable claim without evidence is a non-retryable provider
contract failure.

A human reviewer may resolve a semantic `REVIEW` finding only when no deterministic hard failure
and no stale lineage exist, and only with a structured reason recorded against a row version under
optimistic concurrency. A reviewer can never override corrupt media, stale lineage, a missing
required asset, invalid timestamps, missing caption or narration coverage, a failing T20 hard
result, or an unresolved T21 human-review state. Accepting a review finding can turn `REVIEW` into
`PASS`; nothing can turn `FAIL` into anything.

### Completion gate

`PASS` is computed, never asserted. It requires current compatible lineage, successful deterministic
media, audio and caption checks, no blocking editorial finding, no unresolved T21 human review, no
missing passing T20 result, no unresolved final editorial review, and a persisted report and gate
decision. `FAIL` is required when any blocking issue is confirmed; `REVIEW` when a semantic question
remains genuinely uncertain after bounded adjudication.

The gate is workflow state, not a UI affordance: the parent workflow advances a project to
`completed` only on `PASS`, the database refuses to store a `PASS` alongside a blocking finding or an
unresolved review, and the API exposes no action that marks a deterministic failure as passed.

### Remediation routing

T22 identifies and gates; it never starts another creative repair loop and never makes a paid
generation call. Confirmed findings are grouped into structured routes - `RERENDER_T17`,
`REBUILD_CAPTIONS_T17`, `REMIX_AUDIO_T17`, `REGENERATE_SHOT_T16`, `REPAIR_SHOT_T21`,
`CORRECT_REFERENCE_T19`, `CORRECT_SCRIPT_UPSTREAM`, `HUMAN_EDITORIAL_REVIEW` - which the parent
workflow or an authorized user action hands to the stage that already owns that repair. Only
affected downstream outputs are invalidated; unaffected shot selections and every historical render
and QA asset are preserved. Any route that changes a selected input requires a new T17 render and a
new T22 run against the new render identity; a report is never reused for an older render.

### Temporal integration

The parent project workflow runs T22 after the shot fan-out reports every required shot eligible for
assembly. The message is IDs only - project, render and manifest asset, run ID, idempotency key and
trace context - so reports, findings, sampled frames, caption text, media bytes and provider
payloads never enter workflow history. Progress is query-visible through `final_qa_state`, and the
workflow-visible statuses are `FINAL_QA_QUEUED`, `FINAL_QA_VALIDATING_INPUTS`,
`FINAL_QA_CHECKING_MEDIA`, `FINAL_QA_CHECKING_CAPTIONS`, `FINAL_QA_ANALYZING`,
`FINAL_QA_ADJUDICATING`, `FINAL_QA_REVIEW_REQUIRED`, `FINAL_QA_PASSED` and `FINAL_QA_FAILED`.

### T23 cost and telemetry integration

Every production editorial call reuses the existing T23 infrastructure: a durable provider-attempt
identity, a cost estimate, a transactional budget reservation under the project hard cap, a durable
pre-call checkpoint, propagated trace context, then recorded usage, reconciliation, released
reservation, bounded metrics, taxonomy-classified failures and redacted metadata. Deterministic
checks create no provider charges, and an idempotent retry duplicates no attempt, reservation,
reconciliation, ledger entry or cost. A budget denial stops the run without a decision, so nothing
completes on that render.

### API and dashboard

`GET /api/v1/projects/{id}/final-qa` lists runs, `GET .../final-qa/{run_id}` returns measurements,
checks, dimensions, findings and remediation routes, and `GET .../final-qa/gate` returns the
backend's own completion answer. `POST .../final-qa:run`, `:cancel`, `:review` and `:remediate`
carry `If-Match` and `Idempotency-Key` under existing owner scoping. The final-review page renders
the gate verdict, phase and status, render and audio measurements, caption results, editorial
dimensions, a timeline marker per finding with its evidence and affected shots or cues, the
recommended remediation target, provider, model, cost and report identity. Cancellation is offered
only before a paid analysis, and no control marks a deterministic failure as passed.

### Local commands

```bash
uv run python scripts/run_final_editorial_qa.py PROJECT_UUID --provider fake
VIDGEN_OPENAI_API_KEY=... uv run python scripts/run_final_editorial_qa.py PROJECT_UUID \
    --provider openai
```

Fake mode is fully deterministic and needs no provider credential. The command prints the
final-editorial run ID, the final-render asset ID, the input identity, deterministic check counts,
audio and caption results, blocking/review/warning counts, provider and model, the adjudication
result when one was used, the cost summary, the report asset ID, the gate decision and the final
status.

Production configuration: `VIDGEN_OPENAI_API_KEY`, `VIDGEN_FINAL_QA_FIRST_PASS_MODEL`,
`VIDGEN_FINAL_QA_ADJUDICATOR_MODEL` and `VIDGEN_FINAL_QA_ADJUDICATION_ENABLED`. Both model defaults
are the model this repository already has configured and verified for its other vision agent roles;
check the provider's current official documentation before changing one.

### Known limitations

- Editorial analysis reads deterministically sampled frames and a contact sheet, not the moving
  picture. A defect that is only visible in motion between two sampled timestamps - a single
  glitched frame, for instance - is caught by the deterministic checks or not at all.
- Story-beat coverage is judged against the approved script structure supplied in the request. The
  request carries a beat summary rather than the full plot plan, so a project whose beats are
  encoded only in prose gets a weaker signal than one with structured beats.
- Narration coverage is measured from silence intervals, which detects an absent or displaced
  segment but not one replaced by different speech at the right time. Text-level narration fidelity
  remains T12's responsibility.
- The audio masking check reads the manifest's declared ducking and gain rather than measuring
  narration intelligibility against the bed, because a reliable intelligibility measure needs the
  stems, which the delivery does not carry.
- Single-pass loudness normalization lands within roughly 1 LU of target, so the delivery tolerance
  is a configured value rather than an exact match.
- Cancellation is only meaningful before the editorial phase. Once the provider request is issued
  the cost is already committed, and the run completes rather than abandoning what was bought.
- FFmpeg and ffprobe must be on `PATH`; every T22 media test synthesises its fixtures with them.

T22 persistence adds `final_editorial_runs`, `final_editorial_checks`,
`final_editorial_provider_attempts`, `final_editorial_reviews` and `final_completion_gates` in
Alembic revision `0018_final_editorial_qa`, the repository's single current head.

## T23 observability and cost ledger

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

## T24 Azure infrastructure

`infra/` holds the authoritative infrastructure-as-code for a VidGen deployment. The complete
operational reference - architecture, resource inventory, network topology, parameters, RBAC, Key
Vault secret names, autoscaling rationale, observability, deployment and migration order, smoke
testing, rollback, backup and recovery, cost drivers and known limitations - is in
[`infra/README.md`](../infra/README.md).

```text
infra/
  bicep/
    main.bicep                 resource-group scoped composition
    modules/                   identities, networking, private_dns, container_registry,
                               container_apps_environment, container_apps, container_jobs,
                               postgres, storage, redis, key_vault, monitoring,
                               role_assignments, plus shared types and app configuration
    environments/
      staging.bicepparam                 the deployed environment
      production.example.bicepparam      a documented, undeployed template
  tests/                       compiled-template policy tests, run by `uv run pytest -q`
  scripts/                     bootstrap_oidc, bootstrap_secrets, validate_infrastructure,
                               scan_secrets, deploy_staging, run_migrations, run_smoke_test,
                               verify_deployment, rollback_revision
  dashboards/vidgen-workbook.json        Azure Monitor workbook over the T23 signals
  README.md
```

Staging is private by default. `publicIngressEnabled` is `false`, which makes the Container Apps
environment internal and disables public network access on Key Vault, Blob Storage and Redis;
PostgreSQL is VNet injected and has no public endpoint at all. The API is never externally
reachable, whatever that parameter is set to, because production authentication does not exist yet.

Deployment order is enforced by `.github/workflows/deploy-staging.yml`: build and push
commit-SHA-tagged images, resolve their digests, validate and `what-if`, wait for the protected
GitHub environment approval, deploy infrastructure, run the migration job exactly once and wait for
it, activate the new revisions, run the private smoke-test job, verify health and the deployed
digests, and roll application traffic back if the smoke test fails. A failed migration never reaches
revision activation, and rollback never downgrades the database.

Staging enables `features.allowFakeProviders`, because every provider is off and
the Temporal worker refuses to start with an unconfigured one. That flag must
stay false wherever a paid provider is enabled, so a missing credential fails
loudly rather than silently producing fake output.

Blob storage is selected by `VIDGEN_BLOB_BACKEND`. Local development and every test keep the
filesystem store; a deployed environment sets `azure` and reaches the account over a private
endpoint with its managed identity, so no key, connection string or SAS token is ever configured.
`AssetService`'s content-addressed identity and immutable provenance are preserved: the Azure
adapter's `put_if_absent` uploads with an `If-None-Match: *` precondition and streams both
directions.

## T25 YouTube publication

T25 publishes an approved, current, T22-`PASS` render to the connected user's own YouTube channel,
restartably. It is the last irreversible step in the pipeline: a video that exists on someone's
channel cannot be un-created, only made private. Every design decision below follows from that.

```
services/publisher/
  youtube.py            the single official capability registry: endpoints, scopes, limits,
                        quota units, retryable statuses, processing states, verification date
  contracts.py          the provider-neutral boundary (dataclasses, never Google objects)
  providers.py          failure classification, bounded retries, provider selection
  youtube_adapter.py    the production YouTube Data API v3 adapter over httpx
  fake_youtube.py       the deterministic offline provider, with declarative failure injection
  oauth.py              the PKCE web-server authorization flow
  credentials.py        the publisher's view of vidgen.security.envelope
  eligibility.py        who may publish, decided before any provider request
  metadata.py           deterministic drafts, capability-registry validation, stable identity
  resumable.py          the resumable upload driver and its chunk sources
  processing.py         bounded processing polling
  pipeline.py           the restartable orchestration
  commands.py           composed entry points for the CLI, the API and Temporal
  projections.py        bounded, credential-free views

src/vidgen/contracts/publication.py     strict versioned contracts and the state machine
src/vidgen/db/publication_models.py     six tables, with the machine as a CHECK constraint
src/vidgen/db/publication_repository.py the only code that touches ciphertext columns
src/vidgen/security/envelope.py         AES-256-GCM envelope encryption and the keyring
packages/workflows/publication.py       the restartable workflow
workers/youtube_publisher/main.py       the dedicated, no-ingress publisher worker
```

### The capability registry

Every URL, OAuth endpoint, scope, size limit, privacy state, quota unit cost, retryable status code
and processing state lives once, in `services/publisher/youtube.py`, with the date it was verified
against the official documentation and a profile version that every publication persists. Nothing
else in the publisher hard-codes any of them, so a later profile can never silently reinterpret an
already-published video.

The registry records both the current quota accounting (`videos.insert` at 100 units, uploads
billing to a separate daily allowance) and the pre-2025-12-04 accounting, so an environment pinned
to the older profile is describable rather than mis-reported.

### Eligibility, decided first

`PublicationEligibilityService` proves every precondition from persisted rows before an OAuth
refresh or a YouTube request happens: a selected, complete T17 render with a verification report; a
readable final MP4 of an accepted media type and size; an unrevoked T18 approval for that exact
render job; a selected T22 run decided `PASS` with a matching `PASS` completion gate for that exact
render identity; nothing invalidated after the render was produced; no T21 repair parked in
`HUMAN_REVIEW_REQUIRED`; a caption track reached through the render job rather than by kind, which
is what proves it belongs to this render's lineage; a project-owned JPEG or PNG thumbnail when one
is selected; and a connection this owner controls with every required scope granted.

A refusal is a structured `PublicationGate` with an actionable remediation. A cross-owner or
cross-project ID produces the same "not found" shape as a missing one.

### OAuth and credential storage

The standard web-server flow with PKCE S256. `state` is 256 bits from `secrets`, stored only as a
SHA-256 hash, bound to the owner who started the flow, expiring after ten minutes and consumable
exactly once. The PKCE verifier is sealed at rest and bound by AAD to its own state row. The
post-login redirect target is checked against an allowlist when it is stored *and* again when it is
used, so widening the allowlist later cannot bless an old row. The authorization code is exchanged
only on the backend, and the channel identity is resolved from YouTube afterwards - a channel ID
supplied by a browser is never trusted.

Refresh tokens are sealed with AES-256-GCM: a random 96-bit nonce, the connection's UUID bound in as
associated data, and the key version on the row. Ciphertext lives in `youtube_connection_secrets`,
separate from the canonical connection, so a projection or an accidental `SELECT *` cannot return
it. A database CHECK makes "connected implies a stored credential" an invariant rather than a
convention. Rotation is `open with the recorded version, seal with the active one`, which is why
retired keys stay configured until every row is re-sealed.

Nothing in a contract, an API response, a log record, a span, a Temporal message or the dashboard
has a field for a token, an authorization code, a client secret or a resumable session URI. Where
evidence is needed, a *hash* is projected instead: `session_uri_hash` proves two checkpoints refer
to the same session without being enough to upload to it.

### The resumable upload

The rules, in the order they matter:

1. The session is persisted, sealed, **before a single media byte is sent**. A worker killed after
   `videos.insert` resumes; it does not start again.
2. Only a *server-confirmed* offset advances the checkpoint. A `308` carries the last byte YouTube
   actually holds; a lost response carries nothing. After any interruption the driver asks, and
   trusts the answer over its own bookkeeping.
3. Byte zero is never revisited while a session can be resumed. The only path that creates a second
   session is an expired session that confirmed *nothing* - where no video can exist.
4. The whole file is never in memory. Chunks come from a ranged read against the blob store
   (`RangedBlobStore`, implemented by both the filesystem and Azure adapters); a bounded temporary
   file is a fallback for a store that cannot serve ranges, and is deleted on durable completion or
   terminal failure.

Ambiguity is a state, not an assumption. When the final chunk's response is lost and the session is
gone, the publication stops at `HUMAN_REVIEW_REQUIRED` with the session identity and its confirmed
offset preserved, and resuming refuses. A second video is never created on a guess.

### Processing, captions, thumbnails

The video ID is persisted *before* the first processing poll, so no failure can lose the identity of
what was created. Polling is bounded exponential backoff; exceeding the elapsed budget leaves the
publication in `PROCESSING` with its last observed state, because slow is not failed. A processing
failure retains the video ID and its watch URL for investigation.

Captions and the thumbnail run *after* the video exists and after processing, so their failure can
never cause a second upload - a rule the state machine enforces: `UPLOADING_CAPTIONS` and
`UPLOADING_THUMBNAIL` have no transition back to an upload state. A `captionExists` conflict adopts
the existing track rather than duplicating it, and never touches a track this publication did not
create. A channel that cannot set custom thumbnails keeps its private video and gets an actionable
status. The canonical SRT is verified against its recorded hash and parsed before 400 quota units
are spent on it. The deprecated caption `sync` parameter is never sent.

### Privacy

Every initial upload is `privacyStatus=private` with `notifySubscribers=false`, and there is no way
to express anything else: `PublicationMetadata.initial_privacy` is a `Literal`. The
synthetic-media disclosure (`status.containsSyntheticMedia`) is always sent, because VidGen output
is animated and AI generated and the selected profile supports the field.

A visibility change is an explicit user action that re-checks the render lineage and the T22 gate
*immediately before* it runs, so a render selected during a long upload cannot be published under an
older render's approval. A scheduled publication is submitted as a private video carrying
`publishAt`. The privacy persisted is the one YouTube **returns**, never the one requested, so an
API project restricted to private uploads can never be reported as public.

### Identity and idempotency

A publication identity binds the project, the final render asset and its hash, the T22 run and its
report hash, the approval, the connection, the channel, the metadata version and hash, the caption
asset and hash, the thumbnail asset and hash, the initial privacy state, the publisher version and
the capability profile version. Retrying the same completed identity returns the existing
publication and creates nothing. Changing the selected render produces a different identity.
Changing material metadata after upload creates a new metadata version and updates the existing
video through `videos.update`; it never creates a second one.

### Database

Six additive tables in `0020_youtube_publication`, leaving exactly one Alembic head. The state
machine is compiled into a CHECK constraint *from the same transition table the application
enforces*, so the two cannot disagree. Also enforced in the database: a state after upload names its
video; a published video finished processing; a non-private state required an explicit visibility
decision; one publication per stable identity; one publication per YouTube video ID; one active
upload session per publication; a completed session confirmed every byte and names its video; one
successful caption per publication, language and name; one video and one thumbnail row per
publication; a byte offset within the total size; an OAuth state that expires after it was created
and whose hash is unique.

The downgrade refuses once publication provenance exists: these rows are the only local record of
which YouTube video a render became.

### T23 and Temporal

Every YouTube operation runs inside the existing T23 provider instrumentation. Quota units are
recorded as a typed usage quantity (`youtube_quota_unit`) with a **zero monetary cost**, because
Google does not bill for them and a fabricated dollar figure would corrupt the cost ledger. T25
adds no provider-attempt or telemetry table of its own.

The workflow holds nothing but IDs: a project ID, a publication-run ID, a connection ID, a render
asset ID, an idempotency key and a bounded trace context. It runs on a dedicated
`vidgen-publisher` task queue with a dedicated no-ingress worker, so a multi-hour upload cannot
starve ordinary project activities, and it re-enters the upload activity repeatedly, each time
resuming from the confirmed offset.

### Known limitations

1. **No production authentication.** The API still identifies callers with the development
   `X-VidGen-User` header. The OAuth callback itself does not depend on it - the one-time,
   owner-bound `state` is the authority, and a callback carrying a mismatched identity header is
   refused - but the rest of the publication API is owner-scoped by that header alone. Public
   multi-tenant YouTube OAuth is therefore **not** production-ready, and `providers.youtube` is
   false in both parameter files. Real authentication is T26 work.
2. **The real-channel test has not been run in this repository.** `tests/test_t25_real_youtube.py`
   is opt-in and manually triggered; it needs a Google Cloud project, a verified OAuth client and a
   test channel, none of which exist here. Every fake-provider and mocked-adapter test passes.
3. **Quota costs are point-in-time.** They were verified on the date recorded in the capability
   registry, and Google has changed them twice in the last year. They are configuration, and the
   registry records both profiles.
4. **Only one caption language per publication.** The schema supports several - the uniqueness is
   per language and name - but the draft exposes a single canonical track, which is the one T17
   produces.
5. **Thumbnails are chosen, not generated.** T25 adds no paid thumbnail-generation pipeline: the
   user selects a project-owned JPEG or PNG.
6. **Analytics, comments, playlists, monetization and multi-channel CMS are out of scope**, and no
   Partner or CMS scope is requested.

## T18b control-plane execution and revision orchestration

T18b is a corrective task. Its subject is not a new capability but a class of untruth: several APIs
and UI actions returned `queued`, `accepted` or a workflow ID for work that no process would ever
consume. T18b makes every supported asynchronous command durable, dispatched, queryable,
restartable and eventually terminal.

### The invariant

No endpoint may return `queued`, `accepted`, `running` or a future workflow ID unless one of these
is already true:

- the work was transactionally persisted in `control_commands` and a worker can claim it;
- the target Temporal workflow or child workflow was successfully started;
- an identical completed command is being idempotently reused.

An in-memory flag, an idempotency response by itself, or a workflow ID computed from a request does
not count. The `control_command_dispatched_identity` CHECK constraint enforces the workflow half of
this in the database: a `running`, `awaiting_review` or `completed` command that does not name a
workflow cannot be stored.

### What was disconnected, and what now connects it

| Gap on `main` before T18b | Root cause | What T18b does |
| --- | --- | --- |
| Browser-created projects stored empty settings while T12 requires a voice profile | Voice selection existed only as a local repair CLI | Product-level voice APIs, voice selection in project creation and setup, and a `voice_profile_required` precondition on workflow start |
| `ContinuityReferenceWorkflow` was never registered | The worker's registry did not list it, and its two activities were never defined anywhere | The workflow and `resolve_continuity_inputs`, `build_continuity_references`, `apply_continuity_references` are defined and registered, with production handlers over the existing T19 components |
| T19 build and generate endpoints returned `queued` | Nothing durable was written | Both create a control command that starts the real T19 workflow |
| Reference approvals updated rows only | No signal reached the waiting workflow | An approval creates a `reference_apply` command that signals the exact waiting workflow with the approved set |
| T19 was not part of the normal project lifecycle | The parent workflow went storyboard → fan-out | The parent runs T19 as a child between the authoritative storyboard and any T14 spend, and completes deterministically when nothing needs a reference |
| T18 regeneration returned a calculated child workflow ID | The command was signalled to the *locked* child, which had already completed | A `shot_regenerate` command starts a genuinely new `ShotWorkflow` under a reproducible regeneration identity |
| T20 and T21 decisions had no consumer | The shot workflow had already returned | Both create a continuation command; the dispatcher resumes a live child or starts an immutable recovery run, never signals a terminal one |
| Transcript and script edits recorded invalidation only | Nothing executed the rebuild | A confirmed transcript edit, and the selection of a revised script, each create a revision command that opens a new generation run at the correct entry stage |
| `POST /final-qa:run` returned `queued` | No job or workflow was created | It creates a command that runs T22 against the current selected render in its own workflow |
| T22 remediation classified without executing | Routing was recorded, not dispatched | Executable targets create the owning stage's command; routing-only targets are refused with `remediation_unsupported` rather than accepted |
| A paused or partial project had no continuation path | The parent workflow could not be re-entered | `POST /workflow:continue` opens a new immutable generation run; previous runs are preserved |

### Durable command architecture

`control_commands` persists: command ID, project, owner subject, command type, target type and ID,
idempotency key, request hash, upstream input identity, expected row version, status, attempt count,
claim owner, lease expiry, workflow and run ID, result type and ID, structured error code, the
created/dispatched/started/completed/updated timestamps, bounded redacted metadata and trace
context. Statuses are `pending`, `claimed`, `dispatching`, `running`, `awaiting_review`,
`completed`, `failed`, `cancelled` and `superseded`, with the legal transitions enforced in
`vidgen.contracts.control_commands.ALLOWED_TRANSITIONS` and by the repository.

Concurrency is one pattern throughout: every transition is a conditional `UPDATE` guarded by the row
version that was read, so a claim, a completion and a lease recovery are each safe against another
replica without a backend-specific statement. PostgreSQL additionally takes `FOR UPDATE SKIP LOCKED`
on the candidate scan; SQLite runs the identical code path, which is what makes the deterministic
tests meaningful.

Nothing in a command row is a payload. Metadata is a bounded string map, every reference to content
is an ID or a hash, and no credential, prompt, transcript, script, manifest or provider response can
be stored there.

### Project generation runs

`project_generation_runs` records each immutable generation attempt: sequence, status, entry stage,
input identity, workflow and run ID, the command that opened it, and its parent. A partial unique
index allows exactly one non-terminal run per project, so two concurrent revisions cannot both claim
the active lineage, while every historical run is preserved.

The project workflow now *completes* at every human pause rather than blocking forever, and the
Temporal controller uses `ALLOW_DUPLICATE` rather than `ALLOW_DUPLICATE_FAILED_ONLY`: continuing a
project is a new execution carrying a new generation run, not a retry of a failed one. A run names
the earliest stage it must execute; everything above it is reused.

### The dispatcher

`workers/control_dispatcher/main.py` is a bounded database-polling worker. One pass claims a batch
transactionally, revalidates project ownership and upstream lineage, starts or signals the correct
workflow, persists the identity that actually came back, marks the command running only then, and
later settles it from the workflow's own durable state. A killed replica strands nothing: its lease
expires and the next dispatcher recovers the command, bounded by the command's attempt limit.

```bash
uv run python -m workers.control_dispatcher.main          # poll until stopped
uv run python -m workers.control_dispatcher.main --once   # one bounded pass
make run-control-dispatcher
```

### Cost and telemetry

T18b creates no second cost system. Paid work still happens inside the workflow the dispatcher
started, under T23's existing provider-attempt, budget, reservation and reconciliation machinery. An
idempotent command replay adopts the existing command and therefore produces no second provider
attempt, reservation or ledger entry. The broader T23 metrics-export problem is recorded in
[`docs/ROADMAP_GAPS.md`](ROADMAP_GAPS.md) as T23b and is deliberately not fixed here.

### Local commands

```bash
make run-control-dispatcher                          # dispatch queued commands
curl -s -H "$H" $BASE/commands                       # every command and the generation lineage
curl -s -H "$H" $BASE/commands/$COMMAND_ID
curl -s -X POST -H "$H" $BASE/commands/$COMMAND_ID:cancel
curl -s -X POST -H "$H" $BASE/commands/$COMMAND_ID:retry
curl -s -H "$H" $BASE/voice-profiles                 # the voices this deployment can narrate with
```

### Known limitations

1. **The dispatcher has no deployment definition.** `infra/bicep` does not yet define a Container
   App for it, so a deployed environment would accept commands that nothing dispatches. Recorded in
   `docs/ROADMAP_GAPS.md` under T24b.
2. **Two remediation targets are refused rather than executed.** `CORRECT_SCRIPT_UPSTREAM` and
   `CORRECT_REFERENCE_T19` are routing classifications: acting on them means a human editing a
   script or a reference. The API says so with `remediation_unsupported` instead of answering `202`
   to work nothing would perform.
3. **A shot remediation needs the caller to name the shot.** T22 findings record the finding, not
   its owning shot, so a `REPAIR_SHOT_T21` or `REGENERATE_SHOT_T16` route requires `shot_id`.
4. **T19 builds a reference sheet only where T09 persisted candidate frames.** Without evidence
   frames there is nothing to build a sheet from, so the entity is treated as needing no reference
   and the workflow completes deterministically rather than waiting for an approval nobody can give.
5. **Production authentication is still absent**, so every owner scope here rests on the development
   `X-VidGen-User` header. Recorded under T24b.
