metadata description = 'VNet-integrated Container Apps managed environment. Internal by default: no public ingress endpoint exists unless it is explicitly enabled.'

@description('Azure region for the environment.')
param location string

@description('Deterministic environment name.')
param environmentName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Delegated Container Apps infrastructure subnet.')
param infrastructureSubnetId string

@description('Log Analytics workspace that receives console and system logs.')
param logAnalyticsWorkspaceId string

@description('Log Analytics workspace customer (GUID) id.')
param logAnalyticsCustomerId string

@description('''When false the environment has an internal load balancer only: every
ingress-enabled app is reachable only from inside the virtual network, and no
public FQDN resolves to it. Turning this on is the single explicit change that
makes the environment publicly addressable.''')
param publicIngressEnabled bool = false

@description('Availability-zone redundancy. Off for staging; it doubles the subnet requirements and the cost.')
param zoneRedundant bool = false

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      // `internal: true` is the private-by-default switch. It is derived from
      // publicIngressEnabled rather than set independently so the two can never
      // disagree.
      internal: !publicIngressEnabled
    }
    zoneRedundant: zoneRedundant
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
      }
    }
    workloadProfiles: [
      {
        // Consumption scales to the replica bounds each app declares and bills
        // per running replica-second, which is what keeps a staging
        // environment with a permanently-running worker affordable.
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: managedEnvironment
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

output environmentId string = managedEnvironment.id
output environmentName string = managedEnvironment.name
output defaultDomain string = managedEnvironment.properties.defaultDomain
output staticIp string = managedEnvironment.properties.staticIp
output internal bool = !publicIngressEnabled
