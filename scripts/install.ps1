<#
.SYNOPSIS
  Install or update Mega Admin MCP in one command: app registration, hosting, credential, health check.

.DESCRIPTION
  Everything it does is safe to repeat. If it stops halfway, fix what it says and run it again. To update to
  a newer version, run `git pull` and then run it again with the same parameters.

    1. Checks what usually goes wrong first: sign-in, cloud, subscription, the Microsoft.Web provider.
    2. Creates or updates the Entra app registration (scripts/setup-entra.ps1).
    3. Deploys an App Service running the published container, so there is no build to wait for or to fail.
       If the region has no quota for the plan, it tries the next region.
    4. Makes the web app's managed identity the server's credential, so the server holds no secret.
    5. Waits until /ready says ready, then runs scripts/check-install.ps1.
    6. Prints the setup page, which has every value the Copilot Studio wizard asks for.

  You need: Azure CLI 2.60 or later, Contributor on the subscription or resource group, and the right to create
  app registrations (any user by default, unless your tenant restricts it).

.EXAMPLE
  az login --tenant contoso.onmicrosoft.com
  ./scripts/install.ps1

.EXAMPLE
  ./scripts/install.ps1 -ResourceGroup rg-mega-admin-mcp -Location westeurope -Tier full -GrantAdminConsent
#>
[CmdletBinding()]
param(
    [string] $Name = "Mega Admin MCP",
    [ValidateSet("core", "full")] [string] $Tier = "core",
    [string] $ResourceGroup = "rg-mega-admin-mcp",
    [string] $Location = "uksouth",
    [string[]] $FallbackLocations = @("westeurope", "northeurope", "francecentral", "swedencentral", "eastus2", "centralus"),
    [string] $AppName = "",                # the web app; becomes https://<AppName>.azurewebsites.net
    [string] $AppId = "",                  # use this app registration rather than finding one by -Name
    [string] $SubscriptionId = "",
    [string] $DefaultSubscriptionId = "",  # the default subscription for the cost tools
    [string] $Sku = "B1",
    [string] $Image = "",                  # defaults to this checkout's version of the published container
    [switch] $GrantAdminConsent,
    [switch] $NoSecret,
    [int] $ReadyTimeoutMinutes = 10
)
$ErrorActionPreference = "Stop"

