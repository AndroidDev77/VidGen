metadata description = 'Azure Database for PostgreSQL Flexible Server, VNet injected and unreachable from the public internet.'

@description('Azure region for the server.')
param location string

@description('Deterministic server name.')
param serverName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Delegated subnet for VNet injection. Public access is disabled, so this is the only network path to the server.')
param delegatedSubnetId string

@description('Private DNS zone for privatelink.postgres.database.azure.com.')
param privateDnsZoneId string

@description('Log Analytics workspace that receives the server logs and metrics.')
param logAnalyticsWorkspaceId string

@description('Compute SKU. Staging defaults to a burstable tier; production overrides it.')
param skuName string = 'Standard_B2s'

@description('Compute tier matching the SKU above.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param skuTier string = 'Burstable'

@description('Provisioned storage in GB.')
@minValue(32)
param storageSizeGb int = 64

@description('Automated backup retention in days.')
@minValue(7)
@maxValue(35)
param backupRetentionDays int = 7

@description('Geo-redundant backup. Off for staging; a production environment turns it on and pays for it.')
param geoRedundantBackup bool = false

@description('Zone redundant high availability. Off for staging; unavailable on the Burstable tier.')
param zoneRedundantHighAvailability bool = false

@description('PostgreSQL major version.')
param postgresVersion string = '16'

@description('Application database created by the deployment. Migrations run against this database.')
param databaseName string = 'vidgen'

@description('Entra ID principal that owns the server administrators role. Password authentication stays enabled for the two application roles, which the migration job creates.')
param administratorPrincipalId string

@description('Object type of the administrator principal.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param administratorPrincipalType string = 'ServicePrincipal'

@description('Display name of the administrator principal, recorded on the administrator resource.')
param administratorPrincipalName string

@description('Maximum server connections. Every replica bound in infra/README.md is derived from this number, so it is a parameter rather than a server default.')
@minValue(25)
param maxConnections int = 100

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    // Entra-only authentication for the *administrator*. The application and
    // migration roles are ordinary PostgreSQL roles whose passwords live in
    // Key Vault, because the application connects with libpq and needs a role
    // that exists before any workload starts.
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Enabled'
      tenantId: subscription().tenantId
    }
    storage: {
      storageSizeGB: storageSizeGb
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: geoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    highAvailability: {
      mode: zoneRedundantHighAvailability ? 'ZoneRedundant' : 'Disabled'
    }
    network: {
      // VNet injection. `publicNetworkAccess` is read-only when a delegated
      // subnet is set and is reported as Disabled: there is no firewall rule
      // that can expose this server, because there is no public endpoint.
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
    }
  }
}

resource administrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: administratorPrincipalId
  properties: {
    principalType: administratorPrincipalType
    principalName: administratorPrincipalName
    tenantId: subscription().tenantId
  }
}

resource requireTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'require_secure_transport'
  properties: {
    value: 'on'
    source: 'user-override'
  }
}

resource minimumTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'ssl_min_protocol_version'
  properties: {
    value: 'TLSv1.2'
    source: 'user-override'
  }
  dependsOn: [requireTls]
}

resource connectionLimit 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'max_connections'
  properties: {
    value: string(maxConnections)
    source: 'user-override'
  }
  dependsOn: [minimumTls]
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [connectionLimit]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag'
  scope: server
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

output serverId string = server.id
output serverName string = server.name
output fullyQualifiedDomainName string = server.properties.fullyQualifiedDomainName
output databaseName string = database.name
output maxConnections int = maxConnections
