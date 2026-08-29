metadata description = 'The three continuously running workloads: the review UI, the FastAPI control plane and the Temporal worker.'

import { ContainerResources, ScaleBounds, ProviderToggles, TemporalConfig, FeatureFlags, YouTubePublisherConfig } from './types.bicep'
import { secretReference, env, applicationSecretNames, commonEnv, temporalEnv, youtubeEnv } from './app_config.bicep'

@description('Azure region.')
param location string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Deterministic prefix for the app names.')
param namePrefix string

@description('Container Apps managed environment.')
param environmentId string

@description('Registry login server, for example myregistry.azurecr.io.')
param registryLoginServer string

@description('''Fully qualified, immutable application image reference. A digest
(name@sha256:...) or a commit-SHA tag. Never a moving tag: the deployment
workflow rejects "latest" before it reaches this parameter.''')
param appImage string

@description('Fully qualified, immutable web image reference.')
param webImage string

@description('Key Vault URI, used to build every secret reference.')
param keyVaultUri string

@description('API workload identity.')
param apiIdentity object

@description('Temporal worker identity.')
param workerIdentity object

@description('Web workload identity.')
param webIdentity object

@description('Blob endpoint of the assets storage account.')
param blobEndpoint string

@description('Container that holds the canonical assets.')
param assetsContainerName string

@description('Deployment environment name recorded on every trace and log record.')
param deploymentEnvironment string

@description('Temporal Cloud connection.')
param temporal TemporalConfig

@description('Enabled providers.')
param providers ProviderToggles

@description('Application feature flags.')
param features FeatureFlags

@description('T25 YouTube publication configuration.')
param youtube YouTubePublisherConfig

@description('Per-replica resources for the dedicated publisher worker.')
param publisherResources ContainerResources = { cpu: '0.5', memory: '1Gi' }

@description('''Replica bounds for the publisher worker. Deliberately small: each
replica may hold a multi-gigabyte resumable upload open, and the daily YouTube
upload allowance is far smaller than this worker could saturate.''')
param publisherScale ScaleBounds = { minReplicas: 1, maxReplicas: 2 }

@description('Concurrent resumable uploads per publisher replica.')
@minValue(1)
@maxValue(8)
param publisherMaxConcurrentUploads int = 2

@description('Per-replica resources for the API.')
param apiResources ContainerResources

@description('Per-replica resources for the web tier.')
param webResources ContainerResources

@description('Per-replica resources for the Temporal worker.')
param workerResources ContainerResources

@description('Replica bounds for the API.')
param apiScale ScaleBounds

@description('Replica bounds for the web tier.')
param webScale ScaleBounds

@description('Replica bounds for the Temporal worker.')
param workerScale ScaleBounds

@description('Concurrent HTTP requests per API replica before another replica is added.')
@minValue(1)
param apiConcurrentRequests int = 40

@description('Seconds an API request may run before the ingress terminates it.')
@minValue(1)
@maxValue(3600)
param apiRequestTimeoutSeconds int = 300

@description('''When false the web app has internal ingress only: it is reachable
from inside the virtual network and from a Container Apps Job in this
environment, and there is no public endpoint. Turning it on is the single
explicit configuration change that publishes the environment. The API is never
external, whatever this is set to.''')
param publicIngressEnabled bool = false

// -- shared configuration -----------------------------------------------------

// The application workloads connect as the least-privileged application role.
// The migration job, in container_jobs.bicep, is the only workload that uses
// the migration role.
var databaseSecretName = 'postgres-app-url'
var appSecretNames = applicationSecretNames(providers, databaseSecretName)
var appEnv = concat(
  commonEnv(deploymentEnvironment, blobEndpoint, assetsContainerName, databaseSecretName, temporal, providers, features),
  youtubeEnv(youtube, providers)
)
var workerTemporalEnv = temporalEnv(temporal)

// -- API ----------------------------------------------------------------------

