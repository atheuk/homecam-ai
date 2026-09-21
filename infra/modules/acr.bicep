param name string
param location string
param tags object
resource acr 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}
output id string = acr.id
output loginServer string = acr.properties.loginServer