function Step([string] $text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Stop-Install([string] $text) { Write-Host ""; Write-Host $text -ForegroundColor Red; Write-Host "Fix that and run this script again. Anything already done is kept."; exit 1 }

function Invoke-Az {
    $out = & az @args 2>&1
    if ($LASTEXITCODE -ne 0) { throw (($out | Out-String).Trim()) }
    return ($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
}

# ---------------------------------------------------------------------------
Step "1 of 6  Checking this machine and subscription"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { Stop-Install "The Azure CLI isn't installed. Install it from https://aka.ms/installazurecli, or run this script in Azure Cloud Shell." }
$cliVersion = [version]((az version -o json | ConvertFrom-Json).'azure-cli')
if ($cliVersion -lt [version]"2.60.0") { Stop-Install "Azure CLI $cliVersion is too old. Run: az upgrade" }

if ($SubscriptionId) { & az account set --subscription $SubscriptionId 2>$null; if ($LASTEXITCODE -ne 0) { Stop-Install "Can't select subscription $SubscriptionId with this sign-in." } }
$account = az account show -o json 2>$null | ConvertFrom-Json
if (-not $account) { Stop-Install "Not signed in. Run: az login --tenant <your tenant>" }
if ($account.environmentName -ne "AzureCloud") { Stop-Install "This server only knows the public Microsoft cloud, and you're signed in to $($account.environmentName). Sovereign clouds such as GCC aren't supported yet." }
if ($account.state -ne "Enabled") { Stop-Install "Subscription '$($account.name)' is $($account.state). Choose another with -SubscriptionId." }
Write-Host "Tenant        $($account.tenantId)"
Write-Host "Subscription  $($account.name) ($($account.id))"
Write-Host "Signed in as  $($account.user.name)"

$provider = az provider show -n Microsoft.Web --query registrationState -o tsv 2>$null
if ($provider -ne "Registered") {
    Write-Host "Registering the Microsoft.Web resource provider, which can take a few minutes..."
    try { Invoke-Az provider register -n Microsoft.Web --wait | Out-Null }
    catch { Stop-Install "Could not register Microsoft.Web on this subscription. Someone with Contributor on the subscription needs to run: az provider register -n Microsoft.Web`n$_" }
}

# ---------------------------------------------------------------------------
Step "2 of 6  App registration"
$entraArgs = @{ Name = $Name; Tier = $Tier; PassThru = $true }
if ($AppId) { $entraArgs.AppId = $AppId }
if ($GrantAdminConsent) { $entraArgs.GrantAdminConsent = $true }
if ($NoSecret) { $entraArgs.NoSecret = $true }
try { $entra = & (Join-Path $PSScriptRoot "setup-entra.ps1") @entraArgs | Select-Object -Last 1 }
catch { Stop-Install "The app registration step failed. If it says Authorization_RequestDenied, your tenant restricts who can create apps: ask for the Application Developer role.`n$_" }
$AppId = $entra.AppId

# ---------------------------------------------------------------------------
Step "3 of 6  Hosting"
$version = (Select-String -Path (Join-Path $PSScriptRoot "../src/megamcp/__init__.py") -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
if (-not $Image) { $Image = "ghcr.io/crowdedleopard/mega-admin-mcp:$version" }
Write-Host "Image         $Image"

if ((az group exists -n $ResourceGroup) -eq "true") {
    Write-Host "Resource group $ResourceGroup exists; using it."
} else {
    try { Invoke-Az group create -n $ResourceGroup -l $Location -o none | Out-Null }
    catch { Stop-Install "Could not create resource group $ResourceGroup in $Location. You need Contributor on the subscription, or create the group yourself and pass -ResourceGroup.`n$_" }
}

# A stable default, so a second run finds the first run's web app.
if (-not $AppName) {
    $found = az webapp list -g $ResourceGroup --query "[?tags.application=='mega-admin-mcp'].name | [0]" -o tsv 2>$null
    if ($found) {
        $AppName = $found
    } else {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$($account.id)/$ResourceGroup"))[0..4] | ForEach-Object { $_.ToString("x2") })
        $AppName = "megamcp-$hash"
    }
}
$site = az webapp show -g $ResourceGroup -n $AppName -o json 2>$null | ConvertFrom-Json
$regions = @($Location) + $FallbackLocations | Select-Object -Unique
if ($site) {
    Write-Host "Web app $AppName exists in $($site.location); updating it."
    $regions = @($site.location)
} else {
    $check = az rest --method post --url "https://management.azure.com/subscriptions/$($account.id)/providers/Microsoft.Web/checkNameAvailability?api-version=2023-12-01" --body (@{ name = $AppName; type = "Microsoft.Web/sites" } | ConvertTo-Json -Compress).Replace('"', '\"') -o json 2>$null | ConvertFrom-Json
    if ($check -and -not $check.nameAvailable) { Stop-Install "The name $AppName is taken ($($check.message)). Choose another with -AppName." }
}

$paramFile = New-TemporaryFile
$parameters = @{
    '$schema'      = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
    contentVersion = "1.0.0.0"
    parameters     = @{
        appName = @{ value = $AppName }; sku = @{ value = $Sku }; tenantId = @{ value = $entra.TenantId }; clientId = @{ value = $AppId }
        useManagedIdentityFederatedCredential = @{ value = $true }; containerImage = @{ value = $Image }; defaultSubscriptionId = @{ value = $DefaultSubscriptionId }
    }
}
$template = Join-Path $PSScriptRoot "../deploy/azuredeploy.json"
$deployment = $null
try {
    foreach ($region in $regions) {
        $parameters.parameters.location = @{ value = $region }
        $parameters | ConvertTo-Json -Depth 6 | Set-Content -Path $paramFile -Encoding utf8
        Write-Host "Deploying to $region..."
        try {
            $deployment = Invoke-Az deployment group create -g $ResourceGroup -n "mega-admin-mcp" -f $template -p "@$paramFile" -o json | Out-String | ConvertFrom-Json
            break
        } catch {
            $message = "$_"
            if ($message -match "OverQuota|over quota|SkuNotAvailable|not available in|NoRegisteredProviderFound|LocationNotAvailable|Conflict.*capacity|does not support") {
                Write-Host "  $region can't host a $Sku plan on this subscription; trying the next region." -ForegroundColor Yellow
                continue
            }
            Stop-Install "The deployment failed.`n$message"
        }
    }
} finally {
    Remove-Item $paramFile -Force -ErrorAction SilentlyContinue
}
if (-not $deployment) { Stop-Install "None of these regions can host a $Sku plan on this subscription: $($regions -join ', '). Try -Sku P0v3, another -Location, or request quota." }
$baseUrl = $deployment.properties.outputs.url.value
Write-Host "Web app       $baseUrl"

# ---------------------------------------------------------------------------
Step "4 of 6  Server credential"
& (Join-Path $PSScriptRoot "use-managed-identity.ps1") -ResourceGroup $ResourceGroup -WebAppName $AppName -AppId $AppId
if (-not $?) { Stop-Install "Could not set up the managed identity credential. You need to own the app registration or be an Application Administrator." }
& (Join-Path $PSScriptRoot "setup-entra.ps1") -AppId $AppId -AddRedirectUri "$baseUrl/setup" | Out-Null

# ---------------------------------------------------------------------------
Step "5 of 6  Waiting for the server (the first start pulls the container)"
# On an update, App Service keeps serving the old container until the new one is up, so "ready" alone proves nothing.
$expected = if ($Image -match ":$([regex]::Escape($version))$") { $version } else { "" }
$deadline = (Get-Date).AddMinutes($ReadyTimeoutMinutes)
$ready = $null
$done = $false
$lastSaid = ""
while ((Get-Date) -lt $deadline) {
    $ready = $null
    try {
        $ready = Invoke-RestMethod -Uri "$baseUrl/ready" -TimeoutSec 30 -SkipHttpErrorCheck
    } catch { }
    $done = $ready.status -eq "ready" -and (-not $expected -or $ready.version -eq $expected)
    if ($done) { break }
    $said = if (-not $ready) { "Not answering yet..." }
            elseif ($expected -and $ready.version -ne $expected) { "Still running version $($ready.version); waiting for $expected..." }
            else { "Up, not ready yet: $(@($ready.configuration_problems) + @($ready.server_credential.aad_error) -join '; ')" }
    if ($said -ne $lastSaid) { Write-Host $said; $lastSaid = $said }
    Start-Sleep -Seconds 15
}
if (-not $done) {
    Write-Host "The server wasn't ready on the expected version after $ReadyTimeoutMinutes minutes." -ForegroundColor Red
    if ($ready) { $ready | ConvertTo-Json -Depth 5 | Write-Host }
    Write-Host "See what the container is doing:  az webapp log tail -g $ResourceGroup -n $AppName"
    Write-Host "Then run this script again. It carries on from where it stopped."
    exit 1
}
Write-Host "Ready: version $($ready.version), $($ready.catalog_operations) operations, $($ready.server_credential.credential)." -ForegroundColor Green

& (Join-Path $PSScriptRoot "check-install.ps1") -AppId $AppId -ResourceGroup $ResourceGroup -WebAppName $AppName

# ---------------------------------------------------------------------------
Step "6 of 6  Add it to Copilot Studio"
Write-Host "Open this page. It has every value the wizard asks for, with copy buttons, and shows what the server sees as you connect:"
Write-Host ""
Write-Host "  $baseUrl/setup" -ForegroundColor Green
Write-Host ""
if ($entra.Secret) {
    Write-Host "The wizard also needs this client secret. It's only shown here, once, and the server doesn't use it:"
    Write-Host ""
    Write-Host "  $($entra.Secret)" -ForegroundColor Yellow
    Write-Host ""
}
