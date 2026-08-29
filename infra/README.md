# VidGen Azure infrastructure (T24)

Reproducible, private-by-default Azure infrastructure for a VidGen staging
deployment, expressed entirely in Azure Bicep.

Staging is private. The Container Apps environment has an internal load balancer
only, PostgreSQL is VNet injected, and Blob Storage, Key Vault, the container
registry and Redis are reachable only through private endpoints. Nothing in this
environment is addressable from the internet unless a person changes one
parameter, `publicIngressEnabled`, and redeploys.

---

## Contents

- [Architecture](#architecture)
- [Resource inventory](#resource-inventory)
- [Network topology](#network-topology)
- [Private access](#private-access)
- [Environment parameters](#environment-parameters)
- [Managed identities and RBAC](#managed-identities-and-rbac)
- [Key Vault secrets](#key-vault-secrets)
- [Provider enablement](#provider-enablement)
- [PostgreSQL](#postgresql)
- [Blob Storage](#blob-storage)
- [Redis](#redis)
- [Temporal Cloud](#temporal-cloud)
- [Container topology](#container-topology)
- [Autoscaling](#autoscaling)
- [Observability](#observability)
- [GitHub OIDC bootstrap](#github-oidc-bootstrap)
- [Deployment](#deployment)
- [Migration execution](#migration-execution)
- [Smoke testing](#smoke-testing)
- [Revisions and rollback](#revisions-and-rollback)
- [Backup and recovery](#backup-and-recovery)
- [Cost drivers](#cost-drivers)
- [Validation and tests](#validation-and-tests)
- [Known limitations](#known-limitations)
- [Destroying a disposable environment](#destroying-a-disposable-environment)

---

## Architecture

```
                        ┌──────────────────────────── resource group ─────────────────────────────┐
                        │                                                                          │
   GitHub Actions ──────┼──▶ Container Registry (Premium, private endpoint for pulls)              │
   (OIDC, no secret)    │                                                                          │
                        │   ┌──────────── Container Apps environment (internal, VNet) ──────────┐  │
                        │   │                                                                    │  │
                        │   │  web (nginx, private ingress)  ─▶  api (internal ingress only)     │  │
                        │   │                                        │                            │  │
                        │   │  worker (no ingress, polls Temporal) ──┤                            │  │
                        │   │                                        │                            │  │
                        │   │  jobs: migrate · render · smoke · admin (manual, finite)            │  │
                        │   └────────────────────────────────────────┼───────────────────────────┘  │
                        │                                            │                              │
                        │        private endpoints ──────────────────┼──────────────┐               │
                        │            │              │                │              │               │
                        │      Blob Storage    Key Vault      PostgreSQL Flexible   Azure           │
                        │      (assets,        (RBAC,         (VNet injected)       Managed         │
                        │       scratch)        no public)                          Redis           │
                        │                                                                          │
                        │      Log Analytics ◀── platform + application logs, metrics, diagnostics │
                        │      Application Insights ◀── T23 OpenTelemetry traces                   │
                        └──────────────────────────────────────────────────────────────────────────┘
                                          │
                                          └──▶ Temporal Cloud (outbound HTTPS/gRPC over TLS)
```

Outbound is explicit: the Container Apps subnet NSG allows 443 and 7233 to the
internet for Temporal Cloud and the configured providers, allows DNS and traffic
inside the virtual network, and denies everything else outbound.

## Resource inventory

| Resource | Type | Purpose | Module |
| --- | --- | --- | --- |
| `<prefix>-<env>-log` | `Microsoft.OperationalInsights/workspaces` | Platform logs, application stdout, all diagnostics | `monitoring.bicep` |
| `<prefix>-<env>-appi` | `Microsoft.Insights/components` | T23 OpenTelemetry traces (workspace based) | `monitoring.bicep` |
| `<prefix>-<env>-vnet` | `Microsoft.Network/virtualNetworks` | Three non-overlapping subnets | `networking.bicep` |
| `<prefix>-<env>-nsg-apps`, `-nsg-pe` | `Microsoft.Network/networkSecurityGroups` | Explicit outbound allow-list, inbound deny | `networking.bicep` |
| 5 × `privatelink.*` | `Microsoft.Network/privateDnsZones` | Private name resolution, linked to the VNet only | `private_dns.bicep` |
| `<prefix><env>kv<hash>` | `Microsoft.KeyVault/vaults` | Runtime secrets, RBAC authorisation | `key_vault.bicep` |
| `<prefix><env>st<hash>` | `Microsoft.Storage/storageAccounts` | Canonical assets and disposable scratch | `storage.bicep` |
| `<prefix><env>acr<hash>` | `Microsoft.ContainerRegistry/registries` | Immutable images | `container_registry.bicep` |
| `<prefix>-<env>-pg` | `Microsoft.DBforPostgreSQL/flexibleServers` | Authoritative relational state | `postgres.bicep` |
| `<prefix>-<env>-redis` | `Microsoft.Cache/redisEnterprise` | Cache, rate limiting, ephemeral coordination | `redis.bicep` |
| `<prefix>-<env>-cae` | `Microsoft.App/managedEnvironments` | Internal Container Apps environment | `container_apps_environment.bicep` |
| `<prefix>-<env>-{web,api,worker}` | `Microsoft.App/containerApps` | The three long-running workloads | `container_apps.bicep` |
| `<prefix>-<env>-job-{migrate,render,smoke,admin}` | `Microsoft.App/jobs` | Finite workloads | `container_jobs.bicep` |
| `<prefix>-<env>-id-{api,worker,render,migration,smoke,web}` | `Microsoft.ManagedIdentity/userAssignedIdentities` | One identity per workload | `identities.bicep` |
| (role assignments) | `Microsoft.Authorization/roleAssignments` | Least-privilege, deterministic GUIDs | `role_assignments.bicep` |

Names are a pure function of `namePrefix`, `environmentName` and the resource
group id, so the same parameters always produce the same names. Globally unique
names (vault, storage, registry) take a `uniqueString(resourceGroup().id)`
suffix, which is deterministic for a given resource group.

## Network topology

One `/20` address space, carved with `cidrSubnet()` so overlap is impossible:

| Subnet | Size | Delegation | Purpose |
| --- | --- | --- | --- |
| `snet-container-apps` | `/23` | `Microsoft.App/environments` | Container Apps environment infrastructure. Microsoft requires at least `/27` and recommends `/23`, because replacing a revision consumes addresses while both revisions exist. |
| `snet-private-endpoints` | `/26` | none | Private endpoint NICs. `privateEndpointNetworkPolicies` is `Disabled`, which the platform requires for endpoint placement. |
| `snet-postgres` | `/27` | `Microsoft.DBforPostgreSQL/flexibleServers` | VNet injection for the flexible server. |

Staging uses `10.60.0.0/20`; the production template uses `10.70.0.0/20` so the
two never overlap if they are ever peered.

## Private access

| Service | Public access | Private path |
| --- | --- | --- |
| Container Apps environment | none (`internal: true`) | internal load balancer inside `snet-container-apps` |
| Web app | private ingress | internal FQDN; `publicIngressEnabled` is the only switch |
| API | never external | internal FQDN, reached by the web tier's reverse proxy |
| Temporal worker | no ingress at all | outbound only |
| PostgreSQL | disabled (VNet injected; there is no public endpoint to enable) | delegated subnet + `privatelink.postgres.database.azure.com` |
| Blob Storage | `publicNetworkAccess: Disabled`, `allowBlobPublicAccess: false`, `allowSharedKeyAccess: false` | private endpoint + `privatelink.blob.core.windows.net` |
| Key Vault | `publicNetworkAccess: Disabled`, `defaultAction: Deny` | private endpoint + `privatelink.vaultcore.azure.net` |
| Azure Managed Redis | private endpoint, TLS only | `privatelink.redis.azure.net` |
| Container Registry | public endpoint **enabled** for pushes from GitHub-hosted runners | private endpoint + `privatelink.azurecr.io` for every pull |

The registry is the single deliberate exception, and it is documented under
[Known limitations](#known-limitations).

## Environment parameters

`infra/bicep/environments/staging.bicepparam` is the deployed environment.
`production.example.bicepparam` is a reviewable template that is **not**
deployed. Neither contains a subscription id, tenant id, resource group, region,
public hostname or secret value: everything environment-specific is read from an
environment variable at build time.

| Parameter | Controls | Staging |
| --- | --- | --- |
| `location` | Azure region | `$VIDGEN_AZURE_LOCATION` (no default) |
| `namePrefix`, `environmentName` | Deterministic naming | `vidgen`, `staging` |
| `tags` | The six required tags | see below |
| `appImage`, `webImage` | Immutable image references | `$VIDGEN_APP_IMAGE`, `$VIDGEN_WEB_IMAGE` |
| `vnetAddressPrefix` | Address space | `10.60.0.0/20` |
| `publicIngressEnabled` | Public ingress, and public network access on Key Vault, Storage and Redis | `false` |
| `apiResources`, `webResources`, `workerResources` | CPU and memory per replica | 0.5/1 GiB, 0.25/0.5 GiB, 1/2 GiB |
| `apiScale`, `webScale`, `workerScale` | Replica bounds | min 1 everywhere |
| `apiConcurrentRequests` | HTTP concurrency scale threshold | 40 |
| `migrationLimits`, `renderLimits`, `smokeLimits` | Job timeout, retries, parallelism, completion count, resources | see file |
| `postgresSkuName`, `postgresSkuTier`, `postgresStorageGb` | Database size | `Standard_B2s`, `Burstable`, 64 GB |
| `postgresBackupRetentionDays`, `postgresGeoRedundantBackup`, `postgresZoneRedundant` | Backups and redundancy | 7 days, off, off |
| `postgresMaxConnections` | Connection budget | 200 |
| `storageSkuName` | Blob replication | `Standard_LRS` |
| `blobSoftDeleteRetentionDays`, `blobScratchRetentionDays` | Blob retention | 14, 7 |
| `redisSkuName`, `redisEvictionPolicy` | Redis capacity and eviction | `Balanced_B0`, `AllKeysLRU` |
| `logRetentionInDays`, `logDailyQuotaGb` | Log retention and ingestion cap | 30, 5 |
| `temporal` | Temporal Cloud address, namespace, task queue, TLS, shutdown and concurrency | from environment variables |
| `providers` | Which providers may be used | all `false` |
| `features` | T21/T22/T09 feature flags and `allowFakeProviders` | see file |

Required tags on every resource: `application`, `environment`, `owner`,
`costCenter`, `managedBy`, `repository`.

## Managed identities and RBAC

Six user-assigned identities, one per workload, so every permission reads as
"this workload may do exactly this".

| Identity | AcrPull | Key Vault Secrets User | Blob Data Contributor | Blob Delegator | Monitoring Metrics Publisher |
| --- | :-: | :-: | :-: | :-: | :-: |
| `id-api` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `id-worker` | ✅ | ✅ | ✅ | — | ✅ |
| `id-render` | ✅ | ✅ | ✅ | — | ✅ |
| `id-migration` | ✅ | ✅ | — | — | ✅ |
| `id-smoke` | ✅ | ✅ | ✅ | — | ✅ |
| `id-web` | ✅ | — | — | — | — |

* The web tier holds nothing but image pull: it serves static files and proxies
  HTTP, and never resolves a secret or touches a blob.
* The migration job holds no blob access: it changes the schema and nothing else.
* Only the API holds **Storage Blob Delegator**, because only the API mints the
  user delegation SAS the review UI follows.
* No identity holds `AcrPush`, `Key Vault Secrets Officer`, `Key Vault
  Administrator`, `Owner`, `Contributor` or `User Access Administrator`. A test
  asserts the absence of each of those role GUIDs.
* Every assignment is scoped to one resource and named
  `guid(scopeId, principalId, roleDefinitionId)`, so redeployment is idempotent.

The **deployment identity** is separate and is created by
`bootstrap_oidc.sh`. It holds `Contributor` and `Role Based Access Control
Administrator` **on the one resource group**, never at subscription scope. RBAC
Administrator is required because `main.bicep` creates the workload assignments
above; `Owner` is deliberately not used, because it would also let the
deployment change its own permissions.

## Key Vault secrets

Azure RBAC authorisation, purge protection on, public access disabled. Container
Apps resolves each reference with the workload's managed identity at revision
start: no secret value passes through Bicep, a workflow, an image or a log.

| Secret name | Required | Used by | Notes |
| --- | :-: | --- | --- |
| `postgres-app-url` | yes | api, worker, render, smoke, admin | SQLAlchemy URL for the least-privileged application role |
| `postgres-migration-url` | yes | migration job only | URL for the role that owns the schema |
| `redis-url` | yes | all application workloads | `rediss://` including the access key |
| `blob-signing-secret` | yes | all | HMAC secret for filesystem-store signed URLs |
| `app-signing-secret` | yes | all | application signing secret |
| `temporal-api-key` | yes | all | Temporal Cloud API key |
| `applicationinsights-connection-string` | yes | all | **written by `main.bicep`** from the component it creates |
| `openai-api-key` | when `providers.openai` | all | |
| `runway-api-secret` | when `providers.runway` | all | |
| `google-credentials-json` | when `providers.veo` | all | workload-identity-federation configuration, **not** a static access token |
| `elevenlabs-api-key` | when `providers.elevenlabs` | all | |
| `voicestudio-endpoint`, `voicestudio-api-key` | when `providers.voicestudio` | all | |
| `opensubtitles-api-key`, `opensubtitles-username`, `opensubtitles-password` | when `providers.opensubtitles` | all | |

There is deliberately **no secret pre-check in the deployment workflow**: the
vault has public network access disabled, so a GitHub-hosted runner cannot make
a data-plane call to it and would report every secret as missing. Presence is
enforced where it can be observed - a revision whose Key Vault reference does
not resolve fails to start, so the migration job and then the smoke test fail
the deployment. Run `bootstrap_secrets.sh --check` from a network that can reach
the vault.

Write them with:

```bash
AZURE_SUBSCRIPTION_ID=... VIDGEN_KEY_VAULT_NAME=... ./infra/scripts/bootstrap_secrets.sh
```

The script prompts with echo disabled, accepts values from exported
`VIDGEN_SECRET_*` variables for automation, writes through a pipe rather than
`--value` (which would place the secret in a world-readable `argv`), skips
secrets that already exist unless `--overwrite` is given, and reports missing
required secrets **by name only**. `--check` verifies presence without writing.

`applicationinsights-connection-string` is deliberately absent from that list:
it is generated by the deployment, so `main.bicep` reads it through a runtime
reference and writes it straight into the vault. No other secret value is ever
created by Bicep, and no placeholder value is created at all.

## Provider enablement

Every provider is off in staging. The application starts with Veo, ElevenLabs,
VoiceStudio, OpenAI, Runway and OpenSubtitles all unconfigured: a disabled
provider contributes no Key Vault secret and no environment variable at all, and
the deterministic fake providers cover the routes.

Enabling one means two changes together: set it `true` in `providers`, and write
its secret with `bootstrap_secrets.sh`. Enabling `veo` additionally requires
setting `features.repairAlternateProvider` to `veo`; a test asserts the two never
disagree.

`features.allowFakeProviders` is the flag that makes an all-providers-off
environment actually run. The worker refuses to start with an unconfigured
provider - `build_shot_production_handlers` raises `T16 image generation
provider is not configured` - so staging sets it `true` and runs the
deterministic fakes, which is also what the smoke test's "committed provider
cost is exactly zero" assertion depends on. It must stay `false` wherever a paid
provider is enabled, so that a missing credential fails loudly instead of
silently producing fake output; a test asserts that invariant.

**Veo is intentionally left disabled.** T21's Google adapter currently reads a
static `VIDGEN_GOOGLE_ACCESS_TOKEN`, which is short lived. Deploying one as a
permanent credential would produce an environment that silently stops repairing
shots an hour after deployment. The infrastructure carries a
`google-credentials-json` secret name for workload identity federation, and Veo
stays off until a renewable credential is configured.

**VoiceStudio** is optional in the same way: set `providers.voicestudio` and
write `voicestudio-endpoint` and `voicestudio-api-key`. With it off, narration
uses the configured OpenAI or fake provider.

## PostgreSQL

Azure Database for PostgreSQL Flexible Server, VNet injected into
`snet-postgres`. Because a VNet-injected server has no public endpoint at all,
there is no firewall rule in this deployment and no way to add one that would
expose it.

* `require_secure_transport = on` and `ssl_min_protocol_version = TLSv1.2`.
* Entra ID authentication is enabled and the deployment identity is the server
  administrator. Password authentication stays on for the two application roles,
  because the application connects with libpq and needs roles that exist before
  any workload starts.
* Two roles, two credentials: `postgres-app-url` for the application (least
  privilege) and `postgres-migration-url` for the migration job (owns the
  schema). Create them once against the fresh server, then store the URLs.
* Automated backups with configurable retention; geo-redundancy and zone
  redundancy are parameters, off in staging.
* Diagnostic settings send all logs and metrics to Log Analytics.

### PostgreSQL connection budget

SQLAlchemy's default pool is 5 connections with 10 overflow, so 15 per process
is the worst case. Staging's ceilings are 3 API replicas + 2 worker replicas + 1
render job + 1 migration job + 1 smoke job = 8 processes = 120 connections,
inside `postgresMaxConnections = 200` with room for Azure's own monitoring
sessions. The production template's ceilings are 10 + 8 + 4 + 1 + 1 = 24
processes = 360, inside its `postgresMaxConnections = 400`. `test_replica_ceilings_fit_the_postgres_connection_budget` asserts
this arithmetic, so raising a replica ceiling without raising the budget fails
the build rather than exhausting the server at runtime.

## Blob Storage

Private, key-free, versioned.

* `allowSharedKeyAccess: false` — there is no account key, so there is nothing
  to leak and every signed read URL must be a **user delegation SAS** derived
  from a caller identity that can be revoked.
* `allowBlobPublicAccess: false`, `publicNetworkAccess: Disabled`,
  `supportsHttpsTrafficOnly: true`, `minimumTlsVersion: TLS1_2`.
* Blob versioning and soft delete (blobs and containers), retention configurable.
* Two containers: `assets` for canonical content-addressed output, `scratch` for
  disposable intermediates.
* Lifecycle rules delete `scratch` blobs after `blobScratchRetentionDays` and
  expire only *superseded versions* under `assets` — never a current asset blob.
* No CORS rule by default: the review UI reads assets through the same-origin
  API proxy, so no cross-origin request is ever made.
* Diagnostic settings send audit logs and transaction metrics to Log Analytics.

`AssetService`'s content-addressed identity is preserved exactly.
`AzureBlobStore.put_if_absent` uploads with `overwrite=False`, which the service
enforces as `If-None-Match: *`, so two concurrent activities storing the same
key cannot both believe they created it, and an immutable asset is never
rewritten. Reads and writes stream, so a multi-gigabyte source video is never
materialised in container memory.

Azure Files is deliberately **not** deployed: no workload requires a shared
POSIX filesystem, and adding one would create durable state the architecture
does not want.

## Redis

**Azure Managed Redis** (`Microsoft.Cache/redisEnterprise`), the current
Microsoft-supported managed Redis service. Azure Cache for Redis
(`Microsoft.Cache/redis`) is the previous generation and is on a published
retirement path, so a new environment is built on Managed Redis. Verify the
current guidance before changing the SKU: the offerings do change.

* Private endpoint, `privatelink.redis.azure.net`, TLS 1.2 minimum, encrypted
  client protocol.
* `AllKeysLRU` eviction. That is correct rather than lossy: Redis is a cache, a
  rate limiter and an ephemeral coordination mechanism, and PostgreSQL and
  Temporal remain authoritative for everything durable.
* No AOF or RDB persistence, for the same reason.
* Diagnostic settings send logs and metrics to Log Analytics.

## Temporal Cloud

Temporal Cloud is the workflow backend. The Temporal test server is a test
fixture and is never deployed as infrastructure.

| Setting | Where | Value |
| --- | --- | --- |
| Address | `temporal.address` parameter | `$VIDGEN_TEMPORAL_ADDRESS` |
| Namespace | `temporal.namespace` | `$VIDGEN_TEMPORAL_NAMESPACE` |
| Task queue | `temporal.taskQueue` | `vidgen-projects` |
| TLS | `temporal.tlsEnabled` | `true` |
| API key | Key Vault `temporal-api-key` | resolved by managed identity |
| Reaches | api, worker, render, smoke, admin | the connection is in `commonEnv`, not only on the worker |
| Graceful shutdown | `temporal.gracefulShutdownSeconds` | 45 s, with a 60 s pod termination grace period |
| Activity concurrency | `temporal.maxConcurrentActivities` | 4 per replica |
| Workflow task concurrency | `temporal.maxConcurrentWorkflowTasks` | 50 per replica |

The address, namespace, TLS flag and API key are part of `commonEnv`, so every
workload has them: the API's `TemporalWorkflowController` starts and queries
workflows against Temporal Cloud, and the smoke test proves the endpoint is
reachable. Only the task queue, the graceful-shutdown window and the two
concurrency bounds are worker-only.

The worker reads its own tuning knobs from the environment in
`workers/temporal_worker/main.py` and nowhere else, so a revision change is the
only way any of them can move. It installs SIGTERM and SIGINT handlers and calls
`worker.run(shutdown_event=...)`, so a scale-in or a revision replacement lets
in-flight activities finish or heartbeat a cancellation rather than vanishing
mid-write. Trace context propagates through the existing T23 Temporal
instrumentation.

## Container topology

One application image serves the API, the worker and every job: they share the
same Python dependencies and the same FFmpeg toolchain, so separate images would
only multiply what has to be built, scanned and kept in sync. The workload is
chosen by the container command. The web tier is a separate nginx image.

### Images

Both Dockerfiles pin their base images **by digest**, install dependencies from
the lockfile with `--frozen`, run as a non-root user, carry OCI labels for the
commit SHA and build timestamp, and set an explicit writable `TMPDIR`.

| | Application image | Web image |
| --- | --- | --- |
| Base | `python:3.12.14-slim-trixie@sha256:…` | build `node:24.20.0-alpine3.24@sha256:…`, runtime `nginxinc/nginx-unprivileged:1.29.8-alpine3.23@sha256:…` |
| User | `10001:10001` | `101` (unprivileged nginx) |
| Media tools | `ffmpeg` and `ffprobe` on `PATH` | — |
| CA certificates | yes | yes (base image) |
| Temp | `TMPDIR=/tmp/vidgen`, owned by the runtime user | — |
| Credentials | none; `.dockerignore` excludes `.env*`, `.git` and local data | none; only the public API path and dev identity are compiled in |

Images are deployed **by digest**, never by tag, and never as `latest`. The
deploy script and `infra/tests` both reject any reference that is not a digest
or a 40-character commit SHA tag.

The web tier's API endpoint is *runtime* configuration: the bundle calls the
same-origin `/api` path and nginx substitutes `VIDGEN_API_UPSTREAM` into its
template at container start. Pointing the UI at a different API therefore never
requires a rebuild and never changes the image digest.

Both ingresses set `allowInsecure: false`, so every internal address is `https://`:
the web tier proxies to `https://<api fqdn>` and the smoke test calls the
`https://` FQDNs. nginx forwards `Host $proxy_host` rather than the browser's
host, because Container Apps routes an internal request by its Host header and
terminates TLS with a certificate for that name; the API still sees the original
host in `X-Forwarded-*`.

### Upload session affinity

`UploadService` stages resumable upload parts on the receiving replica's local
disk, so a part `PUT` and the completing `POST` must reach the same replica.
The API ingress therefore sets `stickySessions: { affinity: 'sticky' }`, which
is what makes more than one API replica safe today. Moving part staging into
Blob Storage would remove the constraint; until then, do not remove the
affinity while `apiScale.maxReplicas` is above one.

### Workloads

| Workload | Ingress | Replicas | Notes |
| --- | --- | --- | --- |
| `web` | private (`publicIngressEnabled`) | 1–2 | `/healthz` served by nginx itself, so it reports healthy independently of the API |
| `api` | internal, never external | 1–3 | liveness `/healthz` (dependency-free), readiness `/readyz` (acquires a pooled connection), startup probe; graceful shutdown 30 s; **no Alembic migration at startup**; sticky session affinity, see below |
| `worker` | none | 1–2 | polls Temporal Cloud; graceful activity shutdown; never scales to zero |

### Jobs

| Job | Trigger | Parallelism | Completion | Timeout | Retries |
| --- | --- | :-: | :-: | :-: | :-: |
| `job-migrate` | manual | 1 | 1 | 1800 s | 0 |
| `job-render` | manual | 1 (staging) | 1 | 7200 s | 1 |
| `job-smoke` | manual | 1 | 1 | 1800 s | 1 |
| `job-admin` | manual | 1 | 1 | 3600 s | 0 |

The in-workflow T17 render stays a Temporal activity inside the worker: it is
part of a durable workflow with retries, heartbeats and a completion gate, and
moving it into a finite job would break that boundary. `job-render` exists for
the genuinely finite, out-of-band case — deliberately re-rendering one project —
and takes its project id per execution from `VIDGEN_PROJECT_ID`. No continuously
polling Temporal workload is expressed as a finite job.

## Autoscaling

Every rule below is finite, and every ceiling exists for a stated reason.

| Workload | Rule | Min | Max | Why this is safe |
| --- | --- | :-: | :-: | --- |
| `web` | HTTP concurrency, 100/replica | 1 | 2 | Serving static files is cheap. Minimum 1 because a cold start behind the review UI reads as an outage. |
| `api` | HTTP concurrency, 40/replica | 1 | 3 | Minimum 1 for the same reason. The ceiling is set by the PostgreSQL connection budget above, not by CPU. |
| `worker` | CPU 70% and memory 75% utilisation | 1 | 2 | **Never zero**: a scaled-to-zero worker is a Temporal task queue that nothing drains. Scaling *out* is safe because activities are idempotent and Temporal redelivers anything a stopped replica did not complete; the graceful shutdown window is what makes scaling *in* safe. An HTTP rule would never fire, and a queue-depth scaler would need a Temporal credential inside the scaler. |
| `job-render` | fixed parallelism | — | 1 (staging), 4 (production template) | Render concurrency is never unbounded: each render holds CPU, memory, a PostgreSQL connection and a share of the T23 project budget. |
| `job-migrate` | fixed parallelism | — | 1 | Two concurrent Alembic runs against one database is the failure this prevents outright. |
| `job-smoke` | fixed parallelism | — | 1 | One deployment is being tested. |

Additional bounds the rules respect: provider concurrency is limited by
`temporal.maxConcurrentActivities` per replica (4 × 2 replicas = 8 concurrent
activities in staging); Redis capacity is a cache and evicts rather than
failing; T23 project budgets remain the authoritative money limit and are
unaffected by replica count.

## Observability

The T23 telemetry stack is unchanged in shape; T24 only chooses where it goes.

* **Logs** — T23 writes redacted structured JSON to stdout. Container Apps ships
  stdout to Log Analytics (`ContainerAppConsoleLogs_CL`). Nothing is exported
  twice and no log shipper holds a credential.
* **Traces** — `vidgen.telemetry.bootstrap.initialize_telemetry` installs a
  **batching** span processor exporting to Application Insights when
  `VIDGEN_APPLICATIONINSIGHTS_CONNECTION_STRING` is configured, to a plain OTLP
  endpoint when one is, and to nowhere otherwise. Batching matters: the previous
  `SimpleSpanProcessor` path would block an activity on the exporter.
* **Platform metrics** — replica counts, CPU and memory for Container Apps, plus
  PostgreSQL, Redis and Storage metrics, arrive through `AzureMetrics`.
* **Diagnostic settings** are enabled on PostgreSQL, Storage, Key Vault, Redis,
  the container registry, the Container Apps environment and the NSG.
* **Retention** is `logRetentionInDays` with a `logDailyQuotaGb` ingestion cap,
  so a runaway log loop cannot produce an unbounded bill.

T23 redaction and its bounded label sets are preserved unchanged. The only span
attributes T24 adds are an outcome string, a failure class, a latency and a cost
number — all bounded, none derived from content. No prompt, transcript, caption,
credential, signed URL or media payload reaches telemetry.

### Workbook

`infra/dashboards/vidgen-workbook.json` is an Azure Monitor workbook covering
project completion, stage duration and failures, Temporal activity failures,
provider latency and errors, provider cost and budget denials, worker replicas
and resource usage, render duration and failures, T20 and T22 outcomes,
human-review waits, PostgreSQL health, Redis health and Storage errors. Each
panel is titled with the T23 instrument it corresponds to.

Import it with:

```bash
az monitor workbook create \
  --resource-group "$VIDGEN_RESOURCE_GROUP" \
  --name vidgen-operations \
  --display-name "VidGen operations" \
  --category workbook \
  --serialized-data @infra/dashboards/vidgen-workbook.json
```

The panels query T23 spans and structured logs rather than a Prometheus scrape;
see [Known limitations](#known-limitations) for why.

## GitHub OIDC bootstrap

There is no Azure client secret anywhere in this repository. GitHub mints a
short-lived OIDC token for a specific repository, branch or environment, and
Azure trusts exactly those subjects.

```bash
AZURE_SUBSCRIPTION_ID=...      \
VIDGEN_RESOURCE_GROUP=...      \
VIDGEN_AZURE_LOCATION=...      \
GITHUB_REPOSITORY=AndroidDev77/VidGen \
GITHUB_ENVIRONMENT=staging     \
  ./infra/scripts/bootstrap_oidc.sh
```

It creates the resource group, the deployment application and service principal,
three federated credentials (the `staging` environment, pull requests, and
`refs/heads/main`), and the two resource-group-scoped role assignments. It then
prints the values to store as **GitHub repository variables** — none of them is
a credential on its own:

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `VIDGEN_TEMPORAL_ADDRESS`,
`VIDGEN_TEMPORAL_NAMESPACE`, `VIDGEN_DEPLOY_PRINCIPAL_ID`,
`VIDGEN_DEPLOY_PRINCIPAL_NAME`.

Protect the `staging` GitHub environment with required reviewers: that approval
is what gates every mutating step of the deployment workflow.

## Deployment

### Validate (no Azure credential required)

```bash
./infra/scripts/validate_infrastructure.sh
```

Formatting, `bicep build`, `bicep lint`, parameter-file build, and the secret
scan.

### Deploy

```bash
AZURE_SUBSCRIPTION_ID=...  VIDGEN_RESOURCE_GROUP=...  VIDGEN_AZURE_LOCATION=... \
VIDGEN_APP_IMAGE=<registry>/vidgen-app@sha256:... \
VIDGEN_WEB_IMAGE=<registry>/vidgen-web@sha256:... \
VIDGEN_TEMPORAL_ADDRESS=...  VIDGEN_TEMPORAL_NAMESPACE=... \
VIDGEN_DEPLOY_PRINCIPAL_ID=... \
  ./infra/scripts/deploy_staging.sh
```

`--validate` runs ARM deployment validation only; `--what-if` shows the change
set. `deploy_staging.sh` deploys infrastructure only — it does not run
migrations and does not shift traffic.

### Order

```
deploy infrastructure
  → publish immutable images
  → start the migration job once
  → wait for successful completion
  → verify exactly one Alembic head
  → activate the application revisions
  → run the private smoke test
```

`.github/workflows/deploy-staging.yml` performs exactly this order, and a failed
migration exits the job before the revision-activation step can run.

## Migration execution

```bash
AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... VIDGEN_MIGRATION_JOB=... \
  ./infra/scripts/run_migrations.sh
```

Three independent guarantees, because one is not enough:

1. The job is `parallelism: 1`, `replicaCompletionCount: 1`, `replicaRetryLimit: 0`.
2. `run_migrations.sh` refuses to start a second execution while one is running.
3. `scripts/run_migrations.py` takes a session-level PostgreSQL advisory lock
   before invoking Alembic, which covers the case a platform setting cannot: a
   manual `az containerapp job start` alongside a running deployment.

The script also asserts the script directory has exactly one head *before*
applying anything, and that the database is at that head afterwards. It contains
no downgrade code path at all. Migration failure exits non-zero, which blocks
the traffic switch. Neither the API nor the worker runs a migration at startup.

## Smoke testing

```bash
AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... VIDGEN_SMOKE_JOB=... \
  ./infra/scripts/run_smoke_test.sh
```

The smoke test runs as a Container Apps Job **inside the virtual network**,
because the API has internal ingress and every data service is behind a private
endpoint: a GitHub-hosted runner cannot reach any of them, and is not asked to.
No credential is passed to the job.

Connectivity phase: FFmpeg and ffprobe on `PATH`, T23 telemetry initialisation,
private DNS resolution (asserting every privately linked name resolves to a
*private* address, which is how a missing or unlinked DNS zone is caught), Key
Vault reference resolution, PostgreSQL connectivity and TLS and a single Alembic
head, a TLS Redis `PING`, a Blob Storage write-read-verify with the job
identity, a Temporal Cloud connection, and the API liveness, API readiness and
web health endpoints.

End-to-end phase (`--mode e2e`, the default): create a clearly tagged throwaway
project named `smoke-test <commit> <timestamp>` owned by `vidgen-smoke-test`,
build a small synthetic MP4 with the image's own FFmpeg, upload it, mint a
signed read URL for the stored asset, start the workflow, wait for a terminal
state, then assert worker activity execution, the T22 completion gate, and a
committed provider cost of exactly zero.

**No paid provider can be called.** Every provider is forced `false` in the
smoke job's template, not in the script, so a wrong parameter file cannot cause
a deployment test to spend money — and the job therefore mounts no provider
secret at all.

## Revisions and rollback

The web and API apps run in `Multiple` revision mode with explicit traffic
weights; the worker has no ingress and runs in `Single` mode.

```bash
AZURE_SUBSCRIPTION_ID=... VIDGEN_RESOURCE_GROUP=... \
  ./infra/scripts/rollback_revision.sh <app-name> [target-revision]
```

For an app with ingress it identifies the most recent active, healthy revision
that is not the current one and sends 100% of traffic there. For the worker it
redeploys the previous revision's image. In both cases the failed revision is
left in place, so its logs and configuration remain available for diagnosis, and
`maxInactiveRevisions: 10` keeps them.

Rollback **never** deletes a database row, a blob or an image, and **never** runs
`alembic downgrade`.

### Schema compatibility and rollback

Restoring the previous application revision is only safe because every migration
is expected to be backwards-compatible with the immediately preceding
application revision. Concretely:

* Adding a nullable column, a table, or an index is safe.
* Dropping or renaming a column, or narrowing a constraint, is **not**: the
  previous revision still writes the old shape.
* A change of that kind must be split across two deployments — an *expand*
  migration that adds the new shape while the old one still works, then a
  *contract* migration in a later deployment once no rolled-back revision could
  still be running.

If a migration ever violates this, rollback is not available for that deployment
and the fix is forward.

Deployed image digests are retained in ACR according to the registry retention
policy, which removes untagged manifests after `untaggedRetentionDays`. Every
deployed image is tagged with its commit SHA, so no deployable revision is ever
removed by it.

## Backup and recovery

| Asset | Mechanism | Recovery |
| --- | --- | --- |
| PostgreSQL | automated backups, `postgresBackupRetentionDays` (7 in staging), optional geo-redundancy | `az postgres flexible-server restore --restore-time ...` creates a new server; repoint `postgres-app-url` and `postgres-migration-url` |
| Blob assets | versioning + soft delete, `blobSoftDeleteRetentionDays` (14) | `az storage blob undelete` or restore a version; the content hash is the identity, so a restored blob is byte-identical or it is not the asset |
| Key Vault | soft delete (30 days) + purge protection | `az keyvault recover`; a deleted vault cannot be permanently destroyed inside the window |
| Redis | none, by design | rebuilt from PostgreSQL and Temporal, which are authoritative |
| Images | ACR retention policy | redeploy by digest |
| Infrastructure | this repository | `deploy_staging.sh` reproduces the environment from the same parameters |

## Cost drivers

Sized for correctness, not throughput. The largest drivers, in rough order, and
what moves each:

| Driver | Parameter | Note |
| --- | --- | --- |
| Container Apps replica-seconds | `apiScale`, `webScale`, `workerScale`, `*Resources` | The worker's minimum of one replica is a continuous cost and is deliberate. |
| PostgreSQL compute and storage | `postgresSkuName`, `postgresSkuTier`, `postgresStorageGb` | `Burstable` is the cheapest tier that supports VNet injection. |
| Azure Managed Redis | `redisSkuName` | `Balanced_B0` is the smallest tier. |
| Log ingestion and retention | `logRetentionInDays`, `logDailyQuotaGb` | Usually the largest surprise. The daily cap is the backstop. |
| Private endpoints | fixed | Five endpoints, each billed hourly plus data processed. This is the price of a private environment. |
| Container Registry | fixed | **Premium** is required for private endpoints and the retention policy — the one place staging cannot use the cheapest tier without giving up a stated requirement. |
| Blob Storage | `storageSkuName`, retention parameters | Versioning and soft delete both retain data that is billed. |
| Data egress | — | Provider calls and Temporal Cloud traffic. |
| Temporal Cloud | external | Billed by Temporal, per action and per namespace. |
| Paid providers | `providers` | Zero in staging: every provider is off. |

No cost estimate is hardcoded anywhere: prices change and a stale number in a
README is worse than none. T23 remains responsible for provider cost accounting;
Azure infrastructure cost is deliberately **not** inserted into the T23 provider
ledger, which measures generation spend per project.

## Validation and tests

Locally, with no Azure credential:

```bash
make verify                                   # ruff, mypy --strict, pytest, schema export
uv run pytest -q                              # includes infra/tests
pnpm --filter @vidgen/contracts typecheck
az bicep build --file infra/bicep/main.bicep
./infra/scripts/validate_infrastructure.sh    # format, build, lint, params, secret scan
uv run alembic heads                          # must print exactly one head
```

`infra/tests/` compiles the Bicep sources and asserts, among other things: every
template compiles; names are deterministic; the six tags exist; public ingress
is off by default and off in staging; the API is never external; the worker has
no ingress; PostgreSQL, Storage, Key Vault and Redis are private; every private
endpoint has a DNS zone group; subnets cannot overlap; Key Vault uses RBAC; no
role assignment grants Owner, Contributor, AcrPush or a Key Vault writer role;
role-assignment names are deterministic; secrets are Key Vault references with
no literal values; probes, resource limits and scale bounds exist; the worker
never scales to zero; render concurrency is bounded; migration parallelism is
one with no retry and its own database role; the migration script has no
downgrade path; the smoke job cannot call a paid provider and runs inside the
private environment; diagnostic settings are enabled everywhere; parameter files
contain no secret or subscription identity; and the replica ceilings fit the
PostgreSQL connection budget.

CI runs what a laptop cannot: Docker image builds, the image assertions (FFmpeg
and ffprobe present, non-root user, OCI labels, no credential in the image,
health endpoints answering), PostgreSQL/Redis/Azurite/Temporal integration, the
Temporal test-server suites, Compose validation, ARM deployment validation and
`what-if`, and the manually approved staging deployment.

## Known limitations

1. **No production authentication.** The API trusts an `X-Vidgen-User` header
   (`apps/api/auth.py`). That is precisely why staging is private by default and
   why the production template also keeps `publicIngressEnabled` false.
   Enabling public ingress before authentication exists would expose every
   project to anyone who can set a header.
2. **The container registry's public endpoint is enabled.** GitHub-hosted
   runners push through it. Pulls use the private endpoint. Closing it requires
   a VNet-joined or self-hosted runner; the parameter is already there.
3. **Upload parts are staged on local disk.** Session affinity on the API
   ingress keeps a multi-part upload on one replica, but a replica that is
   replaced mid-upload loses the staged parts and the client has to restart the
   upload. Staging parts in Blob Storage is the real fix and is application
   work, not infrastructure work.
4. **Metrics are not scraped.** T23's `recap_*` instruments are
   `prometheus_client` objects owned per pipeline instance, with no
   process-wide registry, so there is no endpoint to scrape and no correct way
   to bridge them without restructuring T23's metric ownership. The workbook is
   therefore built on T23 spans, T23 structured logs and Azure platform metrics,
   with each panel titled by the instrument it corresponds to. Wiring a
   process-wide registry is T23 work.
5. **Veo is disabled.** See [Provider enablement](#provider-enablement).
6. **Azure Monitor Private Link Scope is not deployed.** Log and trace ingestion
   uses the public Azure Monitor endpoints over TLS from inside the VNet. An
   AMPLS would close that path and is a reasonable follow-up; it also affects
   every workspace in the scope, so it is a deliberate decision rather than a
   default.
7. **PostgreSQL roles are created manually.** The application and migration
   roles are created once against a fresh server and their URLs stored in Key
   Vault. Bicep cannot create a PostgreSQL role, and adding a migration to do it
   would put credential management into the application schema.
8. **The production parameter file is a template.** It has never been deployed,
   and its SKUs are reasoned rather than measured. T26 load testing is what
   would justify them.

## Destroying a disposable environment

Only ever do this to an environment you are certain is disposable. It deletes
every project, every asset and every log.

```bash
# 1. Confirm which environment you are about to destroy.
az group show --name "$VIDGEN_RESOURCE_GROUP" --query 'tags' -o json

# 2. Delete the resource group.
az group delete --name "$VIDGEN_RESOURCE_GROUP" --yes

# 3. The Key Vault survives as a soft-deleted vault, because purge protection is
#    on. It cannot be purged and will expire on its own after the retention
#    window; a redeployment into the same resource group recovers it.
az keyvault list-deleted --query "[?properties.vaultId contains '$VIDGEN_RESOURCE_GROUP']" -o table
```

Purge protection is deliberately not parameterised: a vault that can be purged
is a vault whose secret names and versions can be destroyed by one mistaken
command.