resource api 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-api'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      // Internal, unconditionally. The API is only ever reached through the web
      // tier's reverse proxy or from a job inside this environment; it has no
      // production authentication yet, so it must never be addressable from
      // outside the virtual network.
      ingress: {
        external: false
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
        clientCertificateMode: 'ignore'
        // Resumable uploads stage their parts on the receiving replica's local
        // disk, so a part PUT and the completing POST have to reach the same
        // replica. Affinity is what makes more than one API replica safe until
        // part staging moves into Blob Storage - see infra/README.md.
        stickySessions: {
          affinity: 'sticky'
        }
        // Long enough for a Server-Sent Events stream and for a streamed asset
        // download, bounded so a stuck upstream cannot hold a connection open
        // indefinitely.
        additionalPortMappings: []
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      activeRevisionsMode: 'Multiple'
      maxInactiveRevisions: 10
      registries: [
        {
          server: registryLoginServer
          identity: apiIdentity.id
        }
      ]
      secrets: [for name in appSecretNames: secretReference(keyVaultUri, apiIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: appImage
          resources: {
            cpu: json(apiResources.cpu)
            memory: apiResources.memory
          }
          command: ['uvicorn']
          args: [
            'apps.api.main:app'
            '--host'
            '0.0.0.0'
            '--port'
            '8000'
            // Bounded by the ingress timeout below it, so a request that the
            // proxy has already abandoned does not keep a worker busy.
            '--timeout-graceful-shutdown'
            '30'
          ]
          env: concat(appEnv, [
            env('AZURE_CLIENT_ID', apiIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-api')
            env('VIDGEN_REQUEST_TIMEOUT_SECONDS', string(apiRequestTimeoutSeconds))
          ])
          probes: [
            {
              // Liveness is dependency-free on purpose: a database blip must
              // take a replica out of rotation, not restart it.
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 20
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              // Readiness checks that a pooled connection can be acquired. It
              // never runs a migration.
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 3
              periodSeconds: 5
              timeoutSeconds: 5
              failureThreshold: 30
            }
          ]
        }
      ]
      scale: {
        // Never zero. A cold start behind the review UI reads as an outage, and
        // the ceiling is what keeps concurrent API replicas from exhausting the
        // PostgreSQL connection budget documented in infra/README.md.
        minReplicas: apiScale.minReplicas
        maxReplicas: apiScale.maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(apiConcurrentRequests)
              }
            }
          }
        ]
      }
    }
  }
}

// -- Temporal worker ----------------------------------------------------------

resource worker 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-worker'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workerIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      // No ingress at all: the worker is reached only by polling Temporal
      // Cloud outbound, so it exposes no port and has no address.
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 10
      registries: [
        {
          server: registryLoginServer
          identity: workerIdentity.id
        }
      ]
      secrets: [for name in appSecretNames: secretReference(keyVaultUri, workerIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: appImage
          resources: {
            cpu: json(workerResources.cpu)
            memory: workerResources.memory
          }
          command: ['python']
          args: ['-m', 'workers.temporal_worker.main']
          env: concat(appEnv, workerTemporalEnv, [
            env('AZURE_CLIENT_ID', workerIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-worker')
          ])
        }
      ]
      scale: {
        // Never zero while the task queue requires polling: a scaled-to-zero
        // worker is a queue that nothing drains. CPU and memory are the safe
        // signals here - an HTTP rule would never fire, and a queue-depth rule
        // against Temporal Cloud would need a credential in a scaler.
        minReplicas: workerScale.minReplicas
        maxReplicas: workerScale.maxReplicas
        rules: [
          {
            name: 'cpu-utilisation'
            custom: {
              type: 'cpu'
              metadata: {
                type: 'Utilization'
                value: '70'
              }
            }
          }
          {
            name: 'memory-utilisation'
            custom: {
              type: 'memory'
              metadata: {
                type: 'Utilization'
                value: '75'
              }
            }
          }
        ]
      }
      // Activities are idempotent and Temporal redelivers anything a stopped
      // replica did not complete, so scaling out is safe; the graceful shutdown
      // window above is what keeps scaling *in* from cancelling live work.
      terminationGracePeriodSeconds: temporal.gracefulShutdownSeconds + 15
    }
  }
}

// -- YouTube publisher worker -------------------------------------------------
// A separate app on a separate task queue. A resumable upload can hold an
// activity slot for hours, and sharing the project queue would let one long
// video starve every other project in this environment.

