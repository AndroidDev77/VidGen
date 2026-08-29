using '../main.bicep'

// VidGen production - A TEMPLATE, NOT A DEPLOYED ENVIRONMENT.
//
// T24 delivers staging. This file exists so the shape of a production
// environment is reviewable and so the differences from staging are explicit,
// not so that anyone can deploy it today. Before it is used, at minimum:
//
//   * production authentication has to exist (T24 deliberately does not add it,
//     and the API currently trusts an X-Vidgen-User header), so
//     publicIngressEnabled MUST stay false until that lands;
//   * a renewable Veo credential has to replace the static token path;
//   * the load and failure behaviour in T26 has to have been measured against
//     these SKUs rather than guessed.
//
// Like the staging file, it contains no subscription id, tenant id, resource
// group, public hostname or secret value.

param location = readEnvironmentVariable('VIDGEN_AZURE_LOCATION')
param namePrefix = 'vidgen'
param environmentName = 'prod'

param tags = {
  application: 'vidgen'
  environment: 'production'
  owner: readEnvironmentVariable('VIDGEN_OWNER', 'platform-team')
  costCenter: readEnvironmentVariable('VIDGEN_COST_CENTER', 'engineering')
  managedBy: 'bicep'
  repository: 'AndroidDev77/VidGen'
}

// Resolved by .github/workflows/deploy-staging.yml to the digest of the image it
// just pushed for the exact commit being deployed. A moving tag is rejected.
param appImage = readEnvironmentVariable('VIDGEN_APP_IMAGE')
param webImage = readEnvironmentVariable('VIDGEN_WEB_IMAGE')

// --- networking -------------------------------------------------------------
// A different range from staging, so the two can be peered or reviewed side by
// side without an overlap.
param vnetAddressPrefix = '10.70.0.0/20'
// Still false. Production ingress waits on production authentication; until
// then the environment is reachable only from inside its virtual network.
param publicIngressEnabled = false

// --- sizing -----------------------------------------------------------------
// Staging is sized for correctness, not throughput. Every ceiling below is
// justified in infra/README.md under "Autoscaling".
param apiResources = { cpu: '1.0', memory: '2Gi' }
param webResources = { cpu: '0.5', memory: '1Gi' }
param workerResources = { cpu: '2.0', memory: '4Gi' }

// Two replicas minimum so a revision replacement or a zone event never leaves
// one instance serving. The ceilings are still finite and still derived from
// postgresMaxConnections below.
param apiScale = { minReplicas: 2, maxReplicas: 10 }
param webScale = { minReplicas: 2, maxReplicas: 6 }
param workerScale = { minReplicas: 2, maxReplicas: 8 }
param apiConcurrentRequests = 60

param migrationLimits = {
  timeoutSeconds: 1800
  retryLimit: 0
  parallelism: 1
  completionCount: 1
  resources: { cpu: '0.5', memory: '1Gi' }
}
param renderLimits = {
  timeoutSeconds: 7200
  retryLimit: 1
  // Still bounded. Render concurrency is never unlimited: each render holds
  // CPU, memory, a PostgreSQL connection and a share of the project budget.
  parallelism: 4
  completionCount: 1
  resources: { cpu: '4.0', memory: '8Gi' }
}
param smokeLimits = {
  timeoutSeconds: 1800
  retryLimit: 1
  parallelism: 1
  completionCount: 1
  resources: { cpu: '1.0', memory: '2Gi' }
}

// --- data services ----------------------------------------------------------
param postgresSkuName = 'Standard_B2s'
param postgresSkuTier = 'Burstable'
param postgresStorageGb = 64
param postgresBackupRetentionDays = 7
param postgresGeoRedundantBackup = false
// Unavailable on Burstable, and staging does not need it.
param postgresZoneRedundant = false
// 10 API + 8 worker + 4 render + 1 migration + 1 smoke = 24 processes, and
// SQLAlchemy's default pool is 5 connections with 10 overflow, so the worst
// case is 360. See "PostgreSQL connection budget" in infra/README.md.
param postgresMaxConnections = 400
param postgresAdministratorPrincipalId = readEnvironmentVariable('VIDGEN_DEPLOY_PRINCIPAL_ID')
param postgresAdministratorPrincipalName = readEnvironmentVariable(
  'VIDGEN_DEPLOY_PRINCIPAL_NAME',
  'vidgen-staging-deployer'
)

param storageSkuName = 'Standard_LRS'
param blobSoftDeleteRetentionDays = 14
param blobScratchRetentionDays = 7

param redisSkuName = 'Balanced_B0'
param redisEvictionPolicy = 'AllKeysLRU'

// --- observability ----------------------------------------------------------
param logRetentionInDays = 90
param logDailyQuotaGb = 50

// --- application ------------------------------------------------------------
param temporal = {
  address: readEnvironmentVariable('VIDGEN_TEMPORAL_ADDRESS')
  namespace: readEnvironmentVariable('VIDGEN_TEMPORAL_NAMESPACE')
  taskQueue: 'vidgen-projects'
  tlsEnabled: true
  gracefulShutdownSeconds: 60
  maxConcurrentActivities: 8
  maxConcurrentWorkflowTasks: 100
}

// The paid providers a real production environment needs. Veo stays off until a
// renewable federated credential replaces the static-token path, and the
// optional narration providers stay off until they are chosen deliberately.
param providers = {
  openai: true
  runway: true
  veo: false
  elevenlabs: false
  voicestudio: false
  opensubtitles: true
}

param features = {
  // Stays "none" while providers.veo is false; the two must agree.
  repairAlternateProvider: 'none'
  repairAllowParallaxFallback: true
  finalQaAdjudicationEnabled: true
  subtitleSyncEnabled: false
  // Never true where a paid provider is enabled: a missing credential must
  // fail loudly, not silently produce fake output for a paying customer.
  allowFakeProviders: false
}
