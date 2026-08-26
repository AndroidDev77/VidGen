# VidGen

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

The complete system architecture and T01-T26 implementation roadmap are maintained in
[`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md). Contributors and coding agents should
read it together with `AGENTS.md` before planning the next roadmap task.

This repository implements roadmap tasks T01 through T11 and T23, including subtitle-first transcript acquisition, restartable episode analysis, comedy script generation, and cloud-neutral observability/cost controls:

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
