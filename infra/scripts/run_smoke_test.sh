#!/usr/bin/env bash
# Run the private smoke test as a Container Apps Job and wait for the result.
#
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... VIDGEN_SMOKE_JOB=... \
#     ./infra/scripts/run_smoke_test.sh
#
# The job runs inside the virtual network, which is the only place the internal
# API ingress and the private endpoints are reachable from. A GitHub-hosted
# runner cannot do this, and is not asked to.
#
# A non-zero exit is what triggers rollback_revision.sh in the workflow.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
require_env AZURE_SUBSCRIPTION_ID VIDGEN_RESOURCE_GROUP VIDGEN_SMOKE_JOB
: "${VIDGEN_SMOKE_TIMEOUT_SECONDS:=1800}"
: "${VIDGEN_SMOKE_MODE:=e2e}"

az account set --subscription "${AZURE_SUBSCRIPTION_ID}"

log "starting the smoke-test job in mode ${VIDGEN_SMOKE_MODE}"
execution="$(az containerapp job start \
  --name "${VIDGEN_SMOKE_JOB}" \
  --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --query name -o tsv)"
log "smoke execution ${execution} started"

deadline=$(( $(date +%s) + VIDGEN_SMOKE_TIMEOUT_SECONDS ))
status="Unknown"
while [[ "$(date +%s)" -lt "${deadline}" ]]; do
  status="$(az containerapp job execution show \
    --name "${VIDGEN_SMOKE_JOB}" \
    --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --job-execution-name "${execution}" \
    --query properties.status -o tsv)"
  case "${status}" in
    Succeeded)
      log "smoke execution ${execution} succeeded"
      exit 0
      ;;
    Failed|Degraded|Stopped)
      fail "smoke execution ${execution} finished as ${status}; the per-check report is in ContainerAppConsoleLogs_CL for execution '${execution}'"
      ;;
  esac
  sleep 10
done

fail "smoke execution ${execution} did not finish within ${VIDGEN_SMOKE_TIMEOUT_SECONDS}s (last status ${status})"
