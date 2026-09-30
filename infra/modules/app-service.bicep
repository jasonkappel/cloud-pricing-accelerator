param appServiceName string
param appServicePlanId string
param location string
param tags object
param linuxFxVersion string
param healthCheckPath string
param startupCommand string
param appSettings array
@description('Optional user-assigned identity resource ID, added next to the system-assigned identity.')
param userAssignedIdentityId string = ''
@description('Optional App Service Authentication (authsettingsV2) properties. Empty leaves authentication unmanaged.')
param authSettings object = {}
@description('Optional VNet integration subnet. When set, all outbound traffic routes through the VNet.')
param virtualNetworkSubnetId string = ''
@description('Optional inbound allow rules for the app (not the SCM deploy site). When set, everything else is denied.')
param ipSecurityRestrictions array = []

resource appService 'Microsoft.Web/sites@2025-03-01' = {
  name: appServiceName
  location: location
  tags: tags
  identity: empty(userAssignedIdentityId)
    ? { type: 'SystemAssigned' }
    : {
        type: 'SystemAssigned, UserAssigned'
        userAssignedIdentities: {
          '${userAssignedIdentityId}': {}
        }
      }
  properties: {
    serverFarmId: appServicePlanId
    httpsOnly: true
    virtualNetworkSubnetId: empty(virtualNetworkSubnetId) ? null : virtualNetworkSubnetId
    siteConfig: union(
      {
        linuxFxVersion: linuxFxVersion
        minTlsVersion: '1.2'
        ftpsState: 'Disabled'
        appCommandLine: startupCommand
        appSettings: appSettings
        vnetRouteAllEnabled: !empty(virtualNetworkSubnetId)
      },
      empty(healthCheckPath)
        ? {}
        : {
            healthCheckPath: healthCheckPath
          },
      empty(ipSecurityRestrictions)
        ? {}
        : {
            ipSecurityRestrictions: ipSecurityRestrictions
            ipSecurityRestrictionsDefaultAction: 'Deny'
            // The deploy (SCM) site keeps its own policy: Entra-only, basic auth disabled below.
            scmIpSecurityRestrictionsUseMain: false
            scmIpSecurityRestrictionsDefaultAction: 'Allow'
          }
    )
  }
}

resource scmAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: appService
  name: 'scm'
  properties: { allow: false }
}

resource ftpAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: appService
  name: 'ftp'
  properties: { allow: false }
}

resource authConfig 'Microsoft.Web/sites/config@2024-11-01' = if (!empty(authSettings)) {
  parent: appService
  name: 'authsettingsV2'
  properties: authSettings
}

output id string = appService.id
output principalId string = appService.identity.principalId
output defaultHostName string = appService.properties.defaultHostName
