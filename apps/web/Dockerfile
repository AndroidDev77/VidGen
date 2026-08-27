# Build the T18 review UI as static assets, then serve them from nginx.
FROM node:24-alpine AS build
WORKDIR /repo
RUN corepack enable && corepack prepare pnpm@11.19.0 --activate

COPY pnpm-workspace.yaml pnpm-lock.yaml package.json ./
COPY packages/ts-contracts/package.json packages/ts-contracts/
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile --filter @vidgen/web...

COPY packages/ts-contracts packages/ts-contracts
COPY apps/web apps/web
# No server secrets are baked in: only the public API base URL and the local
# development identity are read from VITE_ variables.
ARG VITE_VIDGEN_API_BASE_URL=""
ARG VITE_VIDGEN_DEV_USER="local-user"
ENV VITE_VIDGEN_API_BASE_URL=$VITE_VIDGEN_API_BASE_URL
ENV VITE_VIDGEN_DEV_USER=$VITE_VIDGEN_DEV_USER
RUN pnpm --filter @vidgen/web build

FROM nginx:1.29-alpine AS runtime
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
