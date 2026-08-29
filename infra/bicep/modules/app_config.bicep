metadata description = 'The single definition of how VidGen configuration is expressed as Container Apps secrets and environment variables. Apps and jobs both import it, so a running app and a job that has to reproduce its behaviour cannot drift apart.'

import { ProviderToggles, TemporalConfig, FeatureFlags } from './types.bicep'

@export()
@description('A Key Vault backed Container Apps secret, resolved by the workload managed identity at revision start. No secret value passes through the template.')
func secretReference(vaultUri string, identityId string, secretName string) object => {
  name: secretName
  keyVaultUrl: '${vaultUri}secrets/${secretName}'
  identity: identityId
}

@export()
@description('A plain environment variable. Only non-secret configuration - endpoints, names and flags - is expressed this way.')
func env(name string, value string) object => {
  name: name
  value: value
}

@export()
@description('An environment variable whose value comes from a Container Apps secret.')
func envSecret(name string, secretName string) object => {
  name: name
  secretRef: secretName
}

@export()
@description('''Key Vault secret names a workload mounts. `databaseSecretName` differs
between the application workloads and the migration job: they connect as
different PostgreSQL roles. A disabled provider contributes no secret at all,
which is what lets the application start with that provider unconfigured.''')
func applicationSecretNames(providers ProviderToggles, databaseSecretName string) array =>
  union(
    [
      databaseSecretName
      'redis-url'
      'blob-signing-secret'
      'app-signing-secret'
      'applicationinsights-connection-string'
      'temporal-api-key'
    ],
    providers.openai ? ['openai-api-key'] : [],
    providers.runway ? ['runway-api-secret'] : [],
    providers.veo ? ['google-credentials-json'] : [],
    providers.elevenlabs ? ['elevenlabs-api-key'] : [],
    providers.voicestudio ? ['voicestudio-endpoint', 'voicestudio-api-key'] : [],
    providers.opensubtitles ? ['opensubtitles-api-key', 'opensubtitles-username', 'opensubtitles-password'] : []
  )

@export()
@description('Environment variables for the enabled providers only.')
func providerEnv(providers ProviderToggles) array =>
  concat(
    providers.openai ? [envSecret('VIDGEN_OPENAI_API_KEY', 'openai-api-key')] : [],
    providers.runway ? [envSecret('VIDGEN_RUNWAY_API_SECRET', 'runway-api-secret')] : [],
    providers.veo ? [envSecret('VIDGEN_GOOGLE_CREDENTIALS_JSON', 'google-credentials-json')] : [],
    providers.elevenlabs ? [envSecret('VIDGEN_ELEVENLABS_API_KEY', 'elevenlabs-api-key')] : [],
    providers.voicestudio
      ? [
          envSecret('VIDGEN_VOICESTUDIO_ENDPOINT', 'voicestudio-endpoint')
          envSecret('VIDGEN_VOICESTUDIO_API_KEY', 'voicestudio-api-key')
        ]
      : [],
    providers.opensubtitles
      ? [
          envSecret('VIDGEN_OPENSUBTITLES_API_KEY', 'opensubtitles-api-key')
          envSecret('VIDGEN_OPENSUBTITLES_USERNAME', 'opensubtitles-username')
          envSecret('VIDGEN_OPENSUBTITLES_PASSWORD', 'opensubtitles-password')
        ]
      : []
  )

@export()
@description('Configuration every application workload shares. Endpoints and flags stay plain; every credential is a secret reference.')
func commonEnv(
  deploymentEnvironment string,
  blobEndpoint string,
  assetsContainerName string,
  databaseSecretName string,
  temporal TemporalConfig,
  providers ProviderToggles,
  features FeatureFlags
) array =>
  concat(
    [
      env('VIDGEN_ENV', deploymentEnvironment)
      env('VIDGEN_DEPLOYMENT_ENVIRONMENT', deploymentEnvironment)
      env('VIDGEN_BLOB_BACKEND', 'azure')
      env('VIDGEN_BLOB_ACCOUNT_URL', blobEndpoint)
      env('VIDGEN_BLOB_CONTAINER', assetsContainerName)
      env('VIDGEN_TELEMETRY_ENABLED', 'true')
      env('VIDGEN_TELEMETRY_LOGGING_MODE', 'json')
      // The Temporal Cloud connection every workload needs: the API starts and
      // queries workflows, the worker polls, and the smoke test proves the
      // endpoint is reachable. Only the worker's tuning knobs are separate.
      env('VIDGEN_TEMPORAL_TARGET_HOST', temporal.address)
      env('VIDGEN_TEMPORAL_NAMESPACE', temporal.namespace)
      env('VIDGEN_TEMPORAL_TLS_ENABLED', string(temporal.tlsEnabled))
      env('TEMPORAL_ADDRESS', temporal.address)
      env('TEMPORAL_NAMESPACE', temporal.namespace)
      env('TEMPORAL_TLS_ENABLED', string(temporal.tlsEnabled))
      envSecret('VIDGEN_TEMPORAL_API_KEY', 'temporal-api-key')
      envSecret('TEMPORAL_API_KEY', 'temporal-api-key')
      // Never true in a deployed environment: the fake controller would report
      // workflows as running while nothing had been started.
      env('VIDGEN_TEMPORAL_USE_FAKE_WORKFLOW_CONTROLLER', 'false')
      // With every provider disabled the worker would refuse to start, so this
      // is what lets a zero-cost staging environment run the real pipeline.
      env('VIDGEN_TEMPORAL_ALLOW_FAKE_PROVIDERS', string(features.allowFakeProviders))
      env('VIDGEN_REPAIR_ALTERNATE_PROVIDER', features.repairAlternateProvider)
      env('VIDGEN_REPAIR_ALLOW_PARALLAX_FALLBACK', string(features.repairAllowParallaxFallback))
      env('VIDGEN_FINAL_QA_ADJUDICATION_ENABLED', string(features.finalQaAdjudicationEnabled))
      env('VIDGEN_SUBTITLE_SYNC_ENABLED', string(features.subtitleSyncEnabled))
      // The review UI reaches the API same-origin through the web tier's reverse
      // proxy, so no cross-origin request is ever made and CORS stays off.
      env('VIDGEN_CORS_ALLOWED_ORIGINS', '')
      envSecret('VIDGEN_DATABASE_URL', databaseSecretName)
      envSecret('VIDGEN_REDIS_URL', 'redis-url')
      envSecret('VIDGEN_SIGNING_SECRET', 'blob-signing-secret')
      envSecret('VIDGEN_APP_SIGNING_SECRET', 'app-signing-secret')
      envSecret('VIDGEN_APPLICATIONINSIGHTS_CONNECTION_STRING', 'applicationinsights-connection-string')
    ],
    providerEnv(providers)
  )

@export()
@description('The worker-only tuning knobs. The connection itself is in commonEnv, because the API and the smoke test need it too.')
func temporalEnv(temporal TemporalConfig) array => [
  env('TEMPORAL_TASK_QUEUE', temporal.taskQueue)
  env('TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS', string(temporal.gracefulShutdownSeconds))
  env('TEMPORAL_MAX_CONCURRENT_ACTIVITIES', string(temporal.maxConcurrentActivities))
  env('TEMPORAL_MAX_CONCURRENT_WORKFLOW_TASKS', string(temporal.maxConcurrentWorkflowTasks))
  env('TEMPORAL_ACTIVITY_THREADS', string(temporal.maxConcurrentActivities))
]
