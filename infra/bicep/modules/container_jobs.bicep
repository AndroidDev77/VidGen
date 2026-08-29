metadata description = 'Finite Container Apps Jobs: schema migration, out-of-band rendering, the private smoke test and administrative commands.'

import { ProviderToggles, TemporalConfig, FeatureFlags, JobLimits } from './types.bicep'
import { secretReference, env, applicationSecretNames, commonEnv } from './app_config.bicep'

@description('Azure region.')
param location string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Deterministic prefix for the job names.')
param namePrefix string

@description('Container Apps managed environment. The jobs run inside the virtual network, which is what lets the smoke test reach private endpoints and internal ingress without a public path.')
param environmentId string

@description('Registry login server.')
param registryLoginServer string

@description('Immutable application image reference, identical to the one the API and worker run.')
param appImage string

@description('Key Vault URI.')
param keyVaultUri string

@description('Migration job identity.')
param migrationIdentity object

@description('Render job identity.')
param renderIdentity object

@description('Smoke-test job identity.')
param smokeIdentity object

@description('Blob endpoint of the assets storage account.')
param blobEndpoint string

@description('Container that holds the canonical assets.')
param assetsContainerName string

@description('Deployment environment name.')
param deploymentEnvironment string

@description('Temporal Cloud connection.')
param temporal TemporalConfig

@description('Enabled providers. The smoke-test job overrides these to all-false so it can never make a paid call.')
param providers ProviderToggles

@description('Application feature flags.')
param features FeatureFlags

@description('Migration job limits. Parallelism is fixed at one below and cannot be raised by a parameter.')
param migrationLimits JobLimits

@description('Render job limits.')
param renderLimits JobLimits

@description('Smoke-test job limits.')
param smokeLimits JobLimits

@description('Internal FQDN of the API, used by the smoke test.')
param apiFqdn string

@description('Internal FQDN of the web app, used by the smoke test.')
param webFqdn string

@description('PostgreSQL fully qualified domain name, resolved through the private DNS zone by the smoke test.')
param postgresFqdn string

@description('Redis host name, resolved through the private DNS zone by the smoke test.')
param redisHostName string

@description('Key Vault name, used by the smoke test to prove secret resolution works.')
param keyVaultName string

// -- migration ----------------------------------------------------------------

var migrationSecretNames = applicationSecretNames(providers, 'postgres-migration-url')
var migrationEnv = commonEnv(
  deploymentEnvironment,
  blobEndpoint,
  assetsContainerName,
  'postgres-migration-url',
  temporal,
  providers,
  features
)

resource migration 'Microsoft.App/jobs@2025-01-01' = {
  name: '${namePrefix}-job-migrate'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${migrationIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      // Manual only. A migration is started once, deliberately, by the
      // deployment workflow between "infrastructure deployed" and "revisions
      // activated"; nothing schedules or event-triggers it.
      triggerType: 'Manual'
      replicaTimeout: migrationLimits.timeoutSeconds
      // Retrying a schema migration is retrying a partially applied
      // transaction. Alembic is transactional per revision and the script takes
      // an advisory lock, but a failed migration is an operator decision, not
      // an automatic one, so there is no retry.
      replicaRetryLimit: 0
      manualTriggerConfig: {
        // Exactly one pod, which must succeed. Two concurrent Alembic runs
        // against one database is the failure mode this prevents outright,
        // before the advisory lock in scripts/run_migrations.py is even
        // reached.
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryLoginServer
          identity: migrationIdentity.id
        }
      ]
      secrets: [for name in migrationSecretNames: secretReference(keyVaultUri, migrationIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: appImage
          resources: {
            cpu: json(migrationLimits.resources.cpu)
            memory: migrationLimits.resources.memory
          }
          command: ['python']
          // The script upgrades to head, asserts exactly one head, and never
          // downgrades. There is no code path in it that can drop data.
          args: ['-m', 'scripts.run_migrations', '--upgrade-head']
          env: concat(migrationEnv, [
            env('AZURE_CLIENT_ID', migrationIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-migration')
          ])
        }
      ]
    }
  }
}

// -- render -------------------------------------------------------------------

var renderSecretNames = applicationSecretNames(providers, 'postgres-app-url')
var appEnv = commonEnv(
  deploymentEnvironment,
  blobEndpoint,
  assetsContainerName,
  'postgres-app-url',
  temporal,
  providers,
  features
)

