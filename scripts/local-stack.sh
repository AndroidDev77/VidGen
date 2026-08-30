#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(
  docker compose
  -f docker-compose.yml
  -f docker-compose.local.yml
)

require_real_provider_keys() {
  if [[ ! -f .env ]]; then
    echo "Missing .env. Run: cp .env.example .env"
    exit 1
  fi

  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  if [[ -z "${VIDGEN_OPENAI_API_KEY:-}" ]]; then
    echo "VIDGEN_OPENAI_API_KEY is missing from .env"
    exit 1
  fi

  if [[ -z "${VIDGEN_RUNWAY_API_SECRET:-}" ]]; then
    echo "VIDGEN_RUNWAY_API_SECRET is missing from .env"
    exit 1
  fi
}

case "${1:-up}" in
  up)
    require_real_provider_keys
    exec "${COMPOSE[@]}" up \
      --build \
      postgres \
      redis \
      azurite \
      temporal \
      storage-init \
      migrate \
      api \
      worker \
      control-dispatcher \
      web
    ;;

  start)
    require_real_provider_keys
    "${COMPOSE[@]}" up \
      --build \
      --detach \
      postgres \
      redis \
      azurite \
      temporal \
      storage-init \
      migrate \
      api \
      worker \
      control-dispatcher \
      web

    "${COMPOSE[@]}" ps
    ;;

  logs)
    exec "${COMPOSE[@]}" logs \
      --follow \
      api \
      worker \
      control-dispatcher \
      web
    ;;

  status)
    "${COMPOSE[@]}" ps
    ;;

  down)
    "${COMPOSE[@]}" down
    ;;

  reset)
    echo "This deletes the local VidGen database, uploads, and generated assets."
    read -r -p "Type RESET to continue: " confirmation

    if [[ "$confirmation" != "RESET" ]]; then
      echo "Cancelled."
      exit 1
    fi

    "${COMPOSE[@]}" down --volumes
    ;;

  *)
    echo "Usage: $0 {up|start|logs|status|down|reset}"
    exit 2
    ;;
esac
