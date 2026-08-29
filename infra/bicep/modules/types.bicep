metadata description = 'Shared parameter types, so every environment file is checked against the same shapes at build time rather than at deployment time.'

@export()
@description('CPU and memory for one replica. CPU is a string because Bicep has no decimal literal; it is converted with json() at the point of use, and Container Apps requires the pair to be one of its supported combinations.')
type ContainerResources = {
  @description('vCPU as a decimal string, for example "0.5".')
  cpu: string
  @description('Memory with a unit, for example "1Gi". Container Apps requires memory to be exactly 2 GiB per vCPU.')
  memory: string
}

@export()
@description('Replica bounds for one workload.')
type ScaleBounds = {
  @minValue(0)
  @description('Minimum replicas. Zero is only safe for a workload that is woken by an inbound request.')
  minReplicas: int
  @minValue(1)
  @description('Maximum replicas. Always finite: it is the backstop for provider concurrency, PostgreSQL connections and render cost.')
  maxReplicas: int
}

@export()
@description('Settings for one finite Container Apps Job.')
type JobLimits = {
  @minValue(60)
  @description('Seconds before the platform terminates the execution. A job that has not finished by then has failed.')
  timeoutSeconds: int
  @minValue(0)
  @description('Retries after a failed execution.')
  retryLimit: int
  @minValue(1)
  @description('Pods started per execution.')
  parallelism: int
  @minValue(1)
  @description('Pods that must succeed for the execution to succeed.')
  completionCount: int
  @description('CPU and memory for the job container.')
  resources: ContainerResources
}

@export()
@description('Which external providers this environment is allowed to use. Every one of these can be false: the application starts, and the deterministic fake providers cover the disabled routes.')
type ProviderToggles = {
  @description('OpenAI: transcription, analysis, script, storyboard, image generation and the QA vision models.')
  openai: bool
  @description('Runway: the default video generation provider.')
  runway: bool
  @description('Google Veo: the T21 alternate-provider repair route. Requires a renewable federated credential, not a static access token.')
  veo: bool
  @description('ElevenLabs: optional narration provider.')
  elevenlabs: bool
  @description('VoiceStudio: optional narration endpoint.')
  voicestudio: bool
  @description('OpenSubtitles: subtitle acquisition.')
  opensubtitles: bool
  @description('YouTube Data API v3: T25 publication. False keeps the deterministic fake publisher, which needs no Google project and calls nothing.')
  youtube: bool
}

@export()
@description('T25 YouTube publication configuration. The OAuth client ID and the redirect URI are ordinary configuration and appear in the browser; the client secret and the token-encryption key are Key Vault secrets and never appear here.')
type YouTubePublisherConfig = {
  @description('OAuth 2.0 client ID for the Google Cloud project. Public by construction: it is in the authorization URL.')
  oauthClientId: string
  @description('The exact redirect URI registered with Google. Must match byte for byte, and must be reachable by an authorized user of this private environment.')
  oauthRedirectUri: string
  @description('Comma-separated same-site paths the OAuth callback may redirect a browser to after authorization.')
  oauthRedirectTargets: string
  @description('Version naming the active token-encryption key in Key Vault. Rotating the key means writing a new secret version and changing this string.')
  tokenEncryptionKeyVersion: string
  @description('Task queue the dedicated publisher worker polls. Separate from the project queue so a multi-hour upload cannot starve ordinary activities.')
  taskQueue: string
  @minValue(262144)
  @description('Resumable upload chunk size in bytes. Must be a multiple of 256 KiB; the application rounds it down to one.')
  uploadChunkBytes: int
  @minValue(60)
  @description('Seconds to keep polling YouTube processing before parking the publication as still processing. Not a failure.')
  processingTimeoutSeconds: int
}

@export()
@description('Temporal Cloud connection for the worker and for the API control plane.')
type TemporalConfig = {
  @description('Temporal Cloud gRPC endpoint, for example "my-namespace.a1b2c.tmprl.cloud:7233". Not a secret.')
  address: string
  @description('Temporal Cloud namespace. Not a secret.')
  namespace: string
  @description('Task queue the production worker polls.')
  taskQueue: string
  @description('TLS to Temporal. Always true against Temporal Cloud.')
  tlsEnabled: bool
  @minValue(1)
  @description('Seconds the worker is given to finish in-flight activities after SIGTERM. Must stay below the platform termination grace period.')
  gracefulShutdownSeconds: int
  @minValue(1)
  @description('Maximum concurrently executing activities per worker replica.')
  maxConcurrentActivities: int
  @minValue(1)
  @description('Maximum concurrent workflow tasks per worker replica.')
  maxConcurrentWorkflowTasks: int
}

@export()
@description('Feature flags forwarded to the application as configuration.')
type FeatureFlags = {
  @description('T21 alternate-provider repair route: "veo" or "none".')
  repairAlternateProvider: string
  @description('T21 free deterministic 2.5D parallax fallback.')
  repairAllowParallaxFallback: bool
  @description('T22 bounded second-opinion adjudication pass.')
  finalQaAdjudicationEnabled: bool
  @description('T09 subtitle timing synchronisation.')
  subtitleSyncEnabled: bool
  @description('''Let the Temporal worker fall back to the deterministic fake
providers for any provider that is disabled. True in staging, where every
provider is off and the worker would otherwise refuse to start; it must be false
wherever a paid provider is enabled, so a misconfigured credential fails loudly
instead of silently producing fake output.''')
  allowFakeProviders: bool
}

@export()
@description('The six tags every resource in this deployment carries.')
type ResourceTags = {
  application: string
  environment: string
  owner: string
  costCenter: string
  managedBy: string
  repository: string
}
