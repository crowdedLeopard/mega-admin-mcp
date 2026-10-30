<#
.SYNOPSIS
  Check a Mega Admin MCP install from the outside and say exactly what to fix. Changes nothing.

.DESCRIPTION
  Copilot Studio tends to report a failed connection as a generic error. This checks the things that cause one: the
  app registration, the secret's expiry, the redirect URL, consent, the managed identity credential and the server.
  It reads secret expiry dates, never secret values. Exit code 1 means at least one FAIL.

.EXAMPLE
  ./scripts/check-install.ps1 -AppId <client id> -ResourceGroup rg-mega-admin-mcp -WebAppName megamcp-contoso

.EXAMPLE
  ./scripts/check-install.ps1 -AppId <client id> -Url https://mcp.contoso.com
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $AppId,
    [string] $ResourceGroup = "",
    [string] $WebAppName = "",
    [string] $Url = ""
)
$ErrorActionPreference = "Stop"
$script:Failed = 0

function Result([string] $state, [string] $what, [string] $fix = "") {
    $colour = @{ PASS = "Green"; WARN = "Yellow"; FAIL = "Red" }[$state]
    Write-Host ("[{0}] {1}" -f $state, $what) -ForegroundColor $colour
    if ($fix) { Write-Host "       $fix" }
    if ($state -eq "FAIL") { $script:Failed++ }
}

