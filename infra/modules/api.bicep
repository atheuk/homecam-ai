param name string
param location string
param tags object
param environmentId string
param environmentDomain string
param managedIdentityId string
param acrId string
param keyVaultName string
param mediaName string
param webName string
param containerImage string
param isPlaceholder bool
param appInsightsConnectionString string
var effectiveImage = containerImage
var effectivePort = isPlaceholder ? 80 : 8000
resource acr 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = { name: last(split(acrId, '/')) }
resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedIdentityId}': {} } }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: effectivePort
        transport: 'auto'
        allowInsecure: false
      }
      registries: isPlaceholder ? [] : [{ server: acr.properties.loginServer, identity: managedIdentityId }]
      secrets: isPlaceholder ? [] : [
        {
          name: 'database-url'
          #disable-next-line no-hardcoded-env-urls
          keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/database-url'
          identity: managedIdentityId
        }
        {
          name: 'redis-url'
          #disable-next-line no-hardcoded-env-urls
          keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/redis-url'
          identity: managedIdentityId
        }
        {
          name: 'secret-key'
          #disable-next-line no-hardcoded-env-urls
          keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/secret-key'
          identity: managedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: effectiveImage
          resources: { cpu: isPlaceholder ? '0.25' : '0.5', memory: isPlaceholder ? '0.5Gi' : '1Gi' }
          probes: isPlaceholder ? [] : [
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
          ]
          env: concat(
            [
              { name: 'APP_ENV', value: 'production' }
              { name: 'AI_PROVIDER', value: 'mock' }
              { name: 'DAHUA_ENABLED', value: 'false' }
              { name: 'EUFY_ENABLED', value: 'false' }
              { name: 'MEDIAMTX_URL', value: 'http://${mediaName}:8889' }
              { name: 'CORS_ORIGINS', value: 'https://${webName}.${environmentDomain}' }
              { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
            ],
            isPlaceholder ? [] : [
              { name: 'DATABASE_URL', secretRef: 'database-url' }
              { name: 'REDIS_URL', secretRef: 'redis-url' }
              { name: 'SECRET_KEY', secretRef: 'secret-key' }
            ]
          )
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
}
output id string = app.id
output fqdn string = app.properties.configuration.ingress.fqdn
