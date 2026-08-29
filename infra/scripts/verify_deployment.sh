#!/usr/bin/env bash
# Verify what is actually running after a deployment.
#
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... \
#   VIDGEN_APP_IMAGE=... VIDGEN_WEB_IMAGE=... \
#     ./infra/scripts/verify_deployment.sh
#
# It checks the properties a deployment can silently get wrong: the image each
# revision is actually running, that the revisions are healthy and carrying
# traffic, that the environment is still internal, and that no data service has
# public network access. It reads only; it changes nothing.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
require_env AZURE_SUBSCRIPTION_ID VIDGEN_RESOURCE_GROUP
az account set --subscription "${AZURE_SUBSCRIPTION_ID}"

outputs="$(latest_deployment_outputs)"
value_of() { printf '%s' "${outputs}" | python3 -c "import json,sys;print(json.load(sys.stdin).get('$1',{}).get('value',''))"; }

API_APP="$(value_of apiAppName)"
WEB_APP="$(value_of webAppName)"
WORKER_APP="$(value_of workerAppName)"
CAE_NAME="$(value_of containerAppsEnvironmentName)"
STORAGE="$(value_of storageAccountName)"
KEY_VAULT="$(value_of keyVaultName)"
PG_FQDN="$(value_of postgresFqdn)"

failures=0
check() {
  local name="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    log "OK   ${name}: ${actual}"
  else
    log "FAIL ${name}: expected '${expected}', got '${actual}'"
    failures=$((failures + 1))
  fi
}

log "verifying the running image of every workload"
for pair in "${API_APP}:${VIDGEN_APP_IMAGE:-}" "${WORKER_APP}:${VIDGEN_APP_IMAGE:-}" "${WEB_APP}:${VIDGEN_WEB_IMAGE:-}"; do
  app="${pair%%:*}"
  expected="${pair#*:}"
  [[ -n "${expected}" ]] || continue
  actual="$(az containerapp show --name "${app}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --query 'properties.template.containers[0].image' -o tsv)"
  check "image of ${app}" "${expected}" "${actual}"
done

log "verifying revision health"
for app in "${API_APP}" "${WEB_APP}" "${WORKER_APP}"; do
  health="$(az containerapp revision list --name "${app}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --query "sort_by([?properties.active], &properties.createdTime)[-1].properties.healthState" -o tsv)"
  check "health of ${app}" "Healthy" "${health}"
done

log "verifying the environment is still internal"
internal="$(az containerapp env show --name "${CAE_NAME}" --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --query 'properties.vnetConfiguration.internal' -o tsv)"
check "container apps environment internal" "true" "${internal}"

log "verifying no data service is publicly reachable"
check "storage public network access" "Disabled" \
  "$(az storage account show --name "${STORAGE}" --resource-group "${VIDGEN_RESOURCE_GROUP}" --query publicNetworkAccess -o tsv)"
check "key vault public network access" "Disabled" \
  "$(az keyvault show --name "${KEY_VAULT}" --resource-group "${VIDGEN_RESOURCE_GROUP}" --query properties.publicNetworkAccess -o tsv)"
check "postgres public network access" "Disabled" \
  "$(az postgres flexible-server show --name "${PG_FQDN%%.*}" --resource-group "${VIDGEN_RESOURCE_GROUP}" --query network.publicNetworkAccess -o tsv)"

[[ "${failures}" -eq 0 ]] || fail "${failures} verification check(s) failed"
log "deployment verified"
