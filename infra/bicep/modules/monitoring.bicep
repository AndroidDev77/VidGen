metadata description = 'Log Analytics workspace, Application Insights and the shared diagnostic-settings target for every VidGen resource.'

@description('Azure region for the workspace and the Application Insights component.')
param location string

@description('Deterministic Log Analytics workspace name.')
param workspaceName string

@description('Deterministic Application Insights component name.')
param applicationInsightsName string

@description('Resource tags applied to every resource in this module.')
param tags object

@description('Log Analytics retention in days. Ingestion and retention are the two largest observability cost drivers, so staging keeps this short.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Daily ingestion cap in GB. A runaway log loop cannot produce an unbounded bill; -1 disables the cap.')
param dailyQuotaGb int = 5

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
    features: {
      // Queries are authenticated through Entra ID and Azure RBAC only. A
      // workspace key cannot be used to read logs.
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: false
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// Workspace-based Application Insights: telemetry lands in the same workspace,
// so the workbook can join application traces against platform logs in one
// query and only one retention policy has to be managed.
resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    IngestionMode: 'LogAnalytics'
    // The connection string is resolved from Key Vault by each workload's
    // managed identity; the ingestion key path is never used interactively.
    DisableLocalAuth: false
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output workspaceId string = workspace.id
output workspaceCustomerId string = workspace.properties.customerId
output workspaceName string = workspace.name
output applicationInsightsId string = applicationInsights.id
output applicationInsightsName string = applicationInsights.name
// The connection string is deliberately NOT an output. It carries an
// instrumentation key, and a module output is recorded in the deployment
// history. main.bicep reads it through an `existing` reference and writes it
// straight into Key Vault, so the value only ever exists as a runtime template
// expression.
