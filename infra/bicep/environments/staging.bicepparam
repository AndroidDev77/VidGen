using '../main.bicep'

// VidGen staging.
//
// Private by default: publicIngressEnabled is false, so the Container Apps
// environment has an internal load balancer only and no application, database,
// cache, vault or storage account is reachable from the internet.
//
// Nothing environment-identifying is committed here. The region, the image
// references, the Temporal Cloud coordinates and the deployment principal all
// arrive as environment variables, so this file contains no subscription id, no
// tenant id, no resource group, no public hostname and no secret value.

param location = readEnvironmentVariable('VIDGEN_AZURE_LOCATION')
param namePrefix = 'vidgen'
param environmentName = 'staging'

param tags = {
  application: 'vidgen'
  environment: 'staging'
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
param vnetAddressPrefix = '10.60.0.0/20'
// The single switch that would publish staging. Leave it false.
param publicIngressEnabled = false

// --- sizing -----------------------------------------------------------------
// Staging is sized for correctness, not throughput. Every ceiling below is
// justified in infra/README.md under "Autoscaling".
param apiResources = { cpu: '0.5', memory: '1Gi' }
param webResources = { cpu: '0.25', memory: '0.5Gi' }
param workerResources = { cpu: '1.0', memory: '2Gi' }

// Minimum one everywhere: the review UI must not cold start, the API must not
// cold start behind it, and a worker at zero replicas is a Temporal task queue
// that nothing polls.
param apiScale = { minReplicas: 1, maxReplicas: 3 }
param webScale = { minReplicas: 1, maxReplicas: 2 }
param workerScale = { minReplicas: 1, maxReplicas: 2 }
param apiConcurrentRequests = 40

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
  parallelism: 1
  completionCount: 1
  resources: { cpu: '2.0', memory: '4Gi' }
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
// Sized against the replica ceilings above: see "PostgreSQL connection budget"
// in infra/README.md, and the assertion in infra/tests/test_infrastructure.py.
param postgresMaxConnections = 200
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
param logRetentionInDays = 30
param logDailyQuotaGb = 5

// --- application ------------------------------------------------------------
param temporal = {
  address: readEnvironmentVariable('VIDGEN_TEMPORAL_ADDRESS')
  namespace: readEnvironmentVariable('VIDGEN_TEMPORAL_NAMESPACE')
  taskQueue: 'vidgen-projects'
  tlsEnabled: true
  gracefulShutdownSeconds: 45
  maxConcurrentActivities: 4
  maxConcurrentWorkflowTasks: 50
}

// Every provider is off. Staging runs the deterministic fake providers, so a
// deployment test cannot spend money, and the application is proven to start
// with Veo, ElevenLabs and VoiceStudio unconfigured. Turning one on means
// enabling it here *and* writing its secret with bootstrap_secrets.sh.
param providers = {
  openai: false
  runway: false
  veo: false
  elevenlabs: false
  voicestudio: false
  opensubtitles: false
}

param features = {
  // "none" while Veo has no renewable federated credential configured.
  repairAlternateProvider: 'none'
  repairAllowParallaxFallback: true
  finalQaAdjudicationEnabled: true
  subtitleSyncEnabled: false
  // True only because every provider above is false. The worker refuses to
  // start with an unconfigured provider, so this is what lets staging run the
  // real pipeline at zero cost - and it is what the smoke test's "committed
  // provider cost is exactly zero" assertion depends on.
  allowFakeProviders: true
}
