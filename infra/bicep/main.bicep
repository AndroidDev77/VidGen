metadata description = 'VidGen environment: a private-by-default Container Apps deployment with PostgreSQL, Blob Storage, Azure Managed Redis, Key Vault and Azure Monitor.'

targetScope = 'resourceGroup'

// Deployed at resource-group scope on purpose. The resource group is created by
// infra/scripts/deploy_staging.sh, and the deployment identity therefore never
// needs a subscription-scoped role: Contributor plus Role Based Access Control
// Administrator on this one resource group is enough to deploy everything here.

import { ContainerResources, ScaleBounds, JobLimits, ProviderToggles, TemporalConfig, FeatureFlags, ResourceTags } from './modules/types.bicep'

// -- identity and naming ------------------------------------------------------

@description('Azure region for every resource. Never defaulted: the region is an environment decision.')
param location string

@description('Short lowercase prefix for every resource name. Combined with the environment name it produces deterministic, collision-free names.')
@minLength(3)
@maxLength(10)
param namePrefix string = 'vidgen'

@description('Environment name, used in resource names and recorded on every log and trace record.')
@minLength(2)
@maxLength(12)
param environmentName string

@description('The six tags every resource carries.')
param tags ResourceTags

// -- images -------------------------------------------------------------------

@description('''Immutable application image reference: either registry/name@sha256:...
or registry/name:<40-character commit sha>. The deployment workflow resolves the
digest before calling this template, and the assertion below rejects a moving
tag outright.''')
param appImage string

@description('Immutable web image reference, under the same rules as appImage.')
param webImage string

// A moving tag makes a revision unreproducible and a rollback meaningless.
//
// Bicep has no way to fail a deployment on a computed condition, so this pair
// is surfaced as the `imagesAreImmutable` output and the *enforcement* lives
// where it can actually stop a deployment: `assert_immutable_image` in
// infra/scripts/_common.sh, which deploy_staging.sh calls before it reaches
// Azure, and the policy tests in infra/tests.
@description('False for "latest" or any other non-immutable reference.')
var appImageIsImmutable = !endsWith(appImage, ':latest') && (contains(appImage, '@sha256:') || length(split(
  appImage,
  ':'
)[length(split(appImage, ':')) - 1]) == 40)
var webImageIsImmutable = !endsWith(webImage, ':latest') && (contains(webImage, '@sha256:') || length(split(
  webImage,
  ':'
)[length(split(webImage, ':')) - 1]) == 40)

// -- networking ---------------------------------------------------------------

@description('Address space this environment owns. Subnets are derived from it, so they can never overlap.')
param vnetAddressPrefix string = '10.60.0.0/20'

@description('''False keeps the environment private: the Container Apps environment
has an internal load balancer only, and no application has a public endpoint.
Setting it to true is the single explicit change that publishes the environment.''')
param publicIngressEnabled bool = false

// -- workload sizing ----------------------------------------------------------

@description('Per-replica CPU and memory for the API.')
param apiResources ContainerResources = { cpu: '0.5', memory: '1Gi' }

@description('Per-replica CPU and memory for the web tier.')
param webResources ContainerResources = { cpu: '0.25', memory: '0.5Gi' }

@description('Per-replica CPU and memory for the Temporal worker. Rendering and media probing are memory hungry, so this is the largest of the three.')
param workerResources ContainerResources = { cpu: '1.0', memory: '2Gi' }

@description('Replica bounds for the API.')
param apiScale ScaleBounds = { minReplicas: 1, maxReplicas: 4 }

@description('Replica bounds for the web tier.')
param webScale ScaleBounds = { minReplicas: 1, maxReplicas: 3 }

@description('Replica bounds for the Temporal worker. The minimum is never zero while the task queue requires polling.')
param workerScale ScaleBounds = { minReplicas: 1, maxReplicas: 3 }

@description('Concurrent HTTP requests per API replica before another replica is added.')
param apiConcurrentRequests int = 40

@description('Migration job limits.')
param migrationLimits JobLimits = {
  timeoutSeconds: 1800
  retryLimit: 0
  parallelism: 1
  completionCount: 1
  resources: { cpu: '0.5', memory: '1Gi' }
}

@description('Render job limits. Parallelism is the hard ceiling on concurrent renders.')
param renderLimits JobLimits = {
  timeoutSeconds: 7200
  retryLimit: 1
  parallelism: 2
  completionCount: 1
  resources: { cpu: '2.0', memory: '4Gi' }
}

