param name string
param location string
param tags object
param workspaceId string
resource workspace 'Microsoft.OperationalInsights/workspaces@2026-03-01' existing = { name: last(split(workspaceId, '/')) }
resource environment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
  }
}
output id string = environment.id
output defaultDomain string = environment.properties.defaultDomain
