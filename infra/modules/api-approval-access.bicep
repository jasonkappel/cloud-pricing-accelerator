// What the API's system identity needs to record SkuMap reviews, approvals, and publications.
// Read the harvester's staged runs, sign with the approval key, create Published artifacts, and in the
// control container write only records under approvals/ and the current.json pointer. It can never delete.
// Append-only artifacts and the pointer compare-and-swap are enforced by the API's conditional writes.
param apiPrincipalId string
param keyVaultName string
param approvalKeyName string
param pricebookStorageName string
param stagingContainer string
param controlContainer string
param publishedContainer string

var cryptoUser = '12338af0-0e69-4776-bea7-57ae8d297424'
var blobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

var blobActions = 'Microsoft.Storage/storageAccounts/blobServices/containers/blobs'
var approvalWriteCondition = '((!(ActionMatches{\'${blobActions}/write\'}) AND !(ActionMatches{\'${blobActions}/add/action\'})) OR @Resource[${blobActions}:path] StringStartsWith \'approvals/\' OR @Resource[${blobActions}:path] StringEquals \'current.json\')'

// Built-in Blob Data Contributor also carries container create and delete, which a blob path condition cannot
// limit. This role reads and creates blobs only: no container management, no delete, no move, no tags.
resource approvalWriterRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'api-approval-record-writer')
  properties: {
    roleName: 'Price book approval record writer (${resourceGroup().name})'
    description: 'Read and create price book blobs. No deletes or container changes.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          '${blobActions}/read'
          '${blobActions}/write'
          '${blobActions}/add/action'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

resource approvalKey 'Microsoft.KeyVault/vaults/keys@2024-11-01' existing = {
  parent: vault
  name: approvalKeyName
}

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: pricebookStorageName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' existing = {
  parent: account
  name: 'default'
}

resource staging 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = {
  parent: blobService
  name: stagingContainer
}

resource control 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = {
  parent: blobService
  name: controlContainer
}

resource published 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = {
  parent: blobService
  name: publishedContainer
}

resource signer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(approvalKey.id, apiPrincipalId, cryptoUser)
  scope: approvalKey
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cryptoUser)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource stagingReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(staging.id, apiPrincipalId, blobDataReader)
  scope: staging
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataReader)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource approvalWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(control.id, apiPrincipalId, 'api-approval-record-writer')
  scope: control
  properties: {
    roleDefinitionId: approvalWriterRole.id
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    description: 'Write approval records under approvals/ and the current.json pointer only; no deletes.'
    conditionVersion: '2.0'
    condition: approvalWriteCondition
  }
}

// Artifacts are assembled server-side from the staged rows (Put Block From URL), so the API writes here.
resource publishedWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(published.id, apiPrincipalId, 'api-approval-record-writer')
  scope: published
  properties: {
    roleDefinitionId: approvalWriterRole.id
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    description: 'Create Published price book artifacts; no deletes.'
  }
}
