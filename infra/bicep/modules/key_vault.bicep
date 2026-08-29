metadata description = 'Azure Key Vault with RBAC authorization, public access disabled and a private endpoint. No secret value is created here.'

@description('Azure region for the vault.')
param location string

@description('Deterministic vault name. Key Vault names are globally unique and limited to 24 characters.')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Subnet that hosts the private endpoint NIC.')
param privateEndpointSubnetId string

@description('Private DNS zone for privatelink.vaultcore.azure.net.')
param privateDnsZoneId string

@description('Log Analytics workspace that receives the audit events.')
param logAnalyticsWorkspaceId string

@description('Soft-delete retention. Purge protection is always on, so a deleted vault cannot be permanently destroyed inside this window.')
@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 30

@description('Set to true only for a deliberately public environment. Staging leaves this false and reaches the vault through the private endpoint.')
param publicNetworkAccessEnabled bool = false

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    // Azure RBAC, not access policies: a workload's access is then one role
    // assignment scoped to this vault, visible in the same place as every
    // other permission, and removable by removing that assignment.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    // Deliberately not parameterised. A staging vault that can be purged is a
    // staging vault whose secret names and versions can be destroyed by a
    // single mistaken command.
    enablePurgeProtection: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: publicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccessEnabled ? 'Allow' : 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${keyVaultName}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: ['vault']
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
        name: 'vault'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        // AuditEvent records every secret read, which is how an unexpected
        // workload reading a provider credential becomes visible.
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

output keyVaultId string = keyVault.id
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
