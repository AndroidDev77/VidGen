# VidGen

VidGen is an automated, restartable pipeline for turning long-form video into animated comedy recap videos.

This repository implements roadmap tasks T01 through T07:

- Python monorepo, CI, and local infrastructure
- Versioned Pydantic contracts plus exported JSON Schema
- PostgreSQL data model and Alembic migration
- Content-addressed asset storage with provenance
- Deterministic fake AI and media providers for offline tests
- Owner-scoped resumable source-video uploads
- Deterministic FFmpeg media probing, audio extraction, scene detection, and frame extraction
- Restartable transcription, overlap reconciliation, and anonymous speaker diarization

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
