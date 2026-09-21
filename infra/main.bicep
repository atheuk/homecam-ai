targetScope = 'subscription'

param environmentName string
param location string
param sessionId string
param deployedBy string
param createdAt string
param deployerObjectId string
param isPlaceholder bool = true
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param apiImage string = ''
param workerImage string = ''
param webImage string = ''
@secure()
param administratorLoginPassword string
@secure()
param secretKey string

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}
var rgName = 'rg-homecam-ai'
var caeName = 'cae-homecam-ai-dev-82ac'
var acrName = 'crhomecamaidev82ac'
var apiName = 'ca-api-homecam-ai-dev-82ac'
var workerName = 'ca-worker-homecam-ai-dev-82ac'
var webName = 'ca-web-homecam-ai-dev-82ac'
var mediaName = 'ca-media-homecam-ai-dev-82ac'
var pgName = 'psql-homecam-ai-dev-82ac'
var redisName = 'redis-homecam-ai-dev-82ac'
var kvName = 'kv-homecam-ai-dev-82ac'
var identityName = 'id-homecam-ai-dev-82ac'
var logName = 'log-homecam-ai-dev-82ac'
var appInsightsName = 'appi-homecam-ai-dev-82ac'
var appDbName = 'homecam'
var postgresAdminLogin = 'homecam'

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: rgName
  location: location
  tags: tags
}

module identity './modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: { name: identityName, location: location, tags: tags }
}
module logAnalytics './modules/log-analytics.bicep' = {
  name: 'log-analytics'
  scope: rg
  params: { name: logName, location: location, tags: tags }
}
module appInsights './modules/app-insights.bicep' = {
  name: 'app-insights'
  scope: rg
  params: { name: appInsightsName, location: location, tags: tags, workspaceId: logAnalytics.outputs.id }
}
module acr './modules/acr.bicep' = {
  name: 'acr'
  scope: rg
  params: { name: acrName, location: location, tags: tags }
}
module postgres './modules/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    name: pgName
    location: location
    tags: tags
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: administratorLoginPassword
    appDbName: appDbName
  }
}
module redis './modules/redis.bicep' = {
  name: 'redis'
  scope: rg
  params: {
    name: redisName
    location: location
    tags: tags
    environmentId: environment.outputs.id
    managedIdentityId: identity.outputs.id
  }
}
module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: rg
  params: {
    name: kvName
    location: location
    tags: tags
    secretKey: secretKey
  }
}
module environment './modules/container-environment.bicep' = {
  name: 'container-environment'
  scope: rg
  params: {
    name: caeName
    location: location
    tags: tags
    workspaceId: logAnalytics.outputs.id
  }
}

module api './modules/api.bicep' = {
  name: 'api'
  scope: rg
  params: {
    name: apiName
    location: location
    tags: tags
    environmentId: environment.outputs.id
    environmentDomain: environment.outputs.defaultDomain
    managedIdentityId: identity.outputs.id
    acrId: acr.outputs.id
    keyVaultName: kvName
    mediaName: mediaName
    webName: webName
    containerImage: empty(apiImage) ? containerImage : apiImage
    isPlaceholder: isPlaceholder
    appInsightsConnectionString: appInsights.outputs.connectionString
  }
}
module worker './modules/worker.bicep' = {
  name: 'worker'
  scope: rg
  params: {
    name: workerName
    location: location
    tags: tags
    environmentId: environment.outputs.id
    managedIdentityId: identity.outputs.id
    acrId: acr.outputs.id
    keyVaultName: kvName
    containerImage: empty(workerImage) ? (empty(apiImage) ? containerImage : apiImage) : workerImage
    isPlaceholder: isPlaceholder
  }
}
module web './modules/web.bicep' = {
  name: 'web'
  scope: rg
  params: {
    name: webName
    location: location
    tags: tags
    environmentId: environment.outputs.id
    managedIdentityId: identity.outputs.id
    acrId: acr.outputs.id
    containerImage: empty(webImage) ? containerImage : webImage
    isPlaceholder: isPlaceholder
    apiFqdn: '${apiName}.${environment.outputs.defaultDomain}'
  }
}
module media './modules/media.bicep' = {
  name: 'media'
  scope: rg
  params: {
    name: mediaName
    location: location
    tags: tags
    environmentId: environment.outputs.id
    managedIdentityId: identity.outputs.id
    containerImage: 'bluenviron/mediamtx:latest'
  }
}
module migration './modules/db-migrate-job.bicep' = {
  name: 'db-migrate-job'
  scope: rg
  params: {
    name: 'job-migrate-homecam-ai-dev-82ac'
    location: location
    tags: tags
    environmentId: environment.outputs.id
    managedIdentityId: identity.outputs.id
    acrId: acr.outputs.id
    keyVaultName: kvName
    containerImage: empty(apiImage) ? containerImage : apiImage
    isPlaceholder: isPlaceholder
  }
}
module roles './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: rg
  params: {
    keyVaultName: kvName
    acrName: acrName
    deployerObjectId: deployerObjectId
    managedIdentityPrincipalId: identity.outputs.principalId
  }
}
