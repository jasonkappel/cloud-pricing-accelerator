param keyVaultName string
param deployerObjectId string
@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param deployerPrincipalType string

var kvOfficer = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource deployerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, deployerObjectId, kvOfficer)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvOfficer)
    principalId: deployerObjectId
    principalType: deployerPrincipalType
  }
}
