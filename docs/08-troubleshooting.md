# Troubleshooting

If the agent can't reach the server at all, start with the setup page, `https://<app>.azurewebsites.net/setup`. It shows whether the server is ready and why it refused any connections. Then run `./scripts/check-install.ps1 -AppId <client id> -ResourceGroup <group> -WebAppName <app>`, which checks the app registration, secret expiry, redirect URL, consent and credential, and changes nothing.

If the agent can reach it, use `doctor`. Ask the agent to run it, or call it from any MCP client. It checks configuration, tokens per service, live calls and the catalog, and finishes with advice. Most of the issues below show up there with a fix attached.

## Adding the server in Copilot Studio

**The wizard cannot reach the server / "could not connect".** Open `https://<app>.azurewebsites.net/health` in a browser. If it does not answer, the App Service is not running: see what it's doing with `az webapp log tail -g <group> -n <app>`. On a source deployment, also check the deployment log and the startup command (`python run.py`). If `/health` works but `/ready` says degraded, read the `configuration_problems` list.

**No tools listed, or fewer tools than expected.** Copilot Studio drops tools whose input schema it cannot handle. All 39 tools use flat primitive parameters on purpose; if you changed a tool signature, keep to string, integer, number and boolean with defaults. The CI test `test_tool_schemas_are_copilot_studio_safe` guards this.

**Sign-in loop or AADSTS50011 (reply URL mismatch).** The redirect URL from the wizard is not on the app registration. Run `./scripts/setup-entra.ps1 -AppId <client id> -AddRedirectUri "<url>"` and sign in again.

**AADSTS65001 or "need admin approval" at sign-in.** Either you are on the full tier (or added an admin-consent scope) and nobody has consented, or your tenant doesn't let users consent to apps at all, which is common in larger organisations. A Global Administrator or Privileged Role Administrator uses the admin consent link on the setup page, or runs the script with `-GrantAdminConsent`. `check-install.ps1` says which applies.

**AADSTS90009 "Application is requesting a token for itself" at sign-in.** The scopes box contains `api://<client id>/.default`. Because the app is both client and resource, Entra insists on the GUID form: `<client id>/.default`. Delete the tool (and the custom connector it created) and run the wizard again with the right value.

**AADSTS500011 / invalid resource / wrong audience.** The scopes box in Copilot Studio must be exactly `<client id>/.default offline_access`. Delete the connection and the tool, and add again with the right value.

**The connection works, then about an hour later "No tools available" or "The connection for this tool is no longer valid"; the connection status says "Failed to refresh access token for service: oauth2pkce ... Missing refresh token".** The scope was entered without `offline_access`, so Entra never issued a refresh token. Fix it once on the connector: Power Apps > Custom connectors > your MCP connector > Edit > Security > Edit, set Scope to `<client id>/.default offline_access`, paste the client secret again (the form insists), Update connector. Then open the tool in Copilot Studio and sign in to the connection again so it gets a refresh token.

**Dynamic discovery or Dynamic OAuth types fail.** Entra does not support dynamic client registration. Use Manual.

**"A custom connector with display name '...' already exists" when you re-run the wizard.** Deleting the connector in Power Apps (or through the API Hub) is not enough: the wizard also wrote a `connectors` record in the environment's Dataverse. Delete that row too (Power Apps, Tables, Connector; or `invoke service=dataverse DELETE /connectors(<id>)`), or give the server a different name in the wizard.

**The connection sign-in opens a popup that never completes, or the browser blocks it.** The consent flow is: Copilot Studio asks the environment's connectivity API for a consent link, that link goes to Entra, Entra returns to `global.consent.azure-apim.net`, and the API Hub shows a "Confirmation required" page (tick the box, Allow access) before it hands the connection back. If a popup blocker eats step one, open the agent's Tools page, pick the tool, and use the connection drop-down to sign in again from a normal tab. Once the connection shows Connected you do not need the popup again.

**"The connection for this tool is no longer valid (it may have been deleted or its authorization revoked)" and "No tools available".** The connection the tool was saved with has been deleted, usually by someone tidying up connections or by creating a fresh one while testing. The server is fine; the call never reaches it (the error detail shows `ConnectionInvalid`, `notFound`, `mcpMethod: initialize`). Open the tool, pick a working connection in the Connection drop-down, or choose Create new connection. If the drop-down answers "We couldn't enable this connection", the agent's connection reference in Dataverse is still pinned to the deleted connection: find it in the solution (Connection references, name starting with your agent's schema name and the connector id) and set its connection to the working one, or update `connectionid` on the `connectionreferences` row. I hit this five days after go-live; repointing the reference brought all the tools back, including the new ones, without re-adding anything.

## Tool calls

**Every tool fails for everyone; `doctor` shows `server_credential.ok: false` or `AADSTS7000215: Invalid client secret provided`.** The server's own credential is refused, so no on-behalf-of exchange works, whatever roles the admin holds. `whoami` will show no roles for the same reason. AADSTS7000215 means `CLIENT_SECRET` is not the Value of a current secret on the app registration: usually the Secret ID was pasted instead of the Value, or the secret was deleted after a rotation. AADSTS7000222 means it expired. The lasting fix is to stop using a secret on the server: `./scripts/use-managed-identity.ps1 -ResourceGroup <rg> -WebAppName <app> -AppId <client id>`, then open `/ready` and check `server_credential.ok`. If you want to keep the secret, create a new one and paste its Value (shown once, next to the Secret ID) into `CLIENT_SECRET`. I hit this on day five; the switch to the managed identity took two minutes and nothing in Copilot Studio had to change.

