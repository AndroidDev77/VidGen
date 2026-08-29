metadata description = 'Private Blob Storage for immutable, content-addressed project assets.'

@description('Azure region for the storage account.')
param location string

@description('Deterministic storage account name. Globally unique, lowercase alphanumeric, 3-24 characters.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Subnet that hosts the private endpoint NIC.')
param privateEndpointSubnetId string

@description('Private DNS zone for privatelink.blob.')
param privateDnsZoneId string

@description('Log Analytics workspace that receives the blob diagnostics.')
param logAnalyticsWorkspaceId string

@description('Replication SKU. Staging defaults to locally redundant; production sets zone or geo redundancy explicitly.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
  'Standard_GZRS'
])
param skuName string = 'Standard_LRS'

@description('Container that holds the canonical content-addressed assets written by AssetService.')
param assetsContainerName string = 'assets'

@description('Container for disposable intermediate artifacts. Everything here is covered by the lifecycle rule below.')
param scratchContainerName string = 'scratch'

@description('Days a soft-deleted blob or a non-current version can still be recovered.')
@minValue(1)
@maxValue(365)
param softDeleteRetentionDays int = 14

@description('Days after which a blob under the scratch container is deleted. Scratch is disposable by construction, so this is the main storage cost control.')
@minValue(1)
@maxValue(365)
param scratchRetentionDays int = 7

@description('Set to true only for a deliberately public environment.')
param publicNetworkAccessEnabled bool = false

@description('Allowed browser origins. Left empty on purpose: the review UI reads assets through the same-origin API proxy, so no CORS rule is needed.')
param allowedCorsOrigins array = []

resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    // Managed identity only. With key access disabled there is no account key
    // to leak, and every signed read URL has to be a user delegation SAS
    // derived from a caller identity that can be revoked.
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    defaultToOAuthAuthentication: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccessEnabled ? 'Allow' : 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
    encryption: {
      requireInfrastructureEncryption: false
      keySource: 'Microsoft.Storage'
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    // Versioning plus soft delete is what makes an "immutable" content-addressed
    // asset actually recoverable: AssetService never overwrites a key, so a
    // version can only appear through an operational mistake, and soft delete
    // is the window in which that mistake can be undone.
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: softDeleteRetentionDays
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: softDeleteRetentionDays
    }
    cors: {
      corsRules: [
        for origin in allowedCorsOrigins: {
          allowedOrigins: [origin]
          allowedMethods: ['GET', 'HEAD']
          allowedHeaders: ['*']
          exposedHeaders: ['*']
          maxAgeInSeconds: 300
        }
      ]
    }
  }
}

resource assetsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: assetsContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource scratchContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: scratchContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-scratch'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${scratchContainerName}/']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: scratchRetentionDays
                }
              }
              version: {
                delete: {
                  daysAfterCreationGreaterThan: scratchRetentionDays
                }
              }
            }
          }
        }
        {
          // Canonical assets are never deleted by policy - they are the
          // product. Only superseded *versions*, which should not exist at
          // all, are cleaned up, and only well after the soft-delete window.
          name: 'expire-superseded-asset-versions'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${assetsContainerName}/']
            }
            actions: {
              version: {
                delete: {
                  daysAfterCreationGreaterThan: softDeleteRetentionDays * 2
                }
              }
            }
          }
        }
      ]
    }
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${storageAccountName}-pe-blob'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storageAccount.id
          groupIds: ['blob']
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
        name: 'blob'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: blobService
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
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

output storageAccountId string = storageAccount.id
output storageAccountName string = storageAccount.name
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output assetsContainerName string = assetsContainer.name
output scratchContainerName string = scratchContainer.name
