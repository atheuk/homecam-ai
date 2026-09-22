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
resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedIdentityId}': {} } }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      registries: isPlaceholder ? [] : [{ server: acr.properties.loginServer, identity: managedIdentityId }]
      secrets: isPlaceholder ? [] : [
        #disable-next-line no-hardcoded-env-urls
        { name: 'database-url', keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/database-url', identity: managedIdentityId }
        #disable-next-line no-hardcoded-env-urls
        { name: 'redis-url', keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/redis-url', identity: managedIdentityId }
        #disable-next-line no-hardcoded-env-urls
        { name: 'secret-key', keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/secret-key', identity: managedIdentityId }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: containerImage
          command: isPlaceholder ? [] : ['python']
          args: isPlaceholder ? [] : ['-m', 'app.worker']
          resources: { cpu: '0.25', memory: '0.5Gi' }
          env: concat(
            [
              { name: 'APP_ENV', value: 'production' }
              { name: 'AI_PROVIDER', value: 'mock' }
              { name: 'DAHUA_ENABLED', value: 'false' }
              { name: 'EUFY_ENABLED', value: 'false' }
            ],
            isPlaceholder ? [] : [
              { name: 'DATABASE_URL', secretRef: 'database-url' }
              { name: 'REDIS_URL', secretRef: 'redis-url' }
              { name: 'SECRET_KEY', secretRef: 'secret-key' }
            ]
          )
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}
