# syntax=docker/dockerfile:1

# The VidGen application image. One image serves the API, the Temporal worker,
# the Alembic migration job, the render job and the administrative jobs: they
# share the same Python dependency set and the same FFmpeg toolchain, so
# splitting them would only multiply the surface that has to be scanned,
# published and kept in sync. The workload is chosen by the container command,
# never by the image.
#
# Base images are pinned by digest, so a rebuild of an old commit produces the
# same layers and a base-image republish cannot silently change what ships.

ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

FROM ${PYTHON_IMAGE} AS builder
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /usr/local/bin/uv
WORKDIR /app
# The lockfile is the only source of versions: `--frozen` fails the build rather
# than silently resolving something newer than what CI verified.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY apps ./apps
COPY services ./services
COPY packages ./packages
COPY workers ./workers
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev --extra azure

FROM ${PYTHON_IMAGE} AS runtime

# ffmpeg carries both `ffmpeg` and `ffprobe`; T17 rendering and the T20/T22
# deterministic media measurements both require them on PATH. ca-certificates
# is required for TLS to Temporal Cloud, Azure and the configured providers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A fixed non-root uid/gid, so a Container Apps `runAsUser` and any file
# ownership in this image agree, and nothing in the container runs as root.
RUN groupadd --system --gid 10001 vidgen \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/vidgen --create-home vidgen

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app /app

# Every workload treats local disk as disposable scratch and streams canonical
# output to Blob Storage. TMPDIR is explicit and writable by the runtime user so
# nothing falls back to a read-only or root-owned path.
ENV TMPDIR=/tmp/vidgen \
    VIDGEN_TMPDIR=/tmp/vidgen \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /tmp/vidgen && chown 10001:10001 /tmp/vidgen

USER 10001:10001
EXPOSE 8000

# Build metadata. Both are supplied by the deployment workflow from the exact
# commit being deployed, so a running revision can always be traced back to a
# source commit without consulting the deployment logs.
ARG VIDGEN_COMMIT_SHA=unknown
ARG VIDGEN_BUILD_TIMESTAMP=unknown
ARG VIDGEN_SOURCE_URL=https://github.com/AndroidDev77/VidGen
ENV VIDGEN_COMMIT_SHA=${VIDGEN_COMMIT_SHA} \
    VIDGEN_BUILD_TIMESTAMP=${VIDGEN_BUILD_TIMESTAMP}
LABEL org.opencontainers.image.title="vidgen-app" \
      org.opencontainers.image.description="VidGen API, Temporal worker and finite jobs" \
      org.opencontainers.image.source="${VIDGEN_SOURCE_URL}" \
      org.opencontainers.image.revision="${VIDGEN_COMMIT_SHA}" \
      org.opencontainers.image.created="${VIDGEN_BUILD_TIMESTAMP}" \
      org.opencontainers.image.licenses="UNLICENSED"

# uvicorn installs its own SIGTERM handler and drains in-flight requests, so the
# exec form is required: the process must be PID 1 and receive the signal.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
