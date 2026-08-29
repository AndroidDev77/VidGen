# syntax=docker/dockerfile:1

# Build the T18 review UI as static assets, then serve them from nginx.
#
# Base images are pinned by digest so a rebuild of an old commit produces the
# same layers. Nothing secret is available to this build: the bundle is public
# by definition, so only the same-origin API path and the local development
# identity are compiled in.

ARG NODE_IMAGE=node:24.20.0-alpine3.24@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.29.8-alpine3.23@sha256:0c79d56aee561a1d81c63f00eee5fb5fe29279560cdc55e91425133104c7fbe6

FROM ${NODE_IMAGE} AS build
WORKDIR /repo
RUN corepack enable && corepack prepare pnpm@11.19.0 --activate

COPY pnpm-workspace.yaml pnpm-lock.yaml package.json ./
COPY packages/ts-contracts/package.json packages/ts-contracts/
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile --filter @vidgen/web...

COPY packages/ts-contracts packages/ts-contracts
COPY apps/web apps/web
# No server secrets are baked in: only the public API base URL and the local
# development identity are read from VITE_ variables. The deployed build leaves
# the base URL empty on purpose, so the bundle calls the same-origin /api path
# and the API endpoint stays a *runtime* setting (VIDGEN_API_UPSTREAM below).
# Changing which API a revision talks to therefore never requires a rebuild and
# never changes the image digest.
ARG VITE_VIDGEN_API_BASE_URL=""
ARG VITE_VIDGEN_DEV_USER="local-user"
ENV VITE_VIDGEN_API_BASE_URL=$VITE_VIDGEN_API_BASE_URL
ENV VITE_VIDGEN_DEV_USER=$VITE_VIDGEN_DEV_USER
RUN pnpm --filter @vidgen/web build

# The unprivileged nginx image already runs as a non-root user and already
# listens on 8080, so no capability or privileged port is needed.
FROM ${NGINX_IMAGE} AS runtime
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
# nginx substitutes VIDGEN_API_UPSTREAM into the template at container start, so
# the API is reverse-proxied under /api on this same origin and the browser
# never makes a cross-origin request.
COPY apps/web/nginx.conf /etc/nginx/templates/default.conf.template
ENV VIDGEN_API_UPSTREAM="http://api:8000"
ENV NGINX_ENVSUBST_TEMPLATE_SUFFIX=".template"
# Only VIDGEN_ names are substituted, so nginx's own $host/$uri survive.
ENV NGINX_ENVSUBST_FILTER="^VIDGEN_"
EXPOSE 8080

ARG VIDGEN_COMMIT_SHA=unknown
ARG VIDGEN_BUILD_TIMESTAMP=unknown
ARG VIDGEN_SOURCE_URL=https://github.com/AndroidDev77/VidGen
LABEL org.opencontainers.image.title="vidgen-web" \
      org.opencontainers.image.description="VidGen review UI served by nginx" \
      org.opencontainers.image.source="${VIDGEN_SOURCE_URL}" \
      org.opencontainers.image.revision="${VIDGEN_COMMIT_SHA}" \
      org.opencontainers.image.created="${VIDGEN_BUILD_TIMESTAMP}" \
      org.opencontainers.image.licenses="UNLICENSED"

USER 101
