metadata description = 'Private Azure Container Registry. Images are pulled by managed identity over a private endpoint.'

@description('Azure region for the registry.')
param location string

@description('Deterministic registry name. Globally unique, alphanumeric, 5-50 characters.')
@minLength(5)
@maxLength(50)
param registryName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Subnet that hosts the private endpoint NIC.')
param privateEndpointSubnetId string

@description('Private DNS zone for privatelink.azurecr.io.')
param privateDnsZoneId string

@description('Log Analytics workspace that receives the registry diagnostics.')
param logAnalyticsWorkspaceId string

@description('Days an untagged manifest is retained before it is cleaned up. Digest-pinned deployed images are always tagged with their commit SHA, so this never removes a deployable revision.')
@minValue(0)
@maxValue(365)
param untaggedRetentionDays int = 30

@description('Set to true only for a deliberately public environment. Note that GitHub-hosted runners push through the registry public endpoint, so a private-only registry additionally requires a self-hosted or VNet-joined runner.')
param publicNetworkAccessEnabled bool = true

// Premium is required for private endpoints and for the retention policy. It is
// the one place where staging cannot use the cheapest tier without giving up a
// stated requirement.
resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // No admin user and no anonymous pull: every pull is a managed identity
    // holding AcrPull, and every push is the OIDC-federated deployment
    // identity holding AcrPush.
    adminUserEnabled: false
    anonymousPullEnabled: false
    dataEndpointEnabled: false
    publicNetworkAccess: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    networkRuleBypassOptions: 'AzureServices'
    zoneRedundancy: 'Disabled'
    policies: {
      // Tags are never moved: a deployed tag is the commit SHA and stays bound
      // to the digest that was pushed for that commit, which is what makes a
      // revision rollback reproducible.
      quarantinePolicy: {
        status: 'disabled'
      }
      trustPolicy: {
        type: 'Notary'
        status: 'disabled'
      }
      retentionPolicy: {
        days: untaggedRetentionDays
        status: 'enabled'
      }
      exportPolicy: {
        status: 'enabled'
      }
    }
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${registryName}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'registry'
        properties: {
          privateLinkServiceId: registry.id
          groupIds: ['registry']
        }
      }
    ]
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'registry'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: registry
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'audit'
        enabled: true
      }
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output registryId string = registry.id
output registryName string = registry.name
output loginServer string = registry.properties.loginServer
