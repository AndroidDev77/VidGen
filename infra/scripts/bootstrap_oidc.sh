#!/usr/bin/env bash
# One-time setup of GitHub Actions -> Azure authentication via OIDC federation.
#
# There is no client secret anywhere in this flow. GitHub mints a short-lived
# OIDC token for a specific repository, branch or environment, and Azure trusts
# exactly those subjects. Nothing long-lived is created, so nothing has to be
# rotated and nothing can be exfiltrated from the repository settings.
#
# Usage:
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... VIDGEN_AZURE_LOCATION=... \
#   GITHUB_REPOSITORY=owner/name GITHUB_ENVIRONMENT=staging \
#     ./infra/scripts/bootstrap_oidc.sh
#
# It prints the three non-secret values to store as GitHub repository variables
# (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID). None of them is a
# credential on its own: without the federation below they authenticate nothing.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
require_env AZURE_SUBSCRIPTION_ID VIDGEN_RESOURCE_GROUP VIDGEN_AZURE_LOCATION GITHUB_REPOSITORY
: "${GITHUB_ENVIRONMENT:=staging}"
: "${VIDGEN_DEPLOY_APP_NAME:=vidgen-${GITHUB_ENVIRONMENT}-deployer}"

az account set --subscription "${AZURE_SUBSCRIPTION_ID}"

log "ensuring the resource group exists"
az group create \
  --name "${VIDGEN_RESOURCE_GROUP}" \
  --location "${VIDGEN_AZURE_LOCATION}" \
  --output none

log "ensuring the deployment application exists"
app_id="$(az ad app list --display-name "${VIDGEN_DEPLOY_APP_NAME}" --query '[0].appId' -o tsv)"
if [[ -z "${app_id}" ]]; then
  app_id="$(az ad app create --display-name "${VIDGEN_DEPLOY_APP_NAME}" --query appId -o tsv)"
fi

principal_id="$(az ad sp list --filter "appId eq '${app_id}'" --query '[0].id' -o tsv)"
if [[ -z "${principal_id}" ]]; then
  principal_id="$(az ad sp create --id "${app_id}" --query id -o tsv)"
fi

# Federated credentials. One per subject GitHub can present. Adding a subject
# here is the only way to let a new branch or environment deploy.
add_federation() {
  local name="$1" subject="$2"
  if az ad app federated-credential list --id "${app_id}" \
      --query "[?name=='${name}'] | [0].name" -o tsv | grep -q .; then
    log "federated credential ${name} already exists"
    return
  fi
  log "creating federated credential ${name}"
  az ad app federated-credential create --id "${app_id}" --parameters "$(cat <<JSON
{
  "name": "${name}",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "${subject}",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
)" --output none
}

# The environment subject is what the mutating deployment job uses, so an
# approval on the GitHub environment is a precondition for the token existing.
add_federation "github-environment-${GITHUB_ENVIRONMENT}" \
  "repo:${GITHUB_REPOSITORY}:environment:${GITHUB_ENVIRONMENT}"
# Pull requests get a token too, but see the role assignments below: the PR
# subject is only ever used for read-only validation and what-if.
add_federation "github-pull-request" "repo:${GITHUB_REPOSITORY}:pull_request"
add_federation "github-branch-main" "repo:${GITHUB_REPOSITORY}:ref:refs/heads/main"

scope="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${VIDGEN_RESOURCE_GROUP}"

assign_role() {
  local role="$1"
  if az role assignment list --assignee "${principal_id}" --scope "${scope}" \
      --query "[?roleDefinitionName=='${role}'] | [0].id" -o tsv | grep -q .; then
    log "role '${role}' already assigned"
    return
  fi
  log "assigning '${role}' on the resource group"
  az role assignment create \
    --assignee-object-id "${principal_id}" \
    --assignee-principal-type ServicePrincipal \
    --role "${role}" \
    --scope "${scope}" \
    --output none
}

# Scoped to the one resource group, never the subscription. Contributor deploys
# the resources; RBAC Administrator is required because main.bicep creates the
# workload role assignments. Owner is deliberately not used: it would also grant
# the ability to change these very assignments.
assign_role "Contributor"
assign_role "Role Based Access Control Administrator"

tenant_id="$(az account show --query tenantId -o tsv)"

cat <<OUT

Store these as GitHub repository *variables* (not secrets - none is a credential):

  AZURE_CLIENT_ID       ${app_id}
  AZURE_TENANT_ID       ${tenant_id}
  AZURE_SUBSCRIPTION_ID ${AZURE_SUBSCRIPTION_ID}
  AZURE_RESOURCE_GROUP  ${VIDGEN_RESOURCE_GROUP}

And this, for the PostgreSQL Entra administrator parameter:

  VIDGEN_DEPLOY_PRINCIPAL_ID   ${principal_id}
  VIDGEN_DEPLOY_PRINCIPAL_NAME ${VIDGEN_DEPLOY_APP_NAME}

Protect the '${GITHUB_ENVIRONMENT}' GitHub environment with required reviewers:
that approval is what gates every mutating deployment.
OUT
