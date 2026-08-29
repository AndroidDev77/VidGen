#!/usr/bin/env bash
# Static infrastructure validation. Requires no Azure credential and no
# deployment: this is the check every pull request runs.
#
#   ./infra/scripts/validate_infrastructure.sh
#
# It verifies Bicep formatting, that every template and parameter file builds
# and lints clean, that the parameter files carry no secret, and that the
# committed templates are byte-identical to a freshly formatted copy.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

BICEP_DIR="${REPO_ROOT}/infra/bicep"
BICEP_BIN="${BICEP_BIN:-az bicep}"

run_bicep() {
  if [[ "${BICEP_BIN}" == "az bicep" ]]; then
    az bicep "$@"
  else
    "${BICEP_BIN}" "$@"
  fi
}

# Placeholder values so `build-params` can evaluate readEnvironmentVariable().
# They are obviously fake and are never used for a deployment.
export VIDGEN_AZURE_LOCATION="${VIDGEN_AZURE_LOCATION:-eastus2}"
export VIDGEN_APP_IMAGE="${VIDGEN_APP_IMAGE:-placeholder.azurecr.io/vidgen-app@sha256:0000000000000000000000000000000000000000000000000000000000000000}"
export VIDGEN_WEB_IMAGE="${VIDGEN_WEB_IMAGE:-placeholder.azurecr.io/vidgen-web@sha256:0000000000000000000000000000000000000000000000000000000000000000}"
export VIDGEN_DEPLOY_PRINCIPAL_ID="${VIDGEN_DEPLOY_PRINCIPAL_ID:-00000000-0000-0000-0000-000000000000}"
export VIDGEN_TEMPORAL_ADDRESS="${VIDGEN_TEMPORAL_ADDRESS:-placeholder.tmprl.cloud:7233}"
export VIDGEN_TEMPORAL_NAMESPACE="${VIDGEN_TEMPORAL_NAMESPACE:-placeholder}"

log "checking Bicep formatting"
format_failures=0
while IFS= read -r file; do
  formatted="$(run_bicep format --stdout "${file}")"
  if ! diff -q <(printf '%s\n' "${formatted}") "${file}" >/dev/null 2>&1; then
    log "not formatted: ${file#"${REPO_ROOT}/"}"
    format_failures=$((format_failures + 1))
  fi
done < <(find "${BICEP_DIR}" -name '*.bicep' -o -name '*.bicepparam' | sort)
[[ "${format_failures}" -eq 0 ]] || fail "${format_failures} file(s) are not formatted; run 'az bicep format --outfile <file> <file>'"

log "building and linting every template"
while IFS= read -r file; do
  run_bicep build --stdout "${file}" >/dev/null
  run_bicep lint "${file}"
done < <(find "${BICEP_DIR}" -name '*.bicep' | sort)

log "building every parameter file"
while IFS= read -r file; do
  run_bicep build-params --stdout "${file}" >/dev/null
done < <(find "${BICEP_DIR}/environments" -name '*.bicepparam' | sort)

log "scanning parameter files and templates for secret-shaped values"
"${REPO_ROOT}/infra/scripts/scan_secrets.sh"

log "static infrastructure validation passed"
