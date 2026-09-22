param name string
param location string
param tags object
param environmentId string
param managedIdentityId string
param acrId string
param keyVaultName string
param containerImage string
param isPlaceholder bool
resource acr 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = { name: last(split(acrId, '/')) }
resource job 'Microsoft.App/jobs@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedIdentityId}': {} } }
  properties: {
    environmentId: environmentId
    configuration: {
      triggerType: 'Manual'
      replicaRetryLimit: 1
      replicaTimeout: 1800
      registries: isPlaceholder ? [] : [{ server: acr.properties.loginServer, identity: managedIdentityId }]
      secrets: isPlaceholder ? [] : [
        #disable-next-line no-hardcoded-env-urls
        { name: 'database-url', keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/database-url', identity: managedIdentityId }
      ]
    }
    template: {
      containers: [
        {
          name: 'db-migrate'
          image: containerImage
          command: isPlaceholder ? [] : ['alembic']
          args: isPlaceholder ? [] : ['upgrade', 'head']
          resources: { cpu: '0.25', memory: '0.5Gi' }
          env: isPlaceholder ? [] : [{ name: 'DATABASE_URL', secretRef: 'database-url' }]
        }
      ]
    }
  }
}
