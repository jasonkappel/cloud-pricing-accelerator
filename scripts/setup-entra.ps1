#Requires -Version 7.3
<#
.SYNOPSIS
    Creates the two Entra app registrations that turn on sign-in for the Cloud Pricing Accelerator.

.DESCRIPTION
    Run once per environment by someone who can create app registrations and grant tenant-wide admin
    consent (for example Application Administrator or Cloud Application Administrator). Safe to re-run:
    existing registrations, scopes, roles, and credentials are reused.

    Creates:
      <prefix>-api  Exposes the user_impersonation scope and the app roles Estimator, SnapshotApprover,
                    and SkuMapReviewer. Assignment is required, so only assigned users or groups can
                    sign in.
      <prefix>-web  The sign-in client for the web app. It has no client secret: a federated credential
                    trusts the web app's user-assigned managed identity instead.

    Reads the web host name and the managed identity from the outputs of the infra deployment, so run
    infra/main.bicep first. Afterwards, redeploy the infra with the three IDs this script prints.

.EXAMPLE
    ./scripts/setup-entra.ps1 -DeploymentName cloud-pricing -EstimatorGroupId <group-object-id>
#>
param(
    [string]$DeploymentName = 'cloud-pricing',
    [string]$Prefix = 'cloud-pricing',
    [string]$WebHostName,
    [string]$WebIdentityPrincipalId,
    [string]$EstimatorGroupId,
    [string]$SnapshotApproverGroupId,
    [string]$SkuMapReviewerGroupId
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$graph = 'https://graph.microsoft.com/v1.0'
$graphAppId = '00000003-0000-0000-c000-000000000000'
$graphDelegated = @{
    openid         = '37f7f235-527c-4136-accd-4a02d197296e'
    profile        = '14dad69e-099b-42c9-810b-d002981feec1'
    email          = '64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0'
    offline_access = '7427e0e9-2fba-42fe-b0c0-848c9e6a8182'
    'User.Read'    = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'
}
$roleDefinitions = @(
    @{ value = 'Estimator'; displayName = 'Estimator'; description = 'Upload intakes, answer questions, and export estimates.' }
    @{ value = 'SnapshotApprover'; displayName = 'Snapshot approver'; description = 'Approve a harvested price book snapshot for publication.' }
    @{ value = 'SkuMapReviewer'; displayName = 'SKU map reviewer'; description = 'Approve SKU map versions.' }
)

function Invoke-Graph([string]$Method, [string]$Uri, $Body) {
    $arguments = @('rest', '--method', $Method, '--uri', $Uri, '--headers', 'Content-Type=application/json')
    $bodyFile = $null
    if ($null -ne $Body) {
        $bodyFile = New-TemporaryFile
        $Body | ConvertTo-Json -Depth 20 | Set-Content -Path $bodyFile -Encoding utf8NoBOM
        $arguments += @('--body', "@$bodyFile")
    }
    try {
        $result = az @arguments
        if ($result) { return ($result | Out-String | ConvertFrom-Json) }
    }
    finally {
        if ($bodyFile) { Remove-Item $bodyFile -Force }
    }
}

function Get-OrCreateApplication([string]$DisplayName) {
    $filter = [uri]::EscapeDataString("displayName eq '$DisplayName'")
    $found = (Invoke-Graph GET "$graph/applications?`$filter=$filter").value
    if ($found.Count -gt 1) { throw "More than one app registration is named '$DisplayName'. Remove the extras first." }
    if ($found.Count -eq 1) { return $found[0] }
    Write-Host "Creating app registration $DisplayName"
    return Invoke-Graph POST "$graph/applications" @{ displayName = $DisplayName; signInAudience = 'AzureADMyOrg' }
}

function Get-OrCreateServicePrincipal([string]$AppId) {
    $filter = [uri]::EscapeDataString("appId eq '$AppId'")
    $found = (Invoke-Graph GET "$graph/servicePrincipals?`$filter=$filter").value
    if ($found.Count -eq 1) { return $found[0] }
    return Invoke-Graph POST "$graph/servicePrincipals" @{ appId = $AppId }
}

if (-not $WebHostName -or -not $WebIdentityPrincipalId) {
    $outputs = az deployment sub show --name $DeploymentName --query properties.outputs | ConvertFrom-Json
    if (-not $WebHostName) { $WebHostName = ([uri]$outputs.webUrl.value).Host }
    if (-not $WebIdentityPrincipalId) { $WebIdentityPrincipalId = $outputs.webIdentityPrincipalId.value }
}
if (-not $WebHostName -or -not $WebIdentityPrincipalId) {
    throw 'The web host name and web managed identity are required. Deploy infra/main.bicep first.'
}
$tenantId = az account show --query tenantId -o tsv

# API registration: scope, app roles, v2 access tokens, assignment required.
$api = Get-OrCreateApplication "$Prefix-api"
$scope = $api.api.oauth2PermissionScopes | Where-Object value -eq 'user_impersonation' | Select-Object -First 1
$scopeId = if ($scope) { $scope.id } else { [guid]::NewGuid().Guid }
$existingRoles = @($api.appRoles)
$appRoles = foreach ($role in $roleDefinitions) {
    $existing = $existingRoles | Where-Object value -eq $role.value | Select-Object -First 1
    @{
        id                 = if ($existing) { $existing.id } else { [guid]::NewGuid().Guid }
        value              = $role.value
        displayName        = $role.displayName
        description        = $role.description
        allowedMemberTypes = @('User')
        isEnabled          = $true
    }
}
Invoke-Graph PATCH "$graph/applications/$($api.id)" @{
    identifierUris = @("api://$($api.appId)")
    appRoles       = @($appRoles)
    api            = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes      = @(@{
                id                      = $scopeId
                value                   = 'user_impersonation'
                type                    = 'User'
                isEnabled               = $true
                adminConsentDisplayName = 'Access the Cloud Pricing Accelerator API'
                adminConsentDescription = 'Lets the Cloud Pricing Accelerator web app call its API as the signed-in user.'
                userConsentDisplayName  = 'Access the Cloud Pricing Accelerator API'
                userConsentDescription  = 'Lets the Cloud Pricing Accelerator web app call its API as you.'
            })
    }
} | Out-Null
$apiSp = Get-OrCreateServicePrincipal $api.appId
Invoke-Graph PATCH "$graph/servicePrincipals/$($apiSp.id)" @{ appRoleAssignmentRequired = $true } | Out-Null

