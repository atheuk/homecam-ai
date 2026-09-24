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
param tailscaleAuthKeySecretUri string
var effectiveImage = containerImage
var effectivePort = isPlaceholder ? 80 : 8000
var tailscaleImage = 'tailscale/tailscale:v1.102.4'
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
        {
          name: 'tailscale-auth-key'
          keyVaultUrl: tailscaleAuthKeySecretUri
          identity: managedIdentityId
        }
      ]
    }
    template: {
      containers: concat(
        [
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
                // Non-secret default only. Edge base URL/token should be set at
                // runtime via the admin API/UI (DB-backed config), not here, so
                // they can change without redeploying. See docs/edge-connector.md.
                { name: 'DAHUA_MODE', value: 'direct' }
                { name: 'EUFY_ENABLED', value: 'false' }
                { name: 'MEDIAMTX_URL', value: 'http://${mediaName}:8889' }
                { name: 'CORS_ORIGINS', value: 'https://${webName}.${environmentDomain}' }
                { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
              ],
              isPlaceholder ? [] : [
                { name: 'DATABASE_URL', secretRef: 'database-url' }
                { name: 'REDIS_URL', secretRef: 'redis-url' }
                { name: 'SECRET_KEY', secretRef: 'secret-key' }
                { name: 'TAILSCALE_HTTP_PROXY', value: 'http://127.0.0.1:1055' }
              ]
            )
          }
        ],
        isPlaceholder ? [] : [
          {
            name: 'tailscale'
            image: tailscaleImage
            resources: { cpu: '0.25', memory: '0.5Gi' }
            env: [
              { name: 'TS_AUTHKEY', secretRef: 'tailscale-auth-key' }
              { name: 'TS_USERSPACE', value: 'true' }
              { name: 'TS_ACCEPT_DNS', value: 'true' }
              { name: 'TS_OUTBOUND_HTTP_PROXY_LISTEN', value: '127.0.0.1:1055' }
              { name: 'TS_HOSTNAME', value: 'homecam-azure' }
              { name: 'TS_EXTRA_ARGS', value: '--advertise-tags=tag:homecam-azure' }
              { name: 'TS_STATE_DIR', value: '/tmp/tailscale' }
              { name: 'TS_ENABLE_HEALTH_CHECK', value: 'true' }
              { name: 'TS_LOCAL_ADDR_PORT', value: '0.0.0.0:9002' }
            ]
            probes: [
              {
                type: 'Liveness'
                httpGet: { path: '/healthz', port: 9002 }
                initialDelaySeconds: 15
                periodSeconds: 30
              }
              {
                type: 'Readiness'
                httpGet: { path: '/healthz', port: 9002 }
                initialDelaySeconds: 5
                periodSeconds: 10
              }
            ]
          }
        ]
      )
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
}
output id string = app.id
output fqdn string = app.properties.configuration.ingress.fqdn
