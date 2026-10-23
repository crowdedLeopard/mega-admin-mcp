# Troubleshooting

Start with `doctor`. Ask the agent to run it, or call it from any MCP client. It checks configuration, tokens per service, live calls and the catalog, and finishes with advice. Most of the issues below show up there with a fix attached.

## Adding the server in Copilot Studio

**The wizard cannot reach the server / "could not connect".** Open `https://<app>.azurewebsites.net/health` in a browser. If it does not answer, the App Service is not running: check the deployment log in the portal (Deployment Center) and the startup command (`python run.py`). If `/health` works but `/ready` says degraded, read the `configuration_problems` list.

**No tools listed, or fewer tools than expected.** Copilot Studio drops tools whose input schema it cannot handle. All 32 tools use flat primitive parameters on purpose; if you changed a tool signature, keep to string, integer, number and boolean with defaults. The CI test `test_tool_schemas_are_copilot_studio_safe` guards this.

**Sign-in loop or AADSTS50011 (reply URL mismatch).** The redirect URL from the wizard is not on the app registration. Run `./scripts/setup-entra.ps1 -AppId <client id> -AddRedirectUri "<url>"` and sign in again.

**AADSTS65001 or "need admin approval" at sign-in.** You are on the full tier (or added an admin-consent scope) and nobody has consented. A Global Administrator opens `https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<client id>`, or runs the script with `-GrantAdminConsent`.

**AADSTS500011 / invalid resource / wrong audience.** The scopes box in Copilot Studio must be exactly `api://<client id>/.default`. Delete the connection and the tool, and add again with the right value.

**Dynamic discovery or Dynamic OAuth types fail.** Entra does not support dynamic client registration. Use Manual.

## Tool calls

**401 from a downstream service after the token was refreshed.** The app registration lacks a permission for that API, or the admin lacks the role. `describe_operation` shows the Graph permission. For the Power Platform API the error includes `InsufficientDelegatedPermissions` and the namespace; add the matching `Licensing.*`, `EnvironmentManagement.*` or `CopilotStudio.*` scope and re-consent.

**403 "S2SUnauthorized / Invalid permission" on Graph reports.** `Reports.Read.All` is missing (full tier) or the admin has no reporting role (Reports Reader, Global Reader, AI Administrator).

**Dataverse 401 "user is not a member of the organization".** The admin is not a user in that environment. Add them in the Power Platform admin centre or use the Power Platform API tools instead.

**"Dataverse calls need dataverse_org_url".** Pass the environment URL, or find it with `pp_environments`.

**Azure 404 InvalidApiVersionParameter.** The server retries automatically with a supported version; if you still see it, pass `api_version` explicitly from `describe_operation`.

**Cost Management 429 with a QPU header.** You are being rate limited on query processing units. The server backs off as instructed; spread cost queries out or use smaller date ranges.

**Response truncated.** Use `$select` and `$top`, a shorter date range, or follow `next_link` with `invoke_url`.

**"circuit_open".** The host failed five times in a row; wait 30 seconds. Check the service health of the API in question.

## Catalog

**`catalog_status` shows loaded_from bundled and a failed refresh.** The server cannot reach GitHub releases. Either allow outbound HTTPS to github.com, set `CATALOG_URL` to a mirror you control, or set `CATALOG_AUTO_REFRESH=false` and redeploy to pick up new catalogs.

**A daily build opened a `catalog-warning` issue.** An upstream source failed or shrank. The previous snapshot was kept, so nothing is broken; look at the issue and the warning text in `catalog_meta.json`.

**Search does not find an operation you know exists.** Try the exact path words, filter by service, or use `describe_operation` on the id pattern `graph.v1.0:GET:/path`, `arm:GET:/path`, `powerplatform:GET:/path`. If the API has no public spec, add it to a curated file and send a pull request.

## Hosting

**Deployment fails with `InternalSubscriptionIsOverQuotaForSku` (Current Limit (B1 VMs): 0).** The subscription has no App Service quota for that SKU in that region. This is common on sponsored, internal and trial subscriptions. Pick another region (`-p location=westeurope`) or SKU (`sku=P0v3`, `sku=S1`), or request quota. The App Service plan does not have to be in the resource group's region. Validate first with `az deployment group validate` using the same parameters; it reports the quota problem without creating anything.

**`az webapp deploy` reports HTTP 504 GatewayTimeout after several minutes.** On B1 the first Oryx build (pip install) takes six to eight minutes and Kudu's HTTP gateway gives up before it finishes. The build usually completes anyway: check with `az webapp log deployment show -n <app> -g <rg>` and look for "Deployment successful", then open `/ready`. Use `--async true` with `az webapp deploy` to avoid the timeout, or deploy from GitHub (the template's `deployFromGitHub=true`), which builds asynchronously.

**App Service shows "Application Error" after deployment.** Check Log stream. The usual causes are a missing setting in delegated mode (`TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`) or a build that did not install requirements (`SCM_DO_BUILD_DURING_DEPLOYMENT=true` must be set before you deploy code).

**Slow first call after idle.** Always On is enabled on B1 and above in the template. On Free or Shared tiers the app sleeps; do not use them.

**Container: health check failing.** The container listens on 8000. Pass `PORT` if your platform maps something else.

## Local development

```powershell
pip install -e ".[dev]"
az login
$env:AUTH_MODE = "none"; $env:DEFAULT_SUBSCRIPTION_ID = "<sub id>"
python -m megamcp.app
python scripts/smoke_test.py --url http://127.0.0.1:8000/mcp --live
```

In `none` mode downstream calls use your Azure CLI login. The Azure CLI's own app registration is pre-authorised for Graph, Azure, Power Platform API, Dataverse and Power BI, but not for every scope (Power Platform API admin actions on agents, for example, return 403 locally and work in delegated mode).
