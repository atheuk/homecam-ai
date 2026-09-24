@description('Azure AI Foundry (AI Services) account powering person recognition.')
param name string
param location string
param tags object

@description('Deployment name for the vision chat model used to caption person photos.')
param visionDeploymentName string = 'homecam-vision'

@description('Vision chat model. gpt-4o-mini is deliberately NOT used: in northeurope it is only offered as GlobalProvisionedManaged, which requires pre-purchased capacity.')
param visionModelName string = 'gpt-5.4-mini'
param visionModelVersion string = '2026-03-17'
param visionCapacity int = 50

// One AIServices account provides both capabilities this feature needs:
//  * /computervision/retrieval:vectorizeImage - multimodal image embeddings,
//    which are what make "the same person returned" detectable at all.
//  * /openai/deployments/{name}/chat/completions - a plain-language caption
//    so a small person crop is understandable at a glance.
// A custom subdomain is required for both data-plane paths.
resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
  }
}

resource visionDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundry
  name: visionDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: visionCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: visionModelName
      version: visionModelVersion
    }
  }
}

output id string = foundry.id
output name string = foundry.name
output endpoint string = foundry.properties.endpoint
output visionDeploymentName string = visionDeployment.name