$token = (az account get-access-token --resource-type ms-graph -o json | ConvertFrom-Json).accessToken
$headers = @{ Authorization = "Bearer $token" }
function Graph([string] $url) { Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/$url" -Headers $headers }

Write-Host ""
Write-Host "App registration" -ForegroundColor Cyan
$app = $null
try { $app = Graph "applications(appId='$AppId')" } catch { Result FAIL "App registration $AppId can't be read." "Check the id and that you're signed in to the right tenant: az login --tenant <tenant>" }
if ($app) {
    if (@($app.identifierUris) -contains "api://$AppId") { Result PASS "API identifier is api://$AppId." } else { Result FAIL "The API identifier isn't api://$AppId." "Run ./scripts/setup-entra.ps1 -AppId $AppId" }
    $scope = @($app.api.oauth2PermissionScopes) | Where-Object { $_.value -eq "access_as_user" -and $_.isEnabled }
    if ($scope) { Result PASS "Scope access_as_user is exposed." } else { Result FAIL "Scope access_as_user is missing or disabled." "Run ./scripts/setup-entra.ps1 -AppId $AppId" }
    if ($app.api.requestedAccessTokenVersion -eq 2) { Result PASS "Requests v2 access tokens." } else { Result WARN "Doesn't request v2 access tokens." "Run ./scripts/setup-entra.ps1 -AppId $AppId" }
    $resources = @($app.requiredResourceAccess)
    if ($resources.Count -ge 3) { Result PASS "Delegated permissions are set on $($resources.Count) Microsoft APIs." } else { Result FAIL "Only $($resources.Count) Microsoft APIs have delegated permissions." "Run ./scripts/setup-entra.ps1 -AppId $AppId" }

    $now = Get-Date
    $secrets = @($app.passwordCredentials)
    $live = @($secrets | Where-Object { ([datetime]$_.endDateTime) -gt $now })
    if ($live.Count -eq 0) {
        $state = if ($secrets.Count) { "FAIL" } else { "WARN" }
        Result $state "The app has no current client secret. The Copilot Studio connector needs one, and connections stop refreshing without it." "Run ./scripts/setup-entra.ps1 -AppId $AppId -NewSecret, then edit the connector's client secret."
    } else {
        $latest = ($live | ForEach-Object { [datetime]$_.endDateTime } | Sort-Object | Select-Object -Last 1)
        $days = [int]($latest - $now).TotalDays
        if ($days -lt 30) { Result WARN "The newest client secret expires in $days days. Connections stop refreshing when it does." "Run ./scripts/setup-entra.ps1 -AppId $AppId -NewSecret, then edit the connector's client secret." }
        else { Result PASS "Client secret is current for another $days days." }
    }

    $redirects = @($app.web.redirectUris)
    if ($redirects | Where-Object { $_ -match "azure-apim\.net/redirect|consent\.azure-apim" }) {
        Result PASS "A Copilot Studio redirect URL is registered."
    } else {
        Result WARN "No Copilot Studio redirect URL yet. Expected until you've pressed Create in the wizard; sign-in then fails with AADSTS50011 until it's added." "Run ./scripts/setup-entra.ps1 -AppId $AppId -AddRedirectUri `"<the redirect URL>`""
    }

    try {
        $sp = Graph "servicePrincipals(appId='$AppId')?`$select=id"
        Result PASS "The enterprise application exists."
        $grants = @((Graph "oauth2PermissionGrants?`$filter=clientId eq '$($sp.id)'").value)
        $tenantWide = @($grants | Where-Object { $_.consentType -eq "AllPrincipals" })
        if ($tenantWide.Count) {
            Result PASS "Admin consent is granted for the whole tenant."
        } else {
            $policy = Graph "policies/authorizationPolicy"
            $assigned = @($policy.defaultUserRolePermissions.permissionGrantPoliciesAssigned)
            if ($assigned | Where-Object { $_ -match "ManagePermissionGrantsForSelf" }) {
                Result PASS "No admin consent yet, but users in this tenant can consent for themselves ($($grants.Count) have)."
            } else {
                Result WARN "Users in this tenant can't consent for themselves and no admin has consented. Sign-in will stop at 'Need admin approval'." "A Global Administrator or Privileged Role Administrator needs to use the admin consent link on the /setup page."
            }
        }
    } catch {
        Result WARN "Couldn't read the enterprise application or its consent: $($_.Exception.Message)" "If it's missing, run ./scripts/setup-entra.ps1 -AppId $AppId"
    }
}

$identity = ""
if ($WebAppName) {
    Write-Host ""
    Write-Host "Hosting" -ForegroundColor Cyan
    $site = az webapp show -g $ResourceGroup -n $WebAppName -o json 2>$null | ConvertFrom-Json
    if (-not $site) {
        Result FAIL "Web app $WebAppName wasn't found in $ResourceGroup."
    } else {
        $Url = if ($Url) { $Url } else { "https://$($site.defaultHostName)" }
        if ($site.state -eq "Running") { Result PASS "Web app is running in $($site.location)." } else { Result FAIL "Web app is $($site.state)." "Run: az webapp start -g $ResourceGroup -n $WebAppName" }
        if ($site.sku -in "Free", "Shared") { Result WARN "The $($site.sku) tier sleeps when idle, and Copilot Studio gives each call 120 seconds, so the first call after a sleep can fail." "Scale up to B1 or above." }
        $identity = $site.identity.principalId
        $settings = az webapp config appsettings list -g $ResourceGroup -n $WebAppName --query "[?name=='USE_MANAGED_IDENTITY_FIC'].value | [0]" -o tsv 2>$null
        if ($settings -eq "true" -or $settings -eq "True") {
            if ($app) {
                $fics = @((Graph "applications/$($app.id)/federatedIdentityCredentials").value)
                if ($fics | Where-Object { $_.subject -eq $identity }) {
                    Result PASS "The managed identity is the server's credential, so the server holds no secret."
                } else {
                    Result FAIL "The server is set to use its managed identity, but the app registration doesn't trust this one." "Run ./scripts/use-managed-identity.ps1 -ResourceGroup $ResourceGroup -WebAppName $WebAppName -AppId $AppId"
                }
            }
        } else {
            Result WARN "The server uses a client secret, which will expire." "Switch to the managed identity: ./scripts/use-managed-identity.ps1 -ResourceGroup $ResourceGroup -WebAppName $WebAppName -AppId $AppId"
        }
    }
}

if ($Url) {
    Write-Host ""
    Write-Host "Server" -ForegroundColor Cyan
    $Url = $Url.TrimEnd("/") -replace "/mcp$", ""
    try {
        $ready = Invoke-RestMethod -Uri "$Url/ready" -TimeoutSec 60 -SkipHttpErrorCheck
        if ($ready.status -eq "ready") {
            Result PASS "/ready says ready: version $($ready.version), $($ready.catalog_operations) operations."
        } else {
            Result FAIL "/ready says $($ready.status)." ((@($ready.configuration_problems) + @($ready.server_credential.fix) | Where-Object { $_ }) -join " ")
        }
        if ($ready.update_available) { Result WARN "Version $($ready.update_available) is available; this server runs $($ready.version)." "Run git pull, then ./scripts/install.ps1 with the same parameters." }
        if ($ready.audit_stored_in -match "^memory") { Result WARN "The audit log is $($ready.audit_stored_in), so it's lost on restart." "Set STATE_DIR to a writable path that survives restarts." }
        if ($ready.connections.last_accepted_utc) { Result PASS "A client last connected at $($ready.connections.last_accepted_utc)." }
        foreach ($r in @($ready.connections.refused)) {
            if ($r.kind -ne "no_token") { Result WARN "Refused a connection $($r.count) times: $($r.message)" $r.fix }
        }
    } catch {
        Result FAIL "$Url/ready didn't answer: $($_.Exception.Message)" "See what the host is doing, for example: az webapp log tail -g $ResourceGroup -n $WebAppName"
    }
    try {
        $probe = Invoke-WebRequest -Uri "$Url/mcp" -Method Post -ContentType "application/json" -Body '{"jsonrpc":"2.0","id":1,"method":"ping"}' -TimeoutSec 30 -SkipHttpErrorCheck
        if ($probe.StatusCode -eq 401) { Result PASS "The MCP endpoint refuses a call with no sign-in." }
        elseif ($probe.StatusCode -eq 503) { Result FAIL "The MCP endpoint says the server isn't configured." ($probe.Content) }
        else { Result FAIL "The MCP endpoint answered $($probe.StatusCode) to a call with no sign-in. It should answer 401." "Check AUTH_MODE on the host." }
    } catch {
        Result FAIL "The MCP endpoint didn't answer: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Copilot Studio can't be checked from here: its data policies, whether the tool uses end user credentials, and the connection itself. The /setup page covers those." -ForegroundColor DarkGray
Write-Host ""
if ($script:Failed) {
    Write-Host "$($script:Failed) check(s) failed." -ForegroundColor Red
    exit 1
}
Write-Host "No failures." -ForegroundColor Green
