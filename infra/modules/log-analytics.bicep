param name string
param location string
param tags object
resource workspace 'Microsoft.OperationalInsights/workspaces@2026-03-01' = {
  name: name
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}
output id string = workspace.id
output customerId string = workspace.properties.customerId
