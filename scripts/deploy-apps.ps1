#Requires -Version 7.3
<#
.SYNOPSIS
    Packages the API and web app and ZIP-deploys them to the App Services created by infra/main.bicep.

.DESCRIPTION
    The API package carries the approved demo price files from samples/ because the API loads them from a
    samples/ folder beside the app. App Service installs requirements.txt during deployment. The web
    package also carries the harvester's standard-library publish contract (core.py, snapshot.py, and the
    coverage matrix) so the API builds exactly the Published artifact the harvester verifies. The web
    package is a prebuilt Vite bundle served by server.mjs, which has no runtime npm dependencies.

.EXAMPLE
    ./scripts/deploy-apps.ps1 -DeploymentName cloud-pricing
.EXAMPLE
    ./scripts/deploy-apps.ps1 -PackageOnly
#>
param(
    [string]$DeploymentName = 'cloud-pricing',
    [switch]$PackageOnly
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $repo '.deploy'
$apiStage = Join-Path $out 'api'
$webStage = Join-Path $out 'web'
$apiZip = Join-Path $out 'api.zip'
$webZip = Join-Path $out 'web.zip'

$harvesterContract = @('__init__.py', 'core.py', 'snapshot.py', 'coverage-matrix.json')

$priceFiles = @(
    'pricebook_seed_v1.json',
    'pricebook_extract_approval_v1.json',
    'skumap_seed_v1.json',
    'skumap_approval_v1.json'
)

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $apiStage 'samples'), $webStage | Out-Null

Write-Host 'Packaging API...'
Copy-Item (Join-Path $repo 'src/api/main.py'), (Join-Path $repo 'src/api/requirements.txt') $apiStage
Copy-Item (Join-Path $repo 'src/api/app') (Join-Path $apiStage 'app') -Recurse
New-Item -ItemType Directory -Path (Join-Path $apiStage 'harvester') | Out-Null
foreach ($file in $harvesterContract) {
    Copy-Item (Join-Path $repo "src/harvester/$file") (Join-Path $apiStage 'harvester')
}
Get-ChildItem $apiStage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
foreach ($file in $priceFiles) {
    Copy-Item (Join-Path $repo "samples/$file") (Join-Path $apiStage 'samples')
}
Compress-Archive -Path (Join-Path $apiStage '*') -DestinationPath $apiZip

Write-Host 'Building web...'
Push-Location (Join-Path $repo 'src/web')
try {
    npm ci --no-audit --no-fund
    npm run build
}
finally {
    Pop-Location
}
Copy-Item (Join-Path $repo 'src/web/server.mjs'), (Join-Path $repo 'src/web/package.json') $webStage
Copy-Item (Join-Path $repo 'src/web/dist') (Join-Path $webStage 'dist') -Recurse
Compress-Archive -Path (Join-Path $webStage '*') -DestinationPath $webZip

if ($PackageOnly) {
    Write-Host "Packages written to $out"
    return
}

$outputs = az deployment sub show --name $DeploymentName --query properties.outputs -o json | ConvertFrom-Json
$resourceGroup = $outputs.resourceGroupName.value

Write-Host "Deploying API to $($outputs.apiAppName.value)..."
az webapp deploy --resource-group $resourceGroup --name $outputs.apiAppName.value `
    --src-path $apiZip --type zip --only-show-errors | Out-Null

Write-Host "Deploying web to $($outputs.webAppName.value)..."
az webapp deploy --resource-group $resourceGroup --name $outputs.webAppName.value `
    --src-path $webZip --type zip --only-show-errors | Out-Null

Write-Host "Web: $($outputs.webUrl.value)"
Write-Host "The API accepts traffic only from the web app's subnet; opening it directly returns 403."
