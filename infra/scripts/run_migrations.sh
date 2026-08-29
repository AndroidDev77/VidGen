#!/usr/bin/env bash
# Start the migration job exactly once and wait for it to succeed.
#
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... VIDGEN_MIGRATION_JOB=... \
#     ./infra/scripts/run_migrations.sh
#
# The job itself is configured with parallelism one and no retry, and the script
# it runs takes a PostgreSQL advisory lock, so a concurrent run cannot apply a
# migration twice. This script adds the outer guarantee: it refuses to start a
# second execution while one is already running, and it exits non-zero unless
# the execution it started reached Succeeded.
#
# A non-zero exit here must block the traffic shift. There is no downgrade path:
# a failed migration is repaired forward.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
require_env AZURE_SUBSCRIPTION_ID VIDGEN_RESOURCE_GROUP VIDGEN_MIGRATION_JOB
: "${VIDGEN_MIGRATION_TIMEOUT_SECONDS:=1800}"

az account set --subscription "${AZURE_SUBSCRIPTION_ID}"

running="$(az containerapp job execution list \
  --name "${VIDGEN_MIGRATION_JOB}" \
  --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --query "[?properties.status=='Running'].name" -o tsv)"
if [[ -n "${running}" ]]; then
  fail "a migration execution is already running: ${running}"
fi

log "starting the migration job"
execution="$(az containerapp job start \
  --name "${VIDGEN_MIGRATION_JOB}" \
  --resource-group "${VIDGEN_RESOURCE_GROUP}" \
  --query name -o tsv)"
log "migration execution ${execution} started"

deadline=$(( $(date +%s) + VIDGEN_MIGRATION_TIMEOUT_SECONDS ))
status="Unknown"
while [[ "$(date +%s)" -lt "${deadline}" ]]; do
  status="$(az containerapp job execution show \
    --name "${VIDGEN_MIGRATION_JOB}" \
    --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --job-execution-name "${execution}" \
    --query properties.status -o tsv)"
  case "${status}" in
    Succeeded)
      log "migration execution ${execution} succeeded"
      exit 0
      ;;
    Failed|Degraded|Stopped)
      # The execution's logs are retained in Log Analytics; they are not
      # reprinted here, because a migration failure can quote a statement and
      # the surrounding job configuration includes a connection string.
      fail "migration execution ${execution} finished as ${status}; inspect ContainerAppConsoleLogs_CL for job '${VIDGEN_MIGRATION_JOB}' execution '${execution}'"
      ;;
  esac
  sleep 10
done

fail "migration execution ${execution} did not finish within ${VIDGEN_MIGRATION_TIMEOUT_SECONDS}s (last status ${status})"
