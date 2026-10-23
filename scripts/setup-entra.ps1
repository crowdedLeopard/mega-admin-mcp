<#
.SYNOPSIS
  Create (or update) the Entra app registration that Mega Admin MCP needs. Needs only the Azure CLI (az login).

.DESCRIPTION
  One app registration does three jobs:
    1. It is the API that Copilot Studio signs the admin in to (scope api://<client-id>/access_as_user).
    2. It is the OAuth client Copilot Studio uses (client id + secret in the MCP wizard).
    3. It holds the delegated permissions the server exchanges on-behalf-of the admin (Graph, Azure, Power Platform API, Dataverse, Power BI).

  Tiers (scripts/permissions.json):
    core  no admin consent needed, every admin can consent for themselves. Covers Azure FinOps, Copilot Credits, Power Platform admin, Dataverse.
    full  adds Microsoft Graph reporting and directory scopes. A Global Administrator (or Privileged Role Administrator) grants consent once.

.EXAMPLE
  az login --tenant contoso.onmicrosoft.com
  ./scripts/setup-entra.ps1 -Name "Mega Admin MCP" -Tier core

.EXAMPLE
  # after the Copilot Studio wizard shows its redirect URL
  ./scripts/setup-entra.ps1 -AppId <client-id> -AddRedirectUri "https://global.consent.azure-apim.net/redirect/xxxx"

.EXAMPLE
  ./scripts/setup-entra.ps1 -Name "Mega Admin MCP" -Tier full -GrantAdminConsent
#>
[CmdletBinding()]
param(
  [string]$Name = "Mega Admin MCP",
  [ValidateSet("core", "full")] [string]$Tier = "core",
  [string]$AppId = "",
  [string]$AddRedirectUri = "",
  [string[]]$RedirectUris = @(),
  [switch]$GrantAdminConsent,
  [switch]$NoSecret,
  [int]$SecretYears = 1,
  [string]$PermissionsFile = (Join-Path $PSScriptRoot "permissions.json")
)

$ErrorActionPreference = "Stop"

Write-Host "Checking Azure CLI sign-in..." -ForegroundColor Cyan
$account = az account show -o json 2>$null | ConvertFrom-Json
if (-not $account) { throw "Run 'az login --tenant <your tenant>' first." }
$tenantId = $account.tenantId
Write-Host "Tenant: $tenantId  ($($account.user.name))"

# Graph calls go through Invoke-RestMethod with a CLI token (avoids shell quoting problems with $select and & on Windows)
$graphToken = (az account get-access-token --resource-type ms-graph -o json | ConvertFrom-Json).accessToken
$script:GraphHeaders = @{ Authorization = "Bearer $graphToken"; "Content-Type" = "application/json" }

function Invoke-Graph {
  param([string]$Method, [string]$Url, $Body = $null)
  $params = @{ Method = $Method; Uri = $Url; Headers = $script:GraphHeaders }
  if ($null -ne $Body) { $params.Body = ($Body | ConvertTo-Json -Depth 20 -Compress) }
  try {
    return Invoke-RestMethod @params
  } catch {
    $detail = ""
    try { $detail = $_.ErrorDetails.Message } catch {}
    throw "Graph $Method $Url failed: $($_.Exception.Message) $detail"
  }
}

function Get-ServicePrincipalByAppId([string]$appId) {
  try { return Invoke-Graph GET "https://graph.microsoft.com/v1.0/servicePrincipals(appId='$appId')?`$select=id,displayName,oauth2PermissionScopes" } catch { return $null }
}

# ---------------------------------------------------------------------------
# Mode 2: only add a redirect URI to an existing app
# ---------------------------------------------------------------------------
if ($AddRedirectUri) {
  if (-not $AppId) { throw "-AppId is required with -AddRedirectUri" }
  $app = Invoke-Graph GET "https://graph.microsoft.com/v1.0/applications(appId='$AppId')?`$select=id,web"
  $uris = @(@($app.web.redirectUris) + $AddRedirectUri | Where-Object { $_ } | Select-Object -Unique)
  Invoke-Graph PATCH "https://graph.microsoft.com/v1.0/applications/$($app.id)" @{ web = @{ redirectUris = $uris } } | Out-Null
  Write-Host "Redirect URIs now:" -ForegroundColor Green
  $uris | ForEach-Object { Write-Host "  $_" }
  return
}

# ---------------------------------------------------------------------------
# Mode 1: create or update the app registration
# ---------------------------------------------------------------------------
$perms = Get-Content $PermissionsFile -Raw | ConvertFrom-Json

if ($AppId) {
  $app = Invoke-Graph GET "https://graph.microsoft.com/v1.0/applications(appId='$AppId')"
  Write-Host "Updating existing app $($app.displayName) ($AppId)" -ForegroundColor Cyan
} else {
  $safeName = $Name.Replace("'", "''")
  $existing = Invoke-Graph GET "https://graph.microsoft.com/v1.0/applications?`$filter=displayName eq '$safeName'&`$select=id,appId,displayName"
  if ($existing.value.Count -gt 0) {
    $app = $existing.value[0]
    $AppId = $app.appId
    Write-Host "Found existing app '$Name' ($AppId); updating it." -ForegroundColor Yellow
  } else {
    Write-Host "Creating app registration '$Name'..." -ForegroundColor Cyan
    $app = Invoke-Graph POST "https://graph.microsoft.com/v1.0/applications" @{
      displayName    = $Name
      signInAudience = "AzureADMyOrg"
      web            = @{ redirectUris = @($RedirectUris) }
      notes          = "Mega Admin MCP server. Copilot Studio signs admins in here and the server calls Microsoft APIs on their behalf."
    }
    $AppId = $app.appId
    Start-Sleep -Seconds 8
  }
}
$objectId = $app.id

# Expose the API scope and request v2 access tokens
$current = Invoke-Graph GET "https://graph.microsoft.com/v1.0/applications/${objectId}?`$select=api,identifierUris,web"
$existingScope = @($current.api.oauth2PermissionScopes) | Where-Object { $_.value -eq "access_as_user" }
$scopeId = if ($existingScope) { $existingScope.id } else { [guid]::NewGuid().ToString() }
$apiPatch = @{
  identifierUris = @("api://$AppId")
  api            = @{
    requestedAccessTokenVersion = 2
    oauth2PermissionScopes      = @(@{
        id                      = $scopeId
        value                   = "access_as_user"
        type                    = "User"
        isEnabled               = $true
        adminConsentDisplayName = "Access Mega Admin MCP as the signed-in admin"
        adminConsentDescription = "Allows Copilot Studio to call Mega Admin MCP on behalf of the signed-in administrator."
        userConsentDisplayName  = "Access Mega Admin MCP on your behalf"
        userConsentDescription  = "Allows Copilot Studio to call Mega Admin MCP on your behalf."
      })
  }
}
if ($RedirectUris.Count -gt 0) {
  $apiPatch.web = @{ redirectUris = @(@($current.web.redirectUris) + $RedirectUris | Where-Object { $_ } | Select-Object -Unique) }
}
Invoke-Graph PATCH "https://graph.microsoft.com/v1.0/applications/$objectId" $apiPatch | Out-Null
Write-Host "API exposed: api://$AppId/access_as_user" -ForegroundColor Green

# Resolve delegated permission ids from each resource's service principal
$required = @()
$missing = @()
foreach ($prop in $perms.resources.PSObject.Properties) {
  $resName = $prop.Name
  $res = $prop.Value
  $wanted = @($res.core)
  if ($Tier -eq "full") { $wanted += @($res.full) }
  $wanted = @($wanted | Where-Object { $_ } | Select-Object -Unique)
  if ($wanted.Count -eq 0) { continue }
  $sp = Get-ServicePrincipalByAppId $res.appId
  if (-not $sp) {
    Write-Host "Service principal for $resName not present in tenant; creating it..." -ForegroundColor Yellow
    az ad sp create --id $res.appId -o none 2>$null
    Start-Sleep -Seconds 10
    $sp = Get-ServicePrincipalByAppId $res.appId
    if (-not $sp) { Write-Host "  could not create or read the service principal for $resName; skipping" -ForegroundColor Yellow; continue }
  }
  $access = @()
  foreach ($w in $wanted) {
    $s = @($sp.oauth2PermissionScopes) | Where-Object { $_.value -eq $w }
    if ($s) { $access += @{ id = $s.id; type = "Scope" } } else { $missing += "$resName/$w" }
  }
  if ($access.Count -gt 0) { $required += @{ resourceAppId = $res.appId; resourceAccess = $access } }
  Write-Host ("  {0,-30} {1,3} scopes" -f $resName, $access.Count)
}
Invoke-Graph PATCH "https://graph.microsoft.com/v1.0/applications/$objectId" @{ requiredResourceAccess = $required } | Out-Null
if ($missing.Count -gt 0) { Write-Host "Scopes not found in this tenant (skipped): $($missing -join ', ')" -ForegroundColor Yellow }

# Service principal for our own app (needed for consent and enterprise app settings)
if (-not (Get-ServicePrincipalByAppId $AppId)) { az ad sp create --id $AppId -o none; Start-Sleep -Seconds 5 }

# Client secret
$secret = $null
if (-not $NoSecret) {
  $cred = az ad app credential reset --id $AppId --append --display-name "mega-admin-mcp $(Get-Date -Format yyyy-MM-dd)" --years $SecretYears -o json | ConvertFrom-Json
  $secret = $cred.password
}

# Admin consent
if ($GrantAdminConsent) {
  Write-Host "Granting tenant-wide admin consent..." -ForegroundColor Cyan
  az ad app permission admin-consent --id $AppId -o none
  if ($LASTEXITCODE -ne 0) { Write-Host "Admin consent failed. You need Global Administrator or Privileged Role Administrator. Use the consent URL below." -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "==================== Mega Admin MCP app registration ====================" -ForegroundColor Green
Write-Host "TENANT_ID      = $tenantId"
Write-Host "CLIENT_ID      = $AppId"
if ($secret) { Write-Host "CLIENT_SECRET  = $secret   (shown once, store it in Key Vault or the App Service setting)" }
Write-Host "Tier           = $Tier"
Write-Host ""
Write-Host "Copilot Studio > Add tool > Model Context Protocol > OAuth 2.0 > Manual:"
Write-Host "  Client ID          : $AppId"
Write-Host "  Client secret      : (the secret above)"
Write-Host "  Authorization URL  : https://login.microsoftonline.com/$tenantId/oauth2/v2.0/authorize"
Write-Host "  Token URL template : https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token"
Write-Host "  Refresh URL        : https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token"
Write-Host "  Scopes             : api://$AppId/.default"
Write-Host ""
Write-Host "After the wizard shows the redirect URL, run:"
Write-Host "  ./scripts/setup-entra.ps1 -AppId $AppId -AddRedirectUri <redirect url>"
if (-not $GrantAdminConsent -and $Tier -eq "full") {
  Write-Host ""
  Write-Host "Admin consent URL (Global Administrator):"
  Write-Host "  https://login.microsoftonline.com/$tenantId/adminconsent?client_id=$AppId"
}
