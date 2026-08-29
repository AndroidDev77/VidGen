metadata description = 'Virtual network, non-overlapping subnets and network security groups for the private VidGen environment.'

@description('Azure region for the virtual network.')
param location string

@description('Deterministic virtual network name.')
param vnetName string

@description('Deterministic prefix for the network security groups.')
param namePrefix string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('''The single address space this environment owns. Every subnet below is
carved out of it deterministically with cidrSubnet(), so the subnets can never
overlap and moving the environment to a different range is a one-parameter
change. /20 leaves room for the Container Apps environment, which reserves a
large block, plus growth.''')
param addressPrefix string = '10.60.0.0/20'

@description('Log Analytics workspace that receives the NSG flow diagnostics.')
param logAnalyticsWorkspaceId string

// Container Apps requires a dedicated subnet of at least /27 for workload
// profile environments, and Microsoft recommends /23 or larger because each
// revision consumes addresses while it is being replaced. This is the first
// and largest slice.
var containerAppsPrefix = cidrSubnet(addressPrefix, 23, 0)
// Private endpoints: one address per endpoint, so a /26 is generous for the
// five endpoints this environment creates plus headroom.
var privateEndpointsPrefix = cidrSubnet(addressPrefix, 26, 8)
// PostgreSQL Flexible Server VNet injection needs a subnet delegated
// exclusively to it; /28 is the documented minimum, /27 leaves room for a
// replica or a SKU change that needs more addresses.
var postgresPrefix = cidrSubnet(addressPrefix, 27, 18)

resource containerAppsNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${namePrefix}-nsg-apps'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        // Explicit outbound HTTPS: Temporal Cloud, the configured AI providers,
        // Azure control plane and the container registry are all reached this
        // way. Everything else outbound is denied by the rule below.
        name: 'AllowOutboundHttps'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: containerAppsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '443'
        }
      }
      {
        // Temporal Cloud's gRPC endpoint is TLS on 7233 for mTLS namespaces.
        // API-key namespaces use 443 and are covered above; the rule is kept
        // so switching a namespace to certificates is a parameter change.
        name: 'AllowOutboundTemporalGrpc'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: containerAppsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '7233'
        }
      }
      {
        // The Azure platform DNS and time service live at 168.63.129.16, which
        // is covered by the AzurePlatformDNS tag and NOT by AzureCloud or
        // Internet. Without this rule the deny below would break name
        // resolution for every private endpoint in the environment.
        name: 'AllowOutboundAzurePlatformDns'
        properties: {
          priority: 115
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: containerAppsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzurePlatformDNS'
          destinationPortRange: '*'
        }
      }
      {
        name: 'AllowOutboundDns'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: containerAppsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'AzureCloud'
          destinationPortRange: '53'
        }
      }
      {
        // Container Apps itself needs these: image pulls from the Microsoft and
        // Azure registries, Entra ID for the managed-identity token exchange,
        // and Azure Monitor for log and trace ingestion. They are public
        // addresses and so are already inside AllowOutboundHttps above; naming
        // them separately makes the dependency explicit and survives a future
        // narrowing of the Internet rule.
        name: 'AllowOutboundAzurePlatformServices'
        properties: {
          priority: 125
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: containerAppsPrefix
          sourcePortRange: '*'
          destinationAddressPrefixes: [
            'AzureActiveDirectory'
            'AzureMonitor'
            'AzureContainerRegistry'
            'MicrosoftContainerRegistry'
            'AzureKeyVault'
          ]
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowOutboundVirtualNetwork'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: containerAppsPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      {
        name: 'DenyOtherInternetOutbound'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '*'
        }
      }
      {
        // Nothing outside the virtual network may reach the workloads. Public
        // ingress, when it is explicitly enabled, terminates on the Container
        // Apps managed load balancer and is governed by the environment's
        // `internal` flag rather than by this rule.
        name: 'DenyInternetInbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource privateEndpointsNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${namePrefix}-nsg-pe'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'DenyInternetInbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [addressPrefix]
    }
    subnets: [
      {
        name: 'snet-container-apps'
        properties: {
          addressPrefix: containerAppsPrefix
          networkSecurityGroup: {
            id: containerAppsNsg.id
          }
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          // The workloads reach Storage, Key Vault and the registry through
          // private endpoints, so no service endpoint is needed and none is
          // configured: a service endpoint would widen the path to the public
          // service front end.
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: privateEndpointsPrefix
          networkSecurityGroup: {
            id: privateEndpointsNsg.id
          }
          // Required for private endpoint NIC placement.
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: postgresPrefix
          delegations: [
            {
              name: 'postgres-flexible-server'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
    ]
  }
}

resource containerAppsNsgDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: containerAppsNsg
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output containerAppsSubnetId string = vnet.properties.subnets[0].id
output privateEndpointSubnetId string = vnet.properties.subnets[1].id
output postgresSubnetId string = vnet.properties.subnets[2].id
output containerAppsSubnetPrefix string = containerAppsPrefix
output privateEndpointSubnetPrefix string = privateEndpointsPrefix
output postgresSubnetPrefix string = postgresPrefix