resource publisher 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-publisher'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      // The same worker identity: ACR pull, Key Vault secret read, Blob read
      // and telemetry. It is deliberately not granted Blob write - the
      // publisher only ever reads the final render, the caption and the
      // thumbnail, and writes nothing back to storage.
      '${workerIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      // No ingress at all. Like the Temporal worker, this app is reached only
      // by polling Temporal Cloud outbound, so it exposes no port and has no
      // address; the OAuth callback is served by the API, never by this worker.
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 10
      registries: [
        {
          server: registryLoginServer
          identity: workerIdentity.id
        }
      ]
      secrets: [for name in appSecretNames: secretReference(keyVaultUri, workerIdentity.id, name)]
    }
    template: {
      containers: [
        {
          name: 'publisher'
          // The same application image as the API and the worker: one build,
          // one digest, three entry points.
          image: appImage
          resources: {
            cpu: json(publisherResources.cpu)
            memory: publisherResources.memory
          }
          command: ['python']
          args: ['-m', 'workers.youtube_publisher.main']
          env: concat(appEnv, workerTemporalEnv, [
            env('AZURE_CLIENT_ID', workerIdentity.clientId)
            env('VIDGEN_SERVICE_NAME', 'vidgen-publisher')
            env('VIDGEN_PUBLISHER_MAX_CONCURRENT_UPLOADS', string(publisherMaxConcurrentUploads))
            env('VIDGEN_PUBLISHER_ACTIVITY_THREADS', string(publisherMaxConcurrentUploads))
            // Long enough for an in-flight chunk to finish and commit its
            // confirmed offset. Anything unfinished resumes from that offset.
            env('VIDGEN_PUBLISHER_GRACEFUL_SHUTDOWN_SECONDS', '60')
          ])
        }
      ]
      scale: {
        // Never zero while the publisher queue requires polling, and a small
        // ceiling: this is the backstop on concurrent uploads and on the daily
        // YouTube upload allowance, not a throughput knob.
        minReplicas: publisherScale.minReplicas
        maxReplicas: publisherScale.maxReplicas
        rules: [
          {
            name: 'cpu-utilisation'
            custom: {
              type: 'cpu'
              metadata: {
                type: 'Utilization'
                value: '70'
              }
            }
          }
        ]
      }
      // Comfortably above the worker's own graceful-shutdown window, so a
      // scale-in never cancels an active resumable upload mid-chunk.
      terminationGracePeriodSeconds: 120
    }
  }
}

// -- Web ----------------------------------------------------------------------

resource web 'Microsoft.App/containerApps@2025-01-01' = {
  name: '${namePrefix}-web'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${webIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        // Private by default. `publicIngressEnabled` is the only thing that
        // makes this environment reachable from the internet, and it is false
        // in the staging parameter file.
        external: publicIngressEnabled
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      activeRevisionsMode: 'Multiple'
      maxInactiveRevisions: 10
      registries: [
        {
          server: registryLoginServer
          identity: webIdentity.id
        }
      ]
      // No secrets. The bundle is public by construction and the web tier holds
      // no credential of any kind.
      secrets: []
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          resources: {
            cpu: json(webResources.cpu)
            memory: webResources.memory
          }
          env: [
            // The API endpoint is runtime configuration substituted into the
            // nginx template at container start, so pointing the UI at a
            // different API never requires a rebuild and never changes the
            // image digest.
            // https, not http: the API ingress sets allowInsecure:false, so a
            // plaintext proxy_pass would be answered with a redirect.
            env('VIDGEN_API_UPSTREAM', 'https://${api.properties.configuration.ingress.fqdn}')
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 20
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 3
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: webScale.minReplicas
        maxReplicas: webScale.maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '100'
              }
            }
          }
        ]
      }
    }
  }
}

output apiName string = api.name
output apiFqdn string = api.properties.configuration.ingress.fqdn
output apiLatestRevision string = api.properties.latestRevisionName
output workerName string = worker.name
output workerLatestRevision string = worker.properties.latestRevisionName
output publisherName string = publisher.name
output publisherLatestRevision string = publisher.properties.latestRevisionName
output webName string = web.name
output webFqdn string = web.properties.configuration.ingress.fqdn
output webLatestRevision string = web.properties.latestRevisionName
output applicationSecretNames array = appSecretNames
