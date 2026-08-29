metadata description = 'Azure Managed Redis (Microsoft.Cache/redisEnterprise), private and TLS only.'

// Azure Managed Redis is the current Microsoft-supported managed Redis service
// and is modelled by the Microsoft.Cache/redisEnterprise resource type with a
// required `databases` child. Azure Cache for Redis (Microsoft.Cache/redis) is
// the previous generation and is on a published retirement path, so a new
// environment is built on Managed Redis.
//
// Redis stays a cache, a rate limiter and an ephemeral coordination mechanism.
// PostgreSQL and Temporal remain authoritative, which is why the eviction
// policy below is allowed to discard keys under memory pressure.

@description('Azure region for the cluster.')
param location string

@description('Deterministic cluster name.')
param redisName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Subnet that hosts the private endpoint NIC.')
param privateEndpointSubnetId string

@description('Private DNS zone for privatelink.redis.azure.net.')
param privateDnsZoneId string

@description('Log Analytics workspace that receives the connection diagnostics.')
param logAnalyticsWorkspaceId string

@description('Azure Managed Redis capacity SKU. Balanced_B0 is the smallest tier and is the staging default.')
param skuName string = 'Balanced_B0'

@description('Eviction policy. VidGen treats Redis as a cache, so evicting the least recently used key is correct behaviour under pressure, not data loss.')
@allowed([
  'AllKeysLRU'
  'AllKeysLFU'
  'VolatileLRU'
  'NoEviction'
])
param evictionPolicy string = 'AllKeysLRU'

@description('Set to true only for a deliberately public environment.')
param publicNetworkAccessEnabled bool = false

resource redis 'Microsoft.Cache/redisEnterprise@2025-04-01' = {
  name: redisName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    minimumTlsVersion: '1.2'
    highAvailability: 'Disabled'
  }
}

resource database 'Microsoft.Cache/redisEnterprise/databases@2025-04-01' = {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'OSSCluster'
    evictionPolicy: evictionPolicy
    // No persistence: everything in this cache is reconstructable from
    // PostgreSQL and Temporal, and persistence would only add cost and a
    // second copy of data that is deliberately ephemeral.
    persistence: {
      aofEnabled: false
      rdbEnabled: false
    }
    accessKeysAuthentication: 'Enabled'
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (!publicNetworkAccessEnabled) {
  name: '${redisName}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'redis'
        properties: {
          privateLinkServiceId: redis.id
          groupIds: ['redisEnterprise']
        }
      }
    ]
  }
  dependsOn: [database]
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (!publicNetworkAccessEnabled) {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'redis'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: database
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
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

output redisId string = redis.id
output redisName string = redis.name
output hostName string = redis.properties.hostName
output port int = database.properties.port
