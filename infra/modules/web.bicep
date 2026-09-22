param name string
param location string
param tags object
param environmentId string
param managedIdentityId string
param acrId string
param containerImage string
param isPlaceholder bool
param apiFqdn string
resource acr 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = { name: last(split(acrId, '/')) }
resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedIdentityId}': {} } }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      ingress: { external: true, targetPort: isPlaceholder ? 80 : 3000, transport: 'auto', allowInsecure: false }
      registries: isPlaceholder ? [] : [{ server: acr.properties.loginServer, identity: managedIdentityId }]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          resources: { cpu: '0.5', memory: '1Gi' }
          env: [
            { name: 'PORT', value: isPlaceholder ? '80' : '3000' }
            { name: 'HOSTNAME', value: '0.0.0.0' }
            { name: 'NEXT_PUBLIC_API_URL', value: 'https://${apiFqdn}' }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
}
output fqdn string = app.properties.configuration.ingress.fqdn
