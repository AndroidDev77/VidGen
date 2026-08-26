# VidGen

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

The complete system architecture and T01-T26 implementation roadmap are maintained in
[`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md). Contributors and coding agents should
read it together with `AGENTS.md` before planning the next roadmap task.

This repository implements roadmap tasks T01 through T13 and T23, including subtitle-first transcript acquisition, restartable episode analysis, comedy script generation, measured narration, deterministic storyboard timing, and cloud-neutral observability/cost controls:

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

## Quick start

```bash
cp .env.example .env
uv sync --all-groups
make verify
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
