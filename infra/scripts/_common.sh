#!/usr/bin/env bash
# Shared helpers. Sourced, never executed directly.
#
# Every script in this directory reads its target environment from environment
# variables. None of them contains a subscription id, tenant id, resource group
# name, region or hostname, and none of them ever prints a secret value.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BICEP_MAIN="${REPO_ROOT}/infra/bicep/main.bicep"

log()  { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

require_env() {
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      fail "${name} is not set"
    fi
  done
}

require_az() {
  command -v az >/dev/null 2>&1 || fail "the Azure CLI (az) is required"
}

# The environment every script deploys against. VIDGEN_ENVIRONMENT selects the
# parameter file; the rest are the values that must never be committed.
resolve_environment() {
  : "${VIDGEN_ENVIRONMENT:=staging}"
  PARAM_FILE="${REPO_ROOT}/infra/bicep/environments/${VIDGEN_ENVIRONMENT}.bicepparam"
  [[ -f "${PARAM_FILE}" ]] || fail "no parameter file at ${PARAM_FILE}"
  require_env AZURE_SUBSCRIPTION_ID VIDGEN_RESOURCE_GROUP VIDGEN_AZURE_LOCATION
  export PARAM_FILE
}

# Fail on any image reference that is not immutable. A moving tag makes a
# revision unreproducible and a rollback meaningless, so this is checked before
# anything is deployed rather than discovered afterwards.
assert_immutable_image() {
  local reference="$1"
  if [[ "${reference}" == *"@sha256:"* ]]; then
    return 0
  fi
  local tag="${reference##*:}"
  if [[ "${reference}" != *:* ]]; then
    fail "image reference '${reference}' has no tag or digest"
  fi
  if [[ "${tag}" == "latest" ]]; then
    fail "refusing to deploy the 'latest' tag"
  fi
  if [[ ! "${tag}" =~ ^[0-9a-f]{40}$ ]]; then
    fail "image reference '${reference}' is not a digest or a 40-character commit SHA tag"
  fi
}

deployment_outputs() {
  local deployment_name="$1"
  az deployment group show \
    --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --name "${deployment_name}" \
    --query properties.outputs -o json
}

latest_deployment_outputs() {
  az deployment group list \
    --resource-group "${VIDGEN_RESOURCE_GROUP}" \
    --query "sort_by([?properties.provisioningState=='Succeeded'], &properties.timestamp)[-1].properties.outputs" \
    -o json
}
