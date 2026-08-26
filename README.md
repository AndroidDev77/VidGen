# VidGen

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

The complete system architecture and T01-T26 implementation roadmap are maintained in
[`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md). Contributors and coding agents should
read it together with `AGENTS.md` before planning the next roadmap task.

This repository implements roadmap tasks T01 through T09, including subtitle-first transcript acquisition:

- Python monorepo, CI, and local infrastructure
- Versioned Pydantic contracts plus exported JSON Schema
- PostgreSQL data model and Alembic migration
- Content-addressed asset storage with provenance
- Deterministic fake AI and media providers for offline tests
- Owner-scoped resumable source-video uploads
- Deterministic FFmpeg media probing, audio extraction, scene detection, and frame extraction
- Restartable transcription, overlap reconciliation, and anonymous speaker diarization
- Embedded, sidecar, and OpenSubtitles acquisition with audio-transcription fallback

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
