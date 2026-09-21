param name string
param location string
param tags object
@secure()
param secretKey string
resource keyVault 'Microsoft.KeyVault/vaults@2026-05-15' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}
resource secretKeyResource 'Microsoft.KeyVault/vaults/secrets@2026-05-15' = {
  parent: keyVault
  name: 'secret-key'
  properties: { value: secretKey }
}
// database-url and redis-url are seeded by the deploy phase (az keyvault secret set) AFTER
// Postgres/Redis provisioning — never built as connection-string literals in Bicep
// (would trip NO-BICEP-LITERAL-SECRET). See scaffold/references/env-var-secrets.md
// § Key Vault Secret Dependency Chain.
output id string = keyVault.id
