#!/usr/bin/env bash
# Deploy or update the infrastructure for one environment.
#
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... VIDGEN_AZURE_LOCATION=... \
#   VIDGEN_APP_IMAGE=<registry>/vidgen-app@sha256:... \
#   VIDGEN_WEB_IMAGE=<registry>/vidgen-web@sha256:... \
#   VIDGEN_TEMPORAL_ADDRESS=... VIDGEN_TEMPORAL_NAMESPACE=... \
#   VIDGEN_DEPLOY_PRINCIPAL_ID=... \
#     ./infra/scripts/deploy_staging.sh [--what-if]
#
# This deploys infrastructure only. It does NOT run migrations and it does NOT
# shift traffic: those are separate, ordered steps owned by run_migrations.sh
# and the deployment workflow, because a schema change has to succeed before a
# new application revision can serve.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
resolve_environment
require_env VIDGEN_APP_IMAGE VIDGEN_WEB_IMAGE VIDGEN_TEMPORAL_ADDRESS \
  VIDGEN_TEMPORAL_NAMESPACE VIDGEN_DEPLOY_PRINCIPAL_ID

MODE="deploy"
for argument in "$@"; do
  case "${argument}" in
    --what-if) MODE="what-if" ;;
    --validate) MODE="validate" ;;
    *) fail "unknown argument ${argument}" ;;
  esac
done

# Checked here, before anything reaches Azure, and again by the infrastructure
# tests. A moving tag would make the deployed revision unreproducible.
assert_immutable_image "${VIDGEN_APP_IMAGE}"
assert_immutable_image "${VIDGEN_WEB_IMAGE}"

az account set --subscription "${AZURE_SUBSCRIPTION_ID}"

DEPLOYMENT_NAME="${VIDGEN_DEPLOYMENT_NAME:-vidgen-${VIDGEN_ENVIRONMENT}-$(date -u +%Y%m%d%H%M%S)}"

log "ensuring the resource group exists"
az group create \
  --name "${VIDGEN_RESOURCE_GROUP}" \
  --location "${VIDGEN_AZURE_LOCATION}" \
  --output none

case "${MODE}" in
  validate)
    log "validating the deployment"
    az deployment group validate \
      --resource-group "${VIDGEN_RESOURCE_GROUP}" \
      --template-file "${BICEP_MAIN}" \
      --parameters "${PARAM_FILE}" \
      --output none
    log "validation succeeded"
    ;;
  what-if)
    log "what-if against the current environment"
    az deployment group what-if \
      --resource-group "${VIDGEN_RESOURCE_GROUP}" \
      --template-file "${BICEP_MAIN}" \
      --parameters "${PARAM_FILE}"
    ;;
  deploy)
    log "validating before mutating anything"
    az deployment group validate \
      --resource-group "${VIDGEN_RESOURCE_GROUP}" \
      --template-file "${BICEP_MAIN}" \
      --parameters "${PARAM_FILE}" \
      --output none
    log "deploying ${DEPLOYMENT_NAME}"
    az deployment group create \
      --resource-group "${VIDGEN_RESOURCE_GROUP}" \
      --name "${DEPLOYMENT_NAME}" \
      --template-file "${BICEP_MAIN}" \
      --parameters "${PARAM_FILE}" \
      --output none
    log "deployment ${DEPLOYMENT_NAME} succeeded"
    # Outputs carry names, endpoints and identity client ids. None is a secret.
    deployment_outputs "${DEPLOYMENT_NAME}"
    ;;
esac
