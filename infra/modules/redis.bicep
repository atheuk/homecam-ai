// Azure Cache for Redis (classic Microsoft.Cache/redis) is retired for new
// deployments in this subscription/region ("Azure Cache for Redis is
// retiring, create Azure Managed Redis instance instead"). Rather than take
// on the new Azure Managed Redis (redisEnterprise) SKU/API surface for a
// single-user home project, Redis runs as its own Container App inside the
// existing Container Apps environment — internal-only TCP ingress, no auth,
// matching the local docker-compose redis:7-alpine setup exactly.
param name string
param location string
param tags object
param environmentId string
param managedIdentityId string

resource redis 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedIdentityId}': {} } }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      ingress: {
        external: false
        targetPort: 6379
        transport: 'tcp'
        exposedPort: 6379
      }
    }
    template: {
      containers: [
        {
          name: 'redis'
          image: 'redis:7-alpine'
          resources: { cpu: '0.25', memory: '0.5Gi' }
          env: []
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

output hostName string = redis.properties.configuration.ingress.fqdn
output port int = 6379
