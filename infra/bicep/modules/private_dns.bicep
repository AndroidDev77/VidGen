metadata description = 'Private DNS zones for every privately linked service, each linked to the virtual network.'

@description('Virtual network that resolves these zones. Only this network is linked, so the names do not resolve anywhere else.')
param vnetId string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Deterministic prefix used for the virtual-network link names.')
param namePrefix string

// Private DNS zone names are fixed by the Azure Private Link service; they are
// not free-form and must match exactly or the private endpoint resolves to the
// public front end instead.
var zoneNames = [
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.vaultcore.azure.net'
  'privatelink.azurecr.io'
  'privatelink.postgres.database.azure.com'
  'privatelink.redis.azure.net'
]

resource zones 'Microsoft.Network/privateDnsZones@2020-06-01' = [
  for zoneName in zoneNames: {
    name: zoneName
    location: 'global'
    tags: tags
  }
]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [
  for (zoneName, index) in zoneNames: {
    parent: zones[index]
    name: '${namePrefix}-link'
    location: 'global'
    tags: tags
    properties: {
      // No auto-registration: every record in these zones is created by a
      // private endpoint's DNS zone group, never by a VM registering itself.
      registrationEnabled: false
      virtualNetwork: {
        id: vnetId
      }
    }
  }
]

output blobZoneId string = zones[0].id
output keyVaultZoneId string = zones[1].id
output registryZoneId string = zones[2].id
output postgresZoneId string = zones[3].id
output redisZoneId string = zones[4].id
output zoneNames array = zoneNames
