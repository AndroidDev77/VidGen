# syntax=docker/dockerfile:1

# The render worker is the shared VidGen application image with the render
# command baked in. It deliberately does not rebuild the dependency set or the
# FFmpeg toolchain: T24 publishes one application image, the workload is chosen
# by the container command, and a second build of the same layers would only
# multiply what has to be scanned, published and kept in sync.
#
# Build it *after* the application image, passing that image as APP_IMAGE:
#
#   docker build -f Dockerfile -t vidgen-app:local .
#   docker build -f workers/render_job/Dockerfile \
#       --build-arg APP_IMAGE=vidgen-app:local -t vidgen-render:local .
#
# The deployed Azure Container Apps Job does not use this file. It runs the
# application image directly with the same command, which is asserted by
# infra/tests/test_infrastructure.py.

ARG APP_IMAGE=vidgen-app:local
FROM ${APP_IMAGE}

# The render job's temporary workspace. It is disposable scratch: inputs are
# staged here, intermediates are written here, and every canonical output is
# streamed to Blob Storage through AssetService before the directory is removed.
ENV VIDGEN_RENDER_WORK_ROOT=/tmp/vidgen/render \
    VIDGEN_SERVICE_NAME=vidgen-render

LABEL org.opencontainers.image.title="vidgen-render-worker" \
      org.opencontainers.image.description="T17b out-of-band render worker"

# VIDGEN_RENDER_JOB_ID is supplied per execution, so the image stays neutral:
# one render job per execution, never a project or a queue baked into the build.
ENTRYPOINT ["python", "-m", "workers.render_job.main"]
CMD ["--from-env"]