@description('Smoke-test job limits.')
param smokeLimits JobLimits = {
  timeoutSeconds: 1800
  retryLimit: 1
  parallelism: 1
  completionCount: 1
  resources: { cpu: '1.0', memory: '2Gi' }
}

// -- data services ------------------------------------------------------------

@description('PostgreSQL compute SKU.')
param postgresSkuName string = 'Standard_B2s'

@description('PostgreSQL compute tier.')
param postgresSkuTier string = 'Burstable'

@description('PostgreSQL provisioned storage in GB.')
param postgresStorageGb int = 64

@description('PostgreSQL automated backup retention in days.')
param postgresBackupRetentionDays int = 7

@description('PostgreSQL geo-redundant backup.')
param postgresGeoRedundantBackup bool = false

@description('PostgreSQL zone-redundant high availability. Unavailable on the Burstable tier.')
param postgresZoneRedundant bool = false

@description('PostgreSQL max_connections. Every replica ceiling above is sized against this number.')
param postgresMaxConnections int = 100

@description('Entra ID object id that becomes the PostgreSQL Entra administrator. Supply the deployment identity; never a personal account in an automated deployment.')
param postgresAdministratorPrincipalId string

@description('Display name recorded on the PostgreSQL administrator resource.')
param postgresAdministratorPrincipalName string

@description('Storage replication SKU.')
param storageSkuName string = 'Standard_LRS'

@description('Days a soft-deleted blob or superseded version can be recovered.')
param blobSoftDeleteRetentionDays int = 14

@description('Days after which a blob under the scratch container is deleted.')
param blobScratchRetentionDays int = 7

@description('Azure Managed Redis capacity SKU.')
param redisSkuName string = 'Balanced_B0'

@description('Redis eviction policy. VidGen treats Redis as a cache, so eviction under pressure is correct.')
param redisEvictionPolicy string = 'AllKeysLRU'

// -- observability ------------------------------------------------------------

@description('Log Analytics retention in days. Ingestion and retention are the two largest observability cost drivers.')
param logRetentionInDays int = 30

@description('Daily Log Analytics ingestion cap in GB.')
param logDailyQuotaGb int = 5

// -- application configuration ------------------------------------------------

@description('Temporal Cloud connection. The API key itself lives in Key Vault; none of these fields is a secret.')
param temporal TemporalConfig

@description('Which providers this environment may use. Every one can be false; the application starts either way.')
param providers ProviderToggles = {
  openai: false
  runway: false
  veo: false
  elevenlabs: false
  voicestudio: false
  opensubtitles: false
}

@description('Application feature flags.')
param features FeatureFlags = {
  repairAlternateProvider: 'none'
  repairAllowParallaxFallback: true
  finalQaAdjudicationEnabled: true
  subtitleSyncEnabled: false
  // Off by default: an environment with a paid provider enabled must fail
  // loudly on a missing credential rather than silently emit fake output.
  allowFakeProviders: false
}

// -- deterministic names ------------------------------------------------------

var base = '${namePrefix}-${environmentName}'
// Globally unique names have no separators and are suffixed with a hash of the
// resource group id: deterministic for a given resource group, and different
// across environments and subscriptions.
var unique = uniqueString(resourceGroup().id)
var compactBase = '${namePrefix}${environmentName}'

var resourceTags = union(tags, {})

// Names are computed here rather than read back from module outputs, so they
// are known before the deployment starts. That is what lets the generated-secret
// resources below reference the vault, and it keeps every name a pure function
// of (namePrefix, environmentName, resource group).
var keyVaultName = take('${compactBase}kv${unique}', 24)
var storageAccountName = take('${compactBase}st${unique}', 24)
var registryName = take('${compactBase}acr${unique}', 50)
var applicationInsightsName = '${base}-appi'
var logAnalyticsWorkspaceName = '${base}-log'

// -- modules ------------------------------------------------------------------

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    workspaceName: logAnalyticsWorkspaceName
    applicationInsightsName: applicationInsightsName
    tags: resourceTags
    retentionInDays: logRetentionInDays
    dailyQuotaGb: logDailyQuotaGb
  }
}

module networking 'modules/networking.bicep' = {
  name: 'networking'
  params: {
    location: location
    vnetName: '${base}-vnet'
    namePrefix: base
    tags: resourceTags
    addressPrefix: vnetAddressPrefix
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
  }
}

module privateDns 'modules/private_dns.bicep' = {
  name: 'private-dns'
  params: {
    vnetId: networking.outputs.vnetId
    namePrefix: base
    tags: resourceTags
  }
}

