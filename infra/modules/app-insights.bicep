param name string
param location string
param tags object
param workspaceId string
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: name
  location: location
  kind: 'web'
  tags: tags
  properties: { Application_Type: 'web', WorkspaceResourceId: workspaceId }
}
output connectionString string = appInsights.properties.ConnectionString
