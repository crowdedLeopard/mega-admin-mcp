<#
.SYNOPSIS
  Make the App Service's managed identity the app registration's credential, so the server needs no client secret.

.DESCRIPTION
  The server needs a credential of its own for the on-behalf-of exchange. A client secret works until someone pastes
  the Secret ID instead of the Value, deletes it, or lets it expire, and then every tool fails for every user.
  This script removes that failure mode:

    1. turns on the web app's system-assigned managed identity (if it is off)
    2. adds a federated credential to the app registration that trusts that identity
    3. sets USE_MANAGED_IDENTITY_FIC=true on the web app (the app restarts)

  It never reads, prints or deletes a secret. CLIENT_SECRET stays in place but is no longer used; delete it and the
  secret on the app registration when you are happy. Re-running is safe.

  You need: Azure CLI signed in, Contributor (or Website Contributor) on the web app, and owner of the app
  registration or Application Administrator.

.EXAMPLE
  ./scripts/use-managed-identity.ps1 -ResourceGroup rg-megamcp -WebAppName megamcp-contoso -AppId <client id>
#>
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $WebAppName,
    [Parameter(Mandatory)] [string] $AppId
)
$ErrorActionPreference = "Stop"

function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $out
}

$tenantId = Invoke-Az account show --query tenantId -o tsv
Write-Host "Tenant           $tenantId"

$principalId = Invoke-Az webapp identity show -g $ResourceGroup -n $WebAppName --query principalId -o tsv 2>$null
if (-not $principalId) {
    Write-Host "Turning on the web app's system-assigned identity..."
    $principalId = Invoke-Az webapp identity assign -g $ResourceGroup -n $WebAppName --query principalId -o tsv
}
Write-Host "Managed identity $principalId"

$existing = Invoke-Az ad app federated-credential list --id $AppId -o json | ConvertFrom-Json
if ($existing | Where-Object { $_.subject -eq $principalId }) {
    Write-Host "Federated credential for this identity already exists."
} else {
    $name = ("$WebAppName-managed-identity" -replace '[^A-Za-z0-9-]', '-')
    if ($name.Length -gt 120) { $name = $name.Substring(0, 120) }
    $fic = @{
        name        = $name
        issuer      = "https://login.microsoftonline.com/$tenantId/v2.0"
        subject     = $principalId
        audiences   = @("api://AzureADTokenExchange")
        description = "App Service $WebAppName system-assigned identity acts as this app's credential (USE_MANAGED_IDENTITY_FIC=true)."
    }
    $file = New-TemporaryFile
    $fic | ConvertTo-Json | Set-Content -Path $file -Encoding utf8
    try {
        Invoke-Az ad app federated-credential create --id $AppId --parameters "@$file" -o none
    } finally {
        Remove-Item $file -Force
    }
    Write-Host "Added federated credential '$name'."
}

Invoke-Az webapp config appsettings set -g $ResourceGroup -n $WebAppName --settings USE_MANAGED_IDENTITY_FIC=true -o none 2>$null
Write-Host "Set USE_MANAGED_IDENTITY_FIC=true. The app restarts now."

$hostName = Invoke-Az webapp show -g $ResourceGroup -n $WebAppName --query defaultHostName -o tsv
Write-Host ""
Write-Host "Give it a minute, then open https://$hostName/ready"
Write-Host "server_credential.ok should be true and credential should say 'managed identity federated credential'."
Write-Host "To go back: set USE_MANAGED_IDENTITY_FIC=false (CLIENT_SECRET is still there)."