**`whoami` or `my_access` shows no Entra roles, or roles shown as `Entra role <guid>`.** Roles come from `/me/transitiveMemberOf`. On the core tier the Graph token only has `User.Read`, and Graph then returns each directory role with its name blanked but its role template id intact. The server names built-in roles from that id (the list ships in `src/megamcp/data/entra_role_templates.json`). A custom role keeps its id. Before 1.2.2 the server ignored nameless roles, so on the core tier every admin looked like they had none. PIM-eligible roles only appear once activated.

**`AADSTS70021` / `AADSTS700213`: no matching federated identity record.** `USE_MANAGED_IDENTITY_FIC=true` but the app registration has no federated credential for this web app's identity. Recreating or cloning the web app gives it a new identity. Run `use-managed-identity.ps1` again; it adds the credential for the current identity.

**`ConnectorTimeoutError: The connector ... has timed out after 120 seconds`.** Copilot Studio's hard limit per tool call. The server keeps calls inside `TOOL_TIME_BUDGET_SECONDS` (default 100): it stops honouring long `Retry-After` waits and returns `retry_after_seconds` instead, and fan-out tools return partial results with a note. If you still see it, the prompt made the orchestrator loop one tool over many resources; ask for the subscription-wide form (`finops_tokenomics` with `subscription_id` and no `resource_id`) or narrow the request. On a Free App Service plan the first call after idle can also take a minute to warm up; use B1 or above.

**`retry_after_seconds` in a response.** The downstream service is rate limiting and waiting would have overrun the tool's time budget. Ask again after that many seconds. Cost Management (query processing units) and the Retail Prices API are the usual sources.

**The agent loops a per-environment tool, or says it needs a tenant-wide tool.** It is not using the inventory. Ask in tenant terms ("across the tenant", "in all environments") or name the tool: `pp_agents_tenant` for agents, `pp_inventory` for apps, flows and environments. If Copilot Studio still shows the old tool list, open the MCP tool in the agent and refresh it.

**`inventory_query_failed`.** Both inventory routes refused. The response lists what each said. Usually the admin lacks an inventory role (Power Platform Administrator, Global Reader, or AI Administrator for agents only), or Conditional Access wants MFA for Azure Resource Manager. `doctor` includes an inventory check that shows how many agents and environments are visible.

**A KQL error from `resource_graph_query`.** The message carries the parser's line, position and token. Cast property values (`tostring(properties.displayName)`, `todatetime(properties.createdAt)`), and remember `count` is a reserved word: name a summary column something else.

**`Unknown job` from `job_status`.** Jobs are kept in memory for 24 hours on the instance that ran them. A restart or a second instance will not know the id; start the job again.

**A long job reports "On-behalf-of exchange failed" for part of its work, or names missing at the end.** Before 1.3.0 a background job kept the sign-in token of the request that started it, and that token expires about an hour after Copilot Studio obtained it. My first twelve-month credits scan lost 19% of its breakdown that way. Since 1.3.0 every request from the same admin (each `job_status` poll included) hands the server a fresh token, jobs always use the newest one, and a job that hits an expired token waits up to four minutes for the next poll and carries on. If you still see it, the agent stopped polling: call `job_status`, then run the tool again with `refresh=true`. Incomplete credit scans are not cached.

**Background job lists many `not_a_member` failures.** You are not a user in those environments, so Dataverse refuses. Add yourself in the admin centre, accept the gap, or use the inventory tools, which do not need environment membership.

**401 from a downstream service after the token was refreshed.** The app registration lacks a permission for that API, or the admin lacks the role. `describe_operation` shows the Graph permission. For the Power Platform API the error includes `InsufficientDelegatedPermissions` and the namespace; add the matching `Licensing.*`, `EnvironmentManagement.*` or `CopilotStudio.*` scope and re-consent.

**403 "S2SUnauthorized / Invalid permission" on Graph reports.** `Reports.Read.All` is missing (full tier) or the admin has no reporting role (Reports Reader, Global Reader, AI Administrator).

**Dataverse 401 "user is not a member of the organization".** The admin is not a user in that environment. Add them in the Power Platform admin centre or use the Power Platform API tools instead.

**"Dataverse calls need dataverse_org_url".** Pass the environment URL, or find it with `pp_environments`.

**Azure 404 InvalidApiVersionParameter.** The server retries automatically with a supported version; if you still see it, pass `api_version` explicitly from `describe_operation`.

**Cost Management 429 with a QPU header.** You are being rate limited on query processing units. The server backs off as instructed while that fits the time budget; otherwise it returns `retry_after_seconds`. Spread cost queries out, use smaller date ranges, and prefer the one-call tools (subscription-wide tokenomics, billing plans) over per-resource loops.

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
