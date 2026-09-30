param serverName string
param databaseName string
param location string
param tags object
param principalId string
param principalName string
@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param principalType string

resource sqlServer 'Microsoft.Sql/servers@2025-01-01' = {
  name: serverName
  location: location
  tags: tags
  properties: {
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: principalType
      login: principalName
      sid: principalId
      tenantId: subscription().tenantId
      azureADOnlyAuthentication: true
    }
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
  }
}

resource database 'Microsoft.Sql/servers/databases@2025-01-01' = {
  parent: sqlServer
  name: databaseName
  location: location
  sku: {
    name: 'Basic'
    tier: 'Basic'
    capacity: 5
  }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
  }
}

output serverId string = sqlServer.id
output serverName string = sqlServer.name
output databaseId string = database.id
output fqdn string = sqlServer.properties.fullyQualifiedDomainName
