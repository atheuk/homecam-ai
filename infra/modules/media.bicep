param name string
param location string
param tags object
param environmentId string
param managedIdentityId string
param containerImage string
resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedIdentityId}': {} } }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      ingress: { external: true, targetPort: 8889, transport: 'auto', allowInsecure: false }
    }
    template: {
      containers: [
        {
          name: 'media'
          image: containerImage
          resources: { cpu: '0.5', memory: '1Gi' }
          env: []
          // Container Apps HTTP ingress cannot expose MediaMTX RTSP 8554; RTSP remains LAN/VPN-only.
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}
