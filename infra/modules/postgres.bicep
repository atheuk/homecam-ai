param name string
param location string
param tags object
param administratorLogin string
@secure()
param administratorLoginPassword string
param appDbName string
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorLoginPassword
    storage: { storageSizeGB: 32 }
    authConfig: { activeDirectoryAuth: 'Disabled', passwordAuth: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}
resource firewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = {
  parent: postgres
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: { value: 'uuid-ossp,pgcrypto,pg_trgm,vector', source: 'user-override' }
}
resource appDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: postgres
  name: appDbName
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}
output fqdn string = postgres.properties.fullyQualifiedDomainName
