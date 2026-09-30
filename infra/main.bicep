targetScope = 'subscription'

@minLength(1)
param location string
@minLength(3)
@maxLength(12)
@description('Short lowercase name (letters, digits, hyphens) used in every resource name, for example pricing-dev.')
param environmentName string = 'pricing-dev'
@description('Resource group to create or reuse.')
param resourceGroupName string = 'rg-cloud-pricing-${environmentName}'
@minLength(1)
@description('Name of the person or pipeline deploying; recorded as a tag.')
param deployedBy string
param createdAt string = utcNow()
@minLength(36)
@description('Entra object ID of the deploying user or service principal (for example: az ad signed-in-user show --query id -o tsv).')
param deployerObjectId string
@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param deployerPrincipalType string = 'User'
@allowed([
  false
])
param deployFunction bool = false
@description('Expose both public-price providers in the review surfaces.')
param presentAwsPricing bool = false
@allowed([
  'published-blob'
  'demo-extract'
])
@description('Where the API gets prices. published-blob: the current Published harvest, verified on load; pricing is unavailable until one is Published. demo-extract: the bundled demo rates.')
param pricebookSource string = 'published-blob'
@minValue(1)
@maxValue(366)
@description('Days after its pricedAsOf date that a Published PriceBook may still price. Harvests run monthly.')
param pricebookMaxAgeDays int = 45
@description('VNet address space (/24 or larger). Change it if it overlaps a network you will peer with.')
param vnetAddressPrefix string = '10.60.0.0/24'
@minLength(1)
@description('SSH public key for the harvester VM (Linux requires one). No inbound path exists, so the private key is never used; administration is Azure Run Command.')
param harvesterAdminSshPublicKey string
@description('Harvester VM size: Trusted Launch (Gen2) capable with SCSI disks; first boot finds the data disk at SCSI LUN 0, so NVMe-only sizes do not work. It runs a few hours a month and is deallocated otherwise.')
param harvesterVmSize string = 'Standard_D4s_v3'
@minValue(1)
@maxValue(28)
@description('Day of the month (UTC) the scheduled harvest runs.')
param harvestMonthDay int = 2
@minValue(0)
@maxValue(23)
@description('Hour (UTC) the scheduled harvest starts.')
param harvestHourUtc int = 6
@description('Optional email address alerted when a scheduled harvest fails. Empty: the alert still fires in Azure Monitor but notifies no one.')
param alertEmail string = ''
@description('Entra tenant ID for sign-in. Leave the three auth values empty on the first deploy; the API refuses every /api call until they are set.')
param authTenantId string = ''
@description('Application (client) ID of the cloud-pricing-api app registration (from scripts/setup-entra.ps1).')
param apiClientId string = ''
@description('Application (client) ID of the cloud-pricing-web app registration (from scripts/setup-entra.ps1).')
param webClientId string = ''

var authConfigured = !empty(authTenantId) && !empty(apiClientId) && !empty(webClientId)
var entraIssuer = '${environment().authentication.loginEndpoint}${authTenantId}/v2.0'

