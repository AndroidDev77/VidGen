metadata description = 'Least-privilege role assignments. Every assignment is scoped to one resource and uses a deterministic name so redeployment is idempotent.'

// No assignment in this file is made at subscription scope and none grants
// Owner or Contributor. Each workload identity gets the narrowest built-in role
// that lets it do its job, on the single resource it needs.

@description('Container registry that holds the deployed images.')
param registryName string

@description('Key Vault that holds the runtime secrets.')
param keyVaultName string

@description('Storage account that holds the canonical assets.')
param storageAccountName string

@description('Application Insights component the workloads export traces to.')
param applicationInsightsName string

@description('API workload identity principal id.')
param apiPrincipalId string

@description('Temporal worker identity principal id.')
param workerPrincipalId string

@description('Render job identity principal id.')
param renderPrincipalId string

@description('Migration job identity principal id.')
param migrationPrincipalId string

@description('Smoke-test job identity principal id.')
param smokePrincipalId string

@description('Web workload identity principal id.')
param webPrincipalId string

// Built-in role definition GUIDs. They are stable Azure-wide identifiers, not
// environment configuration, which is why they are constants here.
var roles = {
  // Pull an image. Never AcrPush: no runtime workload may publish an image.
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  // Read a secret's value. Never Key Vault Administrator or Secrets Officer:
  // no runtime workload may create, change or delete a secret.
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  // Read and write blobs. Held only by workloads that produce assets.
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  // Read blobs only.
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  // Mint a user delegation key, which is what turns a managed identity into a
  // short-lived signed read URL without any account key existing.
  storageBlobDelegator: 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
  // Publish telemetry to Application Insights. Read access is deliberately not
  // granted: a workload writes its own traces and never reads anyone else's.
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: registryName
}

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' existing = {
  name: storageAccountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

// -- image pull ---------------------------------------------------------------
// Every workload that runs a container needs to pull it, and needs nothing else
// from the registry.
var acrPullPrincipals = [
  apiPrincipalId
  workerPrincipalId
  renderPrincipalId
  migrationPrincipalId
  smokePrincipalId
  webPrincipalId
]

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in acrPullPrincipals: {
    scope: registry
    // guid() over (scope, principal, role) is stable across deployments, so a
    // redeploy updates the existing assignment instead of failing on a
    // conflicting name or creating a duplicate.
    name: guid(registry.id, principalId, roles.acrPull)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// -- Key Vault ----------------------------------------------------------------
// The web tier is absent on purpose: it serves static files and proxies HTTP,
// so it never resolves a secret.
var secretsUserPrincipals = [
  apiPrincipalId
  workerPrincipalId
  renderPrincipalId
  migrationPrincipalId
  smokePrincipalId
]

resource keyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in secretsUserPrincipals: {
    scope: keyVault
    name: guid(keyVault.id, principalId, roles.keyVaultSecretsUser)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// -- Blob Storage -------------------------------------------------------------
// The API, the worker, the render job and the smoke test all write assets. The
// migration job touches only the database, so it gets no blob access at all.
var blobContributorPrincipals = [
  apiPrincipalId
  workerPrincipalId
  renderPrincipalId
  smokePrincipalId
]

resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in blobContributorPrincipals: {
    scope: storageAccount
    name: guid(storageAccount.id, principalId, roles.storageBlobDataContributor)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        roles.storageBlobDataContributor
      )
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// Only the API mints signed read URLs for the review UI, so only the API needs
// to request a user delegation key.
resource blobDelegator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, apiPrincipalId, roles.storageBlobDelegator)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDelegator)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// -- telemetry ----------------------------------------------------------------
var telemetryPublisherPrincipals = [
  apiPrincipalId
  workerPrincipalId
  renderPrincipalId
  migrationPrincipalId
  smokePrincipalId
]

resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in telemetryPublisherPrincipals: {
    scope: applicationInsights
    name: guid(applicationInsights.id, principalId, roles.monitoringMetricsPublisher)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        roles.monitoringMetricsPublisher
      )
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

output roleDefinitionIds object = roles