module keyVault 'modules/key_vault.bicep' = {
  name: 'key-vault'
  params: {
    location: location
    keyVaultName: keyVaultName
    tags: resourceTags
    privateEndpointSubnetId: networking.outputs.privateEndpointSubnetId
    privateDnsZoneId: privateDns.outputs.keyVaultZoneId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    publicNetworkAccessEnabled: publicIngressEnabled
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    storageAccountName: storageAccountName
    tags: resourceTags
    privateEndpointSubnetId: networking.outputs.privateEndpointSubnetId
    privateDnsZoneId: privateDns.outputs.blobZoneId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    skuName: storageSkuName
    softDeleteRetentionDays: blobSoftDeleteRetentionDays
    scratchRetentionDays: blobScratchRetentionDays
    publicNetworkAccessEnabled: publicIngressEnabled
  }
}

module registry 'modules/container_registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    registryName: registryName
    tags: resourceTags
    privateEndpointSubnetId: networking.outputs.privateEndpointSubnetId
    privateDnsZoneId: privateDns.outputs.registryZoneId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    // The GitHub-hosted runner pushes images through the registry's public
    // endpoint. Pulls still happen over the private endpoint from inside the
    // virtual network. Set this false once a VNet-joined runner is available.
    publicNetworkAccessEnabled: true
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    serverName: '${base}-pg'
    tags: resourceTags
    delegatedSubnetId: networking.outputs.postgresSubnetId
    privateDnsZoneId: privateDns.outputs.postgresZoneId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGb: postgresStorageGb
    backupRetentionDays: postgresBackupRetentionDays
    geoRedundantBackup: postgresGeoRedundantBackup
    zoneRedundantHighAvailability: postgresZoneRedundant
    maxConnections: postgresMaxConnections
    administratorPrincipalId: postgresAdministratorPrincipalId
    administratorPrincipalName: postgresAdministratorPrincipalName
  }
}

module redis 'modules/redis.bicep' = {
  name: 'redis'
  params: {
    location: location
    redisName: '${base}-redis'
    tags: resourceTags
    privateEndpointSubnetId: networking.outputs.privateEndpointSubnetId
    privateDnsZoneId: privateDns.outputs.redisZoneId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    skuName: redisSkuName
    evictionPolicy: redisEvictionPolicy
    publicNetworkAccessEnabled: publicIngressEnabled
  }
}

module identities 'modules/identities.bicep' = {
  name: 'identities'
  params: {
    location: location
    namePrefix: base
    tags: resourceTags
  }
}

module roleAssignments 'modules/role_assignments.bicep' = {
  name: 'role-assignments'
  params: {
    registryName: registry.outputs.registryName
    keyVaultName: keyVault.outputs.keyVaultName
    storageAccountName: storage.outputs.storageAccountName
    applicationInsightsName: monitoring.outputs.applicationInsightsName
    apiPrincipalId: identities.outputs.api.principalId
    workerPrincipalId: identities.outputs.worker.principalId
    renderPrincipalId: identities.outputs.render.principalId
    migrationPrincipalId: identities.outputs.migration.principalId
    smokePrincipalId: identities.outputs.smoke.principalId
    webPrincipalId: identities.outputs.web.principalId
  }
}

module containerAppsEnvironment 'modules/container_apps_environment.bicep' = {
  name: 'container-apps-environment'
  params: {
    location: location
    environmentName: '${base}-cae'
    tags: resourceTags
    infrastructureSubnetId: networking.outputs.containerAppsSubnetId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    logAnalyticsCustomerId: monitoring.outputs.workspaceCustomerId
    publicIngressEnabled: publicIngressEnabled
  }
}

// -- generated secrets --------------------------------------------------------

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

// The one secret this template writes. It is not a placeholder: it is a value
// Azure generates during this deployment, read through a runtime reference, so
// it never appears as a literal in the template or in a deployment output.
// Every other secret is written by infra/scripts/bootstrap_secrets.sh.
resource applicationInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'applicationinsights-connection-string'
  properties: {
    value: applicationInsights.properties.ConnectionString
    contentType: 'text/plain'
  }
  dependsOn: [keyVault, monitoring]
}

// -- workloads ----------------------------------------------------------------