var token = take(toLower(uniqueString(subscription().id, environmentName, location)), 6)
var suffix = '${environmentName}-${token}'
var compactSuffix = replace(suffix, '-', '')
var webAppName = 'app-web-${suffix}'
var tags = {
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module logAnalytics './modules/log-analytics.bicep' = {
  name: 'log-analytics'
  scope: rg
  params: {
    name: 'log-${suffix}'
    location: location
    tags: tags
  }
}

module appInsights './modules/app-insights.bicep' = {
  name: 'app-insights'
  scope: rg
  params: {
    name: 'appi-${suffix}'
    location: location
    tags: tags
    workspaceId: logAnalytics.outputs.workspaceId
  }
}

module network './modules/network.bicep' = {
  name: 'network'
  scope: rg
  params: {
    namePrefix: suffix
    location: location
    tags: tags
    addressPrefix: vnetAddressPrefix
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: rg
  params: {
    name: 'kv-${suffix}'
    location: location
    tags: tags
    harvesterPrincipalId: harvesterIdentity.outputs.principalId
  }
}

module keyVaultEndpoint './modules/private-endpoint.bicep' = {
  name: 'key-vault-endpoint'
  scope: rg
  params: {
    name: 'pe-kv-${suffix}'
    location: location
    tags: tags
    subnetId: network.outputs.endpointSubnetId
    targetResourceId: keyVault.outputs.id
    groupId: 'vault'
    privateDnsZoneId: network.outputs.vaultZoneId
  }
}

module harvesterIdentity './modules/user-assigned-identity.bicep' = {
  name: 'harvester-identity'
  scope: rg
  params: {
    name: 'id-harvester-${suffix}'
    location: location
    tags: tags
  }
}

module pricebookStorage './modules/pricebook-storage.bicep' = {
  name: 'pricebook-storage'
  scope: rg
  params: {
    name: take('stpb${compactSuffix}', 24)
    location: location
    tags: tags
    harvesterPrincipalId: harvesterIdentity.outputs.principalId
  }
}

module pricebookEndpoint './modules/private-endpoint.bicep' = {
  name: 'pricebook-endpoint'
  scope: rg
  params: {
    name: 'pe-stpb-${suffix}'
    location: location
    tags: tags
    subnetId: network.outputs.endpointSubnetId
    targetResourceId: pricebookStorage.outputs.id
    groupId: 'blob'
    privateDnsZoneId: network.outputs.blobZoneId
  }
}

module harvester './modules/harvester-vm.bicep' = {
  name: 'harvester-vm'
  scope: rg
  params: {
    name: 'vm-harvest-${token}'
    location: location
    tags: union(tags, { workload: 'price-harvester' })
    subnetId: network.outputs.harvesterSubnetId
    identityId: harvesterIdentity.outputs.id
    vmSize: harvesterVmSize
    adminSshPublicKey: harvesterAdminSshPublicKey
  }
}

module harvestSchedule './modules/harvest-schedule.bicep' = {
  name: 'harvest-schedule'
  scope: rg
  params: {
    name: 'logic-harvest-${suffix}'
    location: location
    tags: tags
    vmName: harvester.outputs.name
    harvesterPrincipalId: harvesterIdentity.outputs.principalId
    workspaceId: logAnalytics.outputs.workspaceId
    monthDay: harvestMonthDay
    hourUtc: harvestHourUtc
    alertEmail: alertEmail
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    name: take('st${compactSuffix}', 24)
    location: location
    tags: tags
  }
}

module sql './modules/sql.bicep' = {
  name: 'sql'
  scope: rg
  params: {
    serverName: 'sql-${suffix}'
    databaseName: 'sqldb-${suffix}'
    location: location
    tags: tags
    principalId: deployerObjectId
    principalName: deployedBy
    principalType: deployerPrincipalType
  }
}

module plan './modules/app-service-plan.bicep' = {
  name: 'app-service-plan'
  scope: rg
  params: {
    name: 'asp-${suffix}'
    location: location
    tags: tags
  }
}

// Used only by the web app, as the federated credential that replaces a client secret for sign-in.
module webIdentity './modules/user-assigned-identity.bicep' = {
  name: 'web-identity'
  scope: rg
  params: {
    name: 'id-web-${suffix}'
    location: location
    tags: tags
  }
}

module api './modules/app-service.bicep' = {
  name: 'api-app'
  scope: rg
  params: {
    appServiceName: 'app-api-${suffix}'
    appServicePlanId: plan.outputs.id
    location: location
    tags: tags
    linuxFxVersion: 'PYTHON|3.12'
    healthCheckPath: '/healthz'
    // Publishing assembles a large artifact server-side, which can outlast gunicorn's 30-second default.
    startupCommand: 'gunicorn -w 1 -k uvicorn.workers.UvicornWorker --timeout 600 main:app --bind 0.0.0.0:8000'
    virtualNetworkSubnetId: network.outputs.appSubnetId
    // Only the web app (through its VNet integration and the subnet's Microsoft.Web service endpoint)
    // can reach the API. Direct internet calls get 403 before authentication runs.
    ipSecurityRestrictions: [
      {
        name: 'web-app-subnet'
        action: 'Allow'
        priority: 100
        vnetSubnetResourceId: network.outputs.appSubnetId
      }
    ]
    appSettings: [
      {
        name: 'PORT'
        value: '8000'
      }
      {
        name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
        value: appInsights.outputs.connectionString
      }
      {
        name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
        value: 'true'
      }
      {
        name: 'ENABLE_ORYX_BUILD'
        value: 'true'
      }
      {
        name: 'ORYX_DISABLE_COMPRESSION'
        value: 'true'
      }
      {
        name: 'WEBSITES_CONTAINER_START_TIME_LIMIT'
        value: '1800'
      }
      {
        name: 'PRESENT_AWS_PRICING'
        value: string(presentAwsPricing)
      }
      {
        name: 'AUTH_MODE'
        value: 'appservice'
      }
      {
        name: 'WEBSITE_AUTH_AAD_ALLOWED_TENANTS'
        value: authTenantId
      }
      {
        name: 'PRICEBOOK_APPROVAL_MODE'
        value: 'azure'
      }
      {
        name: 'PRICEBOOK_BLOB_ENDPOINT'
        value: pricebookStorage.outputs.blobEndpoint
      }
      {
        name: 'PRICEBOOK_STAGING_CONTAINER'
        value: pricebookStorage.outputs.stagingContainer
      }
      {
        name: 'PRICEBOOK_CONTROL_CONTAINER'
        value: pricebookStorage.outputs.controlContainer
      }
      {
        name: 'PRICEBOOK_PUBLISHED_CONTAINER'
        value: pricebookStorage.outputs.publishedContainer
      }
      {
        name: 'APPROVAL_SIGNING_KEY_ID'
        value: keyVault.outputs.approvalKeyId
      }
      {
        name: 'PRICEBOOK_SOURCE'
        value: pricebookSource
      }
      {
        name: 'PRICEBOOK_MAX_AGE_DAYS'
        value: string(pricebookMaxAgeDays)
      }
    ]
    // Validates the bearer token the web app forwards: tenant (issuer), audience, and calling app.
    authSettings: {
      platform: {
        enabled: authConfigured
      }
      globalValidation: {
        requireAuthentication: true
        unauthenticatedClientAction: 'Return401'
        excludedPaths: [
          '/healthz'
          '/readyz'
        ]
      }
      httpSettings: {
        requireHttps: true
      }
      identityProviders: !authConfigured ? {} : {
        azureActiveDirectory: {
          enabled: true
          registration: {
            clientId: apiClientId
            openIdIssuer: entraIssuer
          }
          validation: {
            allowedAudiences: [
              'api://${apiClientId}'
              apiClientId
            ]
            defaultAuthorizationPolicy: {
              allowedApplications: [
                webClientId
              ]
            }
          }
        }
      }
      login: {
        tokenStore: {
          enabled: false
        }
      }
    }
  }
}

// After both the API and the price book account exist, so neither module depends on the other.
module apiApprovalAccess './modules/api-approval-access.bicep' = {
  name: 'api-approval-access'
  scope: rg
  params: {
    apiPrincipalId: api.outputs.principalId
    keyVaultName: keyVault.outputs.name
    approvalKeyName: keyVault.outputs.approvalKeyName
    pricebookStorageName: pricebookStorage.outputs.name
    stagingContainer: pricebookStorage.outputs.stagingContainer
    controlContainer: pricebookStorage.outputs.controlContainer
    publishedContainer: pricebookStorage.outputs.publishedContainer
  }
}

module web './modules/app-service.bicep' = {
  name: 'web-app'
  scope: rg
  params: {
    appServiceName: webAppName
    appServicePlanId: plan.outputs.id
    location: location
    tags: tags
    linuxFxVersion: 'NODE|24-lts'
    healthCheckPath: ''
    startupCommand: 'node server.mjs'
    userAssignedIdentityId: webIdentity.outputs.id
    virtualNetworkSubnetId: network.outputs.appSubnetId
    appSettings: [
      {
        name: 'PORT'
        value: '3000'
      }
      {
        name: 'API_UPSTREAM_URL'
        value: 'https://${api.outputs.defaultHostName}'
      }
      {
        // Sign-in uses this managed identity's client ID as a federated credential instead of a secret.
        name: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
        value: webIdentity.outputs.clientId
      }
      {
        name: 'WEBSITE_AUTH_AAD_ALLOWED_TENANTS'
        value: authTenantId
      }
      {
        name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
        value: appInsights.outputs.connectionString
      }
      {
        name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
        value: 'false'
      }
      {
        name: 'ENABLE_ORYX_BUILD'
        value: 'false'
      }
      {
        name: 'ORYX_DISABLE_COMPRESSION'
        value: 'true'
      }
      {
        name: 'WEBSITES_CONTAINER_START_TIME_LIMIT'
        value: '1800'
      }
      {
        name: 'NPM_CONFIG_PRODUCTION'
        value: 'false'
      }
    ]
    // Signs users in, keeps their API access token in the token store, and refreshes it.
    authSettings: {
      platform: {
        enabled: authConfigured
      }
      globalValidation: {
        requireAuthentication: true
        unauthenticatedClientAction: 'RedirectToLoginPage'
        redirectToProvider: 'azureactivedirectory'
      }
      httpSettings: {
        requireHttps: true
      }
      identityProviders: !authConfigured ? {} : {
        azureActiveDirectory: {
          enabled: true
          registration: {
            clientId: webClientId
            clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
            openIdIssuer: entraIssuer
          }
          login: {
            loginParameters: [
              'scope=openid profile email offline_access api://${apiClientId}/user_impersonation'
            ]
          }
          validation: {
            allowedAudiences: [
              webClientId
              'api://${webClientId}'
            ]
          }
        }
      }
      login: {
        tokenStore: {
          enabled: true
        }
      }
    }
  }
}

module functionApp './modules/function-app.bicep' = if (deployFunction) {
  name: 'function-app'
  scope: rg
  params: {
    appServiceName: 'func-${suffix}'
    location: location
    tags: tags
    storageAccountName: storage.outputs.name
    storageBlobEndpoint: storage.outputs.primaryBlobEndpoint
    deploymentContainerName: storage.outputs.deploymentContainerName
    appInsightsConnectionString: appInsights.outputs.connectionString
  }
}

module roles './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: rg
  params: {
    keyVaultName: keyVault.outputs.name
    deployerObjectId: deployerObjectId
    deployerPrincipalType: deployerPrincipalType
  }
}

output resourceGroupName string = rg.name
output apiAppName string = 'app-api-${suffix}'
output webAppName string = webAppName
output webUrl string = 'https://${web.outputs.defaultHostName}'
output apiUrl string = 'https://${api.outputs.defaultHostName}'
output webIdentityPrincipalId string = webIdentity.outputs.principalId
output authConfigured bool = authConfigured
output harvesterVmName string = harvester.outputs.name
output harvestScheduleName string = harvestSchedule.outputs.name
output harvesterIdentityClientId string = harvesterIdentity.outputs.clientId
output pricebookStorageAccount string = pricebookStorage.outputs.name
output pricebookBlobEndpoint string = pricebookStorage.outputs.blobEndpoint
output approvalKeyId string = keyVault.outputs.approvalKeyId
output harvesterEgressIp string = network.outputs.natPublicIp
