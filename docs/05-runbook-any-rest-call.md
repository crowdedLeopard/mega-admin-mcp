# The runbook: any REST call you have access to

The `finops_*` tools cover the questions I get asked most. Everything else is the runbook: a catalog of every operation Microsoft publishes for its admin APIs, and one tool that executes any of them as you.

## Three tools

**`search_operations`** takes plain words and returns operations from the catalog. The catalog currently holds about 55,000 operations across Microsoft Graph v1.0 and beta, Azure Resource Manager (Cost Management, Cognitive Services, Monitor, Consumption, Billing, Authorization, Quota, Resource Graph, Power Platform accounts, Machine Learning, API Management, App Service, Key Vault, Storage, Compute, Network and more), the Power Platform API, Microsoft Fabric, Dataverse, Power BI admin, the legacy Power Platform admin APIs and the Office 365 Management Activity API. Search understands admin vocabulary: "copilot credits" finds the licensing allocation operations, "openai deployments" finds Cognitive Services deployments, "dlp" finds the governance policies.

**`describe_operation`** shows one operation: parameters, request body, api-version, the Graph permissions it needs (delegated and application), a documentation link, and a ready-to-run `invoke` call.

**`invoke`** executes. You give it a service, a method, a path, and optionally query parameters, a body and headers as JSON strings. The server adds the api-version for Azure and Power Platform calls when you leave it out, gets the right token for the service, applies the safety policy, retries, heals, and shapes the response.

## Services

| service | Base | Token audience | Notes |
| --- | --- | --- | --- |
| graph | https://graph.microsoft.com | Graph | Prefix the path with /v1.0 or /beta; defaults to v1.0 |
| arm | https://management.azure.com | Azure Service Management | api-version auto selected from the catalog, newest stable |
| powerplatform | https://api.powerplatform.com | Power Platform API | api-version defaults to 2024-10-01 |
| dataverse | https://<org>.crm.dynamics.com | the environment | Pass `dataverse_org_url`; paths are relative to /api/data/v9.2 |
| powerbi | https://api.powerbi.com | Power BI | Admin endpoints need Tenant.Read.All |
| fabric | https://api.fabric.microsoft.com | Fabric | Tenant settings, capacities, workspaces |
| bap, powerapps, flow | legacy admin APIs | PowerApps / Flow service | Behind the admin PowerShell cmdlets |
| o365mgmt | https://manage.office.com | Office 365 Management | Application permissions only |
| devops | https://dev.azure.com | Azure DevOps | Optional |
| retailprices | https://prices.azure.com | none | Public price list |

## Examples

Who is in the tenant, cheaply:

```json
{"service": "graph", "method": "GET", "path": "/v1.0/users", "query_json": "{\"$select\": \"displayName,userPrincipalName,accountEnabled\", \"$top\": 50}"}
```

All Azure OpenAI accounts across every subscription in one call:

```json
{"service": "arm", "method": "POST", "path": "/providers/Microsoft.ResourceGraph/resources",
 "body_json": "{\"query\": \"resources | where type =~ 'microsoft.cognitiveservices/accounts' | project name, kind, location, resourceGroup, subscriptionId\"}"}
```

Copilot Credits allocated to every environment:

```json
{"service": "powerplatform", "method": "GET", "path": "/licensing/allocationsByEnvironment"}
```

Agents in one environment straight from Dataverse:

```json
{"service": "dataverse", "method": "GET", "path": "/bots", "dataverse_org_url": "https://contoso.crm.dynamics.com",
 "query_json": "{\"$select\": \"name,publishedon,authenticationmode\", \"$top\": 100}"}
```

Quarantine an agent (returns a preview first):

```json
{"service": "powerplatform", "method": "POST", "path": "/copilotstudio/environments/<env id>/bots/<bot id>/api/botQuarantine/SetAsQuarantined", "body_json": "{}"}
```

then the same call with `"confirm": true`.

## Writes and the confirm pattern

Any POST, PUT, PATCH or DELETE that is not a read in disguise (Cost Management query, Resource Graph query, a Graph $batch made up only of GETs and a few others are treated as reads) returns:

```json
{"status": "preview", "needs_confirm": true, "request": {"method": "...", "url": "...", "body": {...}},
 "next_step": "Show this preview to the admin. If they approve, call the same tool again with confirm=true."}
```

The agent is instructed to show you the preview and wait for approval. On the server side `READ_ONLY=true` turns writes off entirely, and `ALLOW_PATTERNS` / `DENY_PATTERNS` (regular expressions matched against `METHOD url`) let you fence the server, for example `DENY_PATTERNS=DELETE .*` or `ALLOW_PATTERNS=GET .*;POST .*CostManagement.*`.

## Paging, async operations and big responses

- Responses include `next_link` when the API returns one. Pass it to `invoke_url` to get the next page.
- Azure operations that return 202 include `async_operation.poll_url`. Pass it to `poll_operation`.
- Responses are capped (`MAX_RESPONSE_CHARS`, default 60,000). When a response is truncated you get `truncated: true`, how many items were in the page and a hint to use `$select`, `$top` or a smaller date range.
- CSV downloads (Graph reports, Power Platform reports) are parsed into rows for you.

## What the server does when a call fails

- 401: refreshes the token once and retries.
- 429, 503, 504: honours Retry-After and retries with backoff up to `MAX_RETRIES`.
- 5xx: retries twice.
- Azure 400/404 with an api-version complaint: parses the supported versions from the error and retries with the newest.
- Power Platform API api-version complaint: same.
- Graph 400/404 on v1.0 for a path that only exists in beta: retries on /beta (GET only).
- Any remaining failure comes back with `hints` (what it usually means and what to change) and `suggested_operations` (the closest catalog paths to what you tried), plus the Graph permissions needed when it is a 403.

Every call is recorded in `audit_recent`: who, what, status, how long, and what healing was applied.