module containerApps 'modules/container_apps.bicep' = {
  name: 'container-apps'
  params: {
    location: location
    tags: resourceTags
    namePrefix: base
    environmentId: containerAppsEnvironment.outputs.environmentId
    registryLoginServer: registry.outputs.loginServer
    appImage: appImage
    webImage: webImage
    keyVaultUri: keyVault.outputs.keyVaultUri
    apiIdentity: identities.outputs.api
    workerIdentity: identities.outputs.worker
    webIdentity: identities.outputs.web
    blobEndpoint: storage.outputs.blobEndpoint
    assetsContainerName: storage.outputs.assetsContainerName
    deploymentEnvironment: environmentName
    temporal: temporal
    providers: providers
    features: features
    apiResources: apiResources
    webResources: webResources
    workerResources: workerResources
    apiScale: apiScale
    webScale: webScale
    workerScale: workerScale
    apiConcurrentRequests: apiConcurrentRequests
    publicIngressEnabled: publicIngressEnabled
  }
  // A revision that starts before its identity holds AcrPull or Key Vault
  // Secrets User fails to pull or fails to resolve a secret, so the role
  // assignments and the generated secret are explicit prerequisites.
  dependsOn: [roleAssignments, applicationInsightsSecret]
}

module containerJobs 'modules/container_jobs.bicep' = {
  name: 'container-jobs'
  params: {
    location: location
    tags: resourceTags
    namePrefix: base
    environmentId: containerAppsEnvironment.outputs.environmentId
    registryLoginServer: registry.outputs.loginServer
    appImage: appImage
    keyVaultUri: keyVault.outputs.keyVaultUri
    migrationIdentity: identities.outputs.migration
    renderIdentity: identities.outputs.render
    smokeIdentity: identities.outputs.smoke
    blobEndpoint: storage.outputs.blobEndpoint
    assetsContainerName: storage.outputs.assetsContainerName
    deploymentEnvironment: environmentName
    temporal: temporal
    providers: providers
    features: features
    migrationLimits: migrationLimits
    renderLimits: renderLimits
    smokeLimits: smokeLimits
    apiFqdn: containerApps.outputs.apiFqdn
    webFqdn: containerApps.outputs.webFqdn
    postgresFqdn: postgres.outputs.fullyQualifiedDomainName
    redisHostName: redis.outputs.hostName
    keyVaultName: keyVault.outputs.keyVaultName
  }
  dependsOn: [roleAssignments, applicationInsightsSecret]
}

// -- outputs ------------------------------------------------------------------
// Everything the deployment workflow, the operational scripts and the rollback
// path need, and nothing secret.

output resourceGroupName string = resourceGroup().name
output location string = location
output environmentName string = environmentName
output publicIngressEnabled bool = publicIngressEnabled
output imagesAreImmutable bool = appImageIsImmutable && webImageIsImmutable

output registryLoginServer string = registry.outputs.loginServer
output registryName string = registry.outputs.registryName
output keyVaultName string = keyVault.outputs.keyVaultName
output keyVaultUri string = keyVault.outputs.keyVaultUri
output storageAccountName string = storage.outputs.storageAccountName
output blobEndpoint string = storage.outputs.blobEndpoint
output assetsContainerName string = storage.outputs.assetsContainerName
output postgresFqdn string = postgres.outputs.fullyQualifiedDomainName
output postgresDatabaseName string = postgres.outputs.databaseName
output postgresMaxConnections int = postgres.outputs.maxConnections
output redisHostName string = redis.outputs.hostName
output redisPort int = redis.outputs.port

output containerAppsEnvironmentName string = containerAppsEnvironment.outputs.environmentName
output containerAppsEnvironmentInternal bool = containerAppsEnvironment.outputs.internal
output apiAppName string = containerApps.outputs.apiName
output apiFqdn string = containerApps.outputs.apiFqdn
output webAppName string = containerApps.outputs.webName
output webFqdn string = containerApps.outputs.webFqdn
output workerAppName string = containerApps.outputs.workerName

output migrationJobName string = containerJobs.outputs.migrationJobName
output renderJobName string = containerJobs.outputs.renderJobName
output adminJobName string = containerJobs.outputs.adminJobName
output smokeJobName string = containerJobs.outputs.smokeJobName

output logAnalyticsWorkspaceName string = monitoring.outputs.workspaceName
output applicationInsightsName string = monitoring.outputs.applicationInsightsName

output requiredSecretNames array = containerApps.outputs.applicationSecretNames
output workloadIdentityClientIds object = {
  api: identities.outputs.api.clientId
  worker: identities.outputs.worker.clientId
  render: identities.outputs.render.clientId
  migration: identities.outputs.migration.clientId
  smoke: identities.outputs.smoke.clientId
  web: identities.outputs.web.clientId
}
