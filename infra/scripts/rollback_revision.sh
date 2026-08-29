#!/usr/bin/env bash
# Return one Container App's traffic to its previous healthy revision.
#
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... \
#     ./infra/scripts/rollback_revision.sh <app-name> [target-revision]
#
# What this does: identify the most recent active, healthy revision that is not
# the current one, and send 100% of traffic to it. Nothing is deleted, so the
# failed revision's logs and configuration remain available for diagnosis.
#
# What this deliberately does NOT do:
#
#   * It never runs `alembic downgrade`. A rollback restores application code,
#     not schema state.
#   * It never deletes a database row, a blob or an image.
#
# That is only safe because every migration is expected to be
# backwards-compatible with the immediately preceding application revision - see
# "Schema compatibility and rollback" in infra/README.md. A migration that
# breaks that expectation must be split into an expand step and a contract step
# across two deployments.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
require_env AZURE_SUBSCRIPTION_ID VIDGEN_RESOURCE_GROUP
[[ $# -ge 1 ]] || fail "usage: rollback_revision.sh <app-name> [target-revision]"
APP="$1"
TARGET="${2:-}"

az account set --subscription "${AZURE_SUBSCRIPTION_ID}"

mode="$(az containerapp show --name "${APP}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --query 'properties.configuration.activeRevisionsMode' -o tsv)"

# The Temporal worker has no ingress, so there is no traffic to split and it
# runs in Single revision mode. Rolling it back means running the previous
# revision's image again, which produces a new revision carrying the old code.
# The failed revision is left in place, so its logs remain available.
if [[ "${mode}" != "Multiple" ]]; then
  log "app '${APP}' is in ${mode} revision mode; rolling back by redeploying the previous image"
  current_image="$(az containerapp show --name "${APP}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --query 'properties.template.containers[0].image' -o tsv)"
  if [[ -z "${TARGET}" ]]; then
    TARGET="$(az containerapp revision list --name "${APP}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
      --query "sort_by([?properties.template.containers[0].image != '${current_image}'], &properties.createdTime)[-1].name" \
      -o tsv)"
  fi
  [[ -n "${TARGET}" ]] || fail "no earlier revision with a different image found for '${APP}'"
  previous_image="$(az containerapp revision show --name "${APP}" \
    --resource-group "${VIDGEN_RESOURCE_GROUP}" --revision "${TARGET}" \
    --query 'properties.template.containers[0].image' -o tsv)"
  [[ -n "${previous_image}" ]] || fail "revision '${TARGET}' has no recorded image"
  log "redeploying '${APP}' with the image from revision '${TARGET}'"
  az containerapp update --name "${APP}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --image "${previous_image}" --output none
  log "rolled '${APP}' back to the image from '${TARGET}'"
  log "no database downgrade was performed and no data was deleted"
  exit 0
fi

current="$(az containerapp show --name "${APP}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --query "properties.configuration.ingress.traffic[?weight==\`100\`] | [0].revisionName" -o tsv)"
log "current traffic revision: ${current:-<none>}"

if [[ -z "${TARGET}" ]]; then
  TARGET="$(az containerapp revision list --name "${APP}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --query "sort_by([?properties.active && properties.healthState=='Healthy' && name!='${current}'], &properties.createdTime)[-1].name" \
    -o tsv)"
fi
[[ -n "${TARGET}" ]] || fail "no previous healthy revision found for '${APP}'"
[[ "${TARGET}" != "${current}" ]] || fail "target revision equals the current revision"

log "shifting 100% of traffic on '${APP}' to '${TARGET}'"
az containerapp ingress traffic set \
  --name "${APP}" \
  --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --revision-weight "${TARGET}=100" \
  --output none

log "rolled '${APP}' back to '${TARGET}'; the failed revision was left in place with its logs intact"
log "no database downgrade was performed and no data was deleted"
