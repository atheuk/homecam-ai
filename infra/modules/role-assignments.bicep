param keyVaultName string
param acrName string
param deployerObjectId string
param managedIdentityPrincipalId string
resource keyVault 'Microsoft.KeyVault/vaults@2026-05-15' existing = { name: keyVaultName }
resource acr 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = { name: acrName }
resource deployerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, deployerObjectId, 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    principalId: deployerObjectId
    principalType: 'User'
  }
}
resource appSecretsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, managedIdentityPrincipalId, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, managedIdentityPrincipalId, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}