# Web registration: redirect URI, delegated permissions, no secret, managed-identity federated credential.
$web = Get-OrCreateApplication "$Prefix-web"
Invoke-Graph PATCH "$graph/applications/$($web.id)" @{
    web                    = @{
        redirectUris          = @("https://$WebHostName/.auth/login/aad/callback")
        logoutUrl             = "https://$WebHostName/.auth/logout"
        implicitGrantSettings = @{ enableIdTokenIssuance = $true; enableAccessTokenIssuance = $false }
    }
    api                    = @{ requestedAccessTokenVersion = 2 }
    requiredResourceAccess = @(
        @{
            resourceAppId  = $graphAppId
            resourceAccess = @($graphDelegated.Values | ForEach-Object { @{ id = $_; type = 'Scope' } })
        }
        @{
            resourceAppId  = $api.appId
            resourceAccess = @(@{ id = $scopeId; type = 'Scope' })
        }
    )
} | Out-Null
$credentials = (Invoke-Graph GET "$graph/applications/$($web.id)/federatedIdentityCredentials").value
$credential = @{
    name      = 'web-app-managed-identity'
    issuer    = "https://login.microsoftonline.com/$tenantId/v2.0"
    subject   = $WebIdentityPrincipalId
    audiences = @('api://AzureADTokenExchange')
    description = 'The web app signs users in with its user-assigned managed identity instead of a client secret.'
}
$current = $credentials | Where-Object name -eq $credential.name | Select-Object -First 1
if ($current) {
    Invoke-Graph PATCH "$graph/applications/$($web.id)/federatedIdentityCredentials/$($current.id)" $credential | Out-Null
}
else {
    Invoke-Graph POST "$graph/applications/$($web.id)/federatedIdentityCredentials" $credential | Out-Null
}
$webSp = Get-OrCreateServicePrincipal $web.appId

# Pre-authorize the web client for the API scope, then grant tenant-wide consent for the web client.
Invoke-Graph PATCH "$graph/applications/$($api.id)" @{
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes      = @((Invoke-Graph GET "$graph/applications/$($api.id)").api.oauth2PermissionScopes)
        preAuthorizedApplications   = @(@{ appId = $web.appId; delegatedPermissionIds = @($scopeId) })
    }
} | Out-Null
# Tenant-wide consent needs an admin. Without it, each user consents to the low-risk sign-in permissions
# (openid, profile, email, offline_access, User.Read) at first sign-in if the tenant allows user consent.
$consented = $false
for ($attempt = 1; $attempt -le 6; $attempt++) {
    $consentOutput = & {
        $PSNativeCommandUseErrorActionPreference = $false
        az ad app permission admin-consent --id $web.appId 2>&1
    }
    if ($LASTEXITCODE -eq 0) { $consented = $true; break }
    if ("$consentOutput" -match 'Authorization_RequestDenied') { break }
    if ($attempt -eq 6) { throw "Admin consent failed: $consentOutput" }
    Start-Sleep -Seconds 10
}
if (-not $consented) {
    Write-Warning ("Tenant-wide consent needs an administrator. Users consent at first sign-in, or an " +
        "admin runs: az ad app permission admin-consent --id $($web.appId)")
}

# Optional group assignments. Group assignment needs Microsoft Entra ID P1 or higher.
$assignments = @{
    Estimator        = $EstimatorGroupId
    SnapshotApprover = $SnapshotApproverGroupId
    SkuMapReviewer   = $SkuMapReviewerGroupId
}
$roleIds = @{}
foreach ($role in $appRoles) { $roleIds[$role.value] = $role.id }
$assigned = (Invoke-Graph GET "$graph/servicePrincipals/$($apiSp.id)/appRoleAssignedTo").value
foreach ($entry in $assignments.GetEnumerator()) {
    if (-not $entry.Value) { continue }
    $exists = $assigned | Where-Object { $_.principalId -eq $entry.Value -and $_.appRoleId -eq $roleIds[$entry.Key] }
    if ($exists) { continue }
    Write-Host "Assigning $($entry.Key) to group $($entry.Value)"
    Invoke-Graph POST "$graph/servicePrincipals/$($apiSp.id)/appRoleAssignedTo" @{
        principalId = $entry.Value
        resourceId  = $apiSp.id
        appRoleId   = $roleIds[$entry.Key]
    } | Out-Null
}

Write-Host ''
Write-Host 'Sign-in is registered. Redeploy the infrastructure with these values to turn it on:'
Write-Host "  authTenantId = $tenantId"
Write-Host "  apiClientId  = $($api.appId)"
Write-Host "  webClientId  = $($web.appId)"
[pscustomobject]@{ authTenantId = $tenantId; apiClientId = $api.appId; webClientId = $web.appId }
