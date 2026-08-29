metadata description = 'User-assigned managed identities, one per workload, so every role assignment can be read as "this workload may do exactly this".'

@description('Azure region for the identities.')
param location string

@description('Deterministic prefix for the identity names.')
param namePrefix string

@description('Resource tags applied to every resource in this module.')
param tags object

// Separate identities rather than one shared identity: the migration job is the
// only principal that may change the schema, the smoke-test job is the only one
// that may write to the disposable test container prefix, and the render
// workloads never need Key Vault secrets that the API needs. A shared identity
// would collapse all of those into one blast radius.

resource api 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id-api'
  location: location
  tags: tags
}

resource worker 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id-worker'
  location: location
  tags: tags
}

resource render 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id-render'
  location: location
  tags: tags
}

resource migration 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id-migration'
  location: location
  tags: tags
}

resource smoke 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id-smoke'
  location: location
  tags: tags
}

// The web tier serves static assets and proxies to the API. It needs to pull
// its image and nothing else, which is why it gets its own identity rather than
// reusing the API's Key Vault access.
resource web 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id-web'
  location: location
  tags: tags
}

@export()
@description('The identity fields every consuming module needs.')
type WorkloadIdentity = {
  @description('Resource ID, used to attach the identity to a Container App.')
  id: string
  @description('Object ID, used as the principal of a role assignment.')
  principalId: string
  @description('Application (client) ID, used by DefaultAzureCredential to pick this identity.')
  clientId: string
  @description('Resource name.')
  name: string
}

func identityOf(id string, principalId string, clientId string, name string) WorkloadIdentity => {
  id: id
  principalId: principalId
  clientId: clientId
  name: name
}

output api WorkloadIdentity = identityOf(api.id, api.properties.principalId, api.properties.clientId, api.name)
output worker WorkloadIdentity = identityOf(
  worker.id,
  worker.properties.principalId,
  worker.properties.clientId,
  worker.name
)
output render WorkloadIdentity = identityOf(
  render.id,
  render.properties.principalId,
  render.properties.clientId,
  render.name
)
output migration WorkloadIdentity = identityOf(
  migration.id,
  migration.properties.principalId,
  migration.properties.clientId,
  migration.name
)
output smoke WorkloadIdentity = identityOf(
  smoke.id,
  smoke.properties.principalId,
  smoke.properties.clientId,
  smoke.name
)
output web WorkloadIdentity = identityOf(web.id, web.properties.principalId, web.properties.clientId, web.name)
