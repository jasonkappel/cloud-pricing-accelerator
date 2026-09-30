param name string
param location string
param tags object
param workspaceId string

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: name
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspaceId
    DisableIpMasking: false
  }
}

output id string = appInsights.id
output connectionString string = appInsights.properties.ConnectionString
