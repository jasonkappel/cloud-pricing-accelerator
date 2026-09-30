// Price book account: harvester staging, published snapshots, and the publication pointer.
// Separate from the evidence account, whose container-level WORM policy cannot coexist with versioning.
param name string
param location string
param tags object
@description('Principal ID of the harvester VM identity; it may write only to the staging container.')
param harvesterPrincipalId string

var blobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    allowCrossTenantReplication: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
  }
}

// Versioning and soft delete are for recovery; they are not WORM retention.
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
  }
}

resource staging 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'staged-runs'
  properties: {
    publicAccess: 'None'
  }
}

resource published 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'published-pricebooks'
  properties: {
    publicAccess: 'None'
  }
}

resource control 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'publication-control'
  properties: {
    publicAccess: 'None'
  }
}

// The harvester can stage, never publish or move the pointer.
resource stagingWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(staging.id, harvesterPrincipalId, blobDataContributor)
  scope: staging
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributor)
    principalId: harvesterPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// It reads the current pointer and Published artifact to validate each refresh against them.
resource publishedReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(published.id, harvesterPrincipalId, blobDataReader)
  scope: published
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataReader)
    principalId: harvesterPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource controlReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(control.id, harvesterPrincipalId, blobDataReader)
  scope: control
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataReader)
    principalId: harvesterPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output id string = storage.id
output name string = storage.name
output blobEndpoint string = storage.properties.primaryEndpoints.blob
output stagingContainer string = staging.name
output publishedContainer string = published.name
output controlContainer string = control.name