// The in-workflow T17 render stays a Temporal activity inside the worker: it is
// part of a durable workflow with retries, heartbeats and a completion gate,
// and moving it here would break that boundary. This job exists for the finite,
// out-of-band case - re-rendering one project deliberately - and its bounded
// parallelism is what keeps that from competing with the workflow for CPU.
resource render 'Microsoft.App/jobs@2025-01-01' = {
  name: '${namePrefix}-job-render'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${renderIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: renderLimits.timeoutSeconds
      replicaRetryLimit: renderLimits.retryLimit
      manualTriggerConfig: {
        // Bounded, never unlimited: rendering is CPU and memory bound and each
        // concurrent render holds a PostgreSQL connection and a provider budget.
        parallelism: renderLimits.parallelism
        replicaCompletionCount: renderLimits.completionCount
      }
      registries: [
        {
          server: registryLoginServer
          identity: renderIdentity.id
        }
      ]
      secrets: [for name in renderSecretNames: secretReference(keyVaultUri, renderIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'render'
          image: appImage
          resources: {
            cpu: json(renderLimits.resources.cpu)
            memory: renderLimits.resources.memory
          }
          command: ['python']
          // VIDGEN_PROJECT_ID is supplied per execution by `az containerapp job
          // start --env-vars`, so the job definition itself is project-neutral.
          args: ['-m', 'scripts.render_project', '--from-env']
          env: concat(appEnv, [
            env('AZURE_CLIENT_ID', renderIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-render')
          ])
        }
      ]
    }
  }
}

// -- administrative commands --------------------------------------------------

// The same image and the same application role, with the command supplied per
// execution. It exists so an operator never needs an interactive shell inside a
// running API or worker replica.
resource admin 'Microsoft.App/jobs@2025-01-01' = {
  name: '${namePrefix}-job-admin'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${renderIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryLoginServer
          identity: renderIdentity.id
        }
      ]
      secrets: [for name in renderSecretNames: secretReference(keyVaultUri, renderIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'admin'
          image: appImage
          resources: {
            cpu: json(renderLimits.resources.cpu)
            memory: renderLimits.resources.memory
          }
          command: ['python']
          args: ['-c', 'print("supply a command with: az containerapp job start --command")']
          env: concat(appEnv, [
            env('AZURE_CLIENT_ID', renderIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-admin')
          ])
        }
      ]
    }
  }
}

// -- smoke test ---------------------------------------------------------------

// Every provider is forced off for the smoke test, in the template rather than
// in the script, so a misconfigured parameter file cannot cause a deployment
// test to spend money. The fake providers cover every route it exercises.
var smokeProviders ProviderToggles = {
  openai: false
  runway: false
  veo: false
  elevenlabs: false
  voicestudio: false
  opensubtitles: false
}
var smokeSecretNames = applicationSecretNames(smokeProviders, 'postgres-app-url')
var smokeEnv = commonEnv(
  deploymentEnvironment,
  blobEndpoint,
  assetsContainerName,
  'postgres-app-url',
  temporal,
  smokeProviders,
  features
)

resource smoke 'Microsoft.App/jobs@2025-01-01' = {
  name: '${namePrefix}-job-smoke'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${smokeIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: smokeLimits.timeoutSeconds
      // One retry absorbs a revision that is still warming up; a second
      // failure is a real failure and rolls the deployment back.
      replicaRetryLimit: smokeLimits.retryLimit
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryLoginServer
          identity: smokeIdentity.id
        }
      ]
      secrets: [for name in smokeSecretNames: secretReference(keyVaultUri, smokeIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'smoke'
          image: appImage
          resources: {
            cpu: json(smokeLimits.resources.cpu)
            memory: smokeLimits.resources.memory
          }
          command: ['python']
          args: ['-m', 'scripts.staging_smoke_test']
          env: concat(smokeEnv, [
            env('AZURE_CLIENT_ID', smokeIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-smoke')
            // The endpoints under test. They are internal names that only
            // resolve inside this virtual network, which is the point: the
            // smoke test runs here rather than on a GitHub-hosted runner
            // precisely because a hosted runner cannot reach them.
            env('VIDGEN_SMOKE_API_URL', 'http://${apiFqdn}')
            env('VIDGEN_SMOKE_WEB_URL', 'http://${webFqdn}')
            env('VIDGEN_SMOKE_POSTGRES_HOST', postgresFqdn)
            env('VIDGEN_SMOKE_REDIS_HOST', redisHostName)
            env('VIDGEN_SMOKE_KEY_VAULT_NAME', keyVaultName)
            env('VIDGEN_SMOKE_BLOB_ACCOUNT_URL', blobEndpoint)
          ])
        }
      ]
    }
  }
}

output migrationJobName string = migration.name
output renderJobName string = render.name
output adminJobName string = admin.name
output smokeJobName string = smoke.name
output migrationParallelism int = migration.properties.configuration.manualTriggerConfig.parallelism
