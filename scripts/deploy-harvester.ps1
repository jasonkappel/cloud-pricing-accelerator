#Requires -Version 7.3
<#
.SYNOPSIS
    Installs or updates the price harvester on the harvester VM created by infra/main.bicep.

.DESCRIPTION
    Packages src/harvester (code, requirements, coverage matrix, rate extract spec, the approved baseline
    extract, and the VM service files), embeds it in a
    script, and runs that script as root on the VM through Azure Run Command. The VM has no public IP and
    no inbound access, so Run Command is the only install path. The VM must be stopped or deallocated; the script
    closes any unfinished harvest request, starts the VM, and deallocates it afterwards unless -LeaveRunning is set. Rerunning is safe.

.EXAMPLE
    ./scripts/deploy-harvester.ps1 -DeploymentName cloud-pricing
.EXAMPLE
    ./scripts/deploy-harvester.ps1 -AzureRegion westus2 -AwsRegion us-west-2
    Other regions also need src/harvester/rate-extract-spec.json scoped to them; the script refuses a mismatch.
.EXAMPLE
    ./scripts/deploy-harvester.ps1 -PackageOnly
#>
param(
    [string]$DeploymentName = 'cloud-pricing',
    [ValidatePattern('^[a-z0-9]{2,40}$')]
    [string]$AzureRegion = 'eastus2',
    [ValidatePattern('^[a-z]{2}(-[a-z]+)+-[0-9]$')]
    [string]$AwsRegion = 'us-east-1',
    [switch]$LeaveRunning,
    [switch]$PackageOnly
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$repo = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repo 'src/harvester'
$out = Join-Path $repo '.deploy/harvester'
$stage = Join-Path $out 'package'
$archive = Join-Path $out 'harvester.tgz'

# Each harvest derives the rate extract with this spec; a region mismatch would fail every run.
$specScope = (Get-Content (Join-Path $source 'rate-extract-spec.json') -Raw | ConvertFrom-Json).scope
if ($specScope.azureRegion -ne $AzureRegion -or $specScope.awsRegion -ne $AwsRegion) {
    throw "rate-extract-spec.json is scoped to $($specScope.azureRegion)/$($specScope.awsRegion), not $AzureRegion/$AwsRegion. Update the spec for the new regions first."
}
$runScript = Join-Path $out 'install-run-command.sh'

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $stage 'harvester'), (Join-Path $stage 'vm') | Out-Null

function Copy-Lf([string]$from, [string]$to) {
    # The VM runs these files with bash and Python; Windows checkouts may carry CRLF.
    $text = [IO.File]::ReadAllText($from) -replace "`r`n", "`n"
    [IO.File]::WriteAllText($to, $text, [Text.UTF8Encoding]::new($false))
}

Write-Host 'Packaging harvester...'
$files = @(Get-ChildItem $source -File -Filter '*.py') +
    @(Get-Item (Join-Path $source 'requirements.txt'), (Join-Path $source 'coverage-matrix.json'),
        (Join-Path $source 'rate-extract-spec.json'))
foreach ($file in $files) {
    Copy-Lf $file.FullName (Join-Path $stage "harvester/$($file.Name)")
}
# The approved extract each harvest is diffed against.
Copy-Lf (Join-Path $repo 'samples/pricebook_seed_v1.json') (Join-Path $stage 'harvester/baseline-rate-extract.json')
foreach ($file in Get-ChildItem (Join-Path $source 'vm') -File) {
    Copy-Lf $file.FullName (Join-Path $stage "vm/$($file.Name)")
}
tar -czf $archive -C $stage harvester vm

$bytes = [IO.File]::ReadAllBytes($archive)
$sha = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$encoded = [Convert]::ToBase64String($bytes, [Base64FormattingOptions]::InsertLineBreaks) -replace "`r`n", "`n"
if ($encoded.Length -gt 200KB) {
    throw "The harvester package is $($encoded.Length) bytes encoded; Run Command accepts about 256 KB."
}
if ($PackageOnly) {
    Write-Host "Package written to $archive ($($bytes.Length) bytes, sha256 $sha)"
    return
}

$outputs = az deployment sub show --name $DeploymentName --query properties.outputs -o json | ConvertFrom-Json
$resourceGroup = $outputs.resourceGroupName.value
$vmName = $outputs.harvesterVmName.value
$accountUrl = $outputs.pricebookBlobEndpoint.value
$clientId = $outputs.harvesterIdentityClientId.value
$approvalKeyId = $outputs.approvalKeyId.value
if (-not $vmName -or -not $accountUrl -or -not $clientId -or -not $approvalKeyId) {
    throw "Deployment '$DeploymentName' has no harvester outputs. Deploy infra/main.bicep first."
}

$script = @"
#!/usr/bin/env bash
set -euo pipefail
work=`$(mktemp -d)
trap 'rm -rf "`$work"' EXIT
base64 -d > "`$work/harvester.tgz" <<'PACKAGE'
$encoded
PACKAGE
echo "$sha  `$work/harvester.tgz" | sha256sum -c --quiet -
tar -xzf "`$work/harvester.tgz" -C "`$work"
bash "`$work/vm/install.sh" '$accountUrl' '$clientId' '$AzureRegion' '$AwsRegion' '$approvalKeyId'
"@ -replace "`r`n", "`n"
[IO.File]::WriteAllText($runScript, $script, [Text.UTF8Encoding]::new($false))

$power = az vm get-instance-view --resource-group $resourceGroup --name $vmName `
    --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" -o tsv
if ($power -eq 'PowerState/running' -or $power -eq 'PowerState/starting') {
    throw ("$vmName is $power; a harvest or maintenance session may be in progress. Azure also starts a " +
        "newly created VM. When nothing is running on it: az vm deallocate --resource-group $resourceGroup " +
        "--name $vmName, then rerun.")
}

# Close any request a cancelled schedule run left open, so this maintenance start cannot harvest.
$vmId = az vm show --resource-group $resourceGroup --name $vmName --query id -o tsv
$openResult = (az tag list --resource-id $vmId -o json | ConvertFrom-Json).properties.tags.'harvest-result'
if ($openResult -eq 'pending') {
    Write-Host 'Closing an unfinished harvest request before starting the VM...'
    az tag update --resource-id $vmId --operation Merge --tags 'harvest-result=failed maintenance-start' `
        --only-show-errors | Out-Null
}

$started = $false
try {
    Write-Host "Starting $vmName ($power)..."
    $started = $true
    az vm start --resource-group $resourceGroup --name $vmName --only-show-errors | Out-Null

    Write-Host "Installing on $vmName (sha256 $sha)..."
    $result = az vm run-command invoke --resource-group $resourceGroup --name $vmName `
        --command-id RunShellScript --scripts "@$runScript" --only-show-errors -o json | ConvertFrom-Json
    $message = ($result.value | ForEach-Object { $_.message }) -join "`n"
    Write-Host $message
    if ($message -notmatch 'HARVESTER_INSTALL_OK') {
        throw "The harvester install did not finish. Read the output above."
    }
    Write-Host 'Harvester installed. The monthly schedule starts the VM; a manual start does not harvest.'
}
finally {
    if ($started -and -not $LeaveRunning) {
        Write-Host "Deallocating $vmName..."
        az vm deallocate --resource-group $resourceGroup --name $vmName --only-show-errors | Out-Null
    }
    elseif ($started) {
        Write-Host "$vmName left running; deallocate it when done to stop compute charges."
    }
}
