param name string
param location string
param tags object
@description('Principal ID of the harvester VM identity; it reads the approval key\'s public half to verify approvals.')
param harvesterPrincipalId string

var keyVaultReader = '21090545-7ca7-4776-b22c-e363652d74d2'

resource kv 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
    // Trusted Microsoft services only: ARM creates the approval key through this path. Every data-plane
    // operation still needs an RBAC role on the vault or key.
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

// Signs SkuMap review and snapshot approval records. The private key never leaves the vault.
// ARM creates the key only if it does not exist, so redeploying keeps the same pinned version. Its creation
// parameters must be identical on every deployment, so it carries no tags (the deployment tags include
// the deployment time).
resource approvalKey 'Microsoft.KeyVault/vaults/keys@2024-11-01' = {
  parent: kv
  name: 'approval-signing'
  properties: {
    kty: 'RSA'
    keySize: 3072
    keyOps: [
      'sign'
      'verify'
    ]
    attributes: {
      enabled: true
      exportable: false
    }
  }
}

// Key Vault Reader on this key only: key metadata and the public half, no crypto operations.
resource harvesterKeyReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(approvalKey.id, harvesterPrincipalId, keyVaultReader)
  scope: approvalKey
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultReader)
    principalId: harvesterPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output id string = kv.id
output name string = kv.name
output approvalKeyName string = approvalKey.name
output approvalKeyId string = approvalKey.properties.keyUriWithVersion
