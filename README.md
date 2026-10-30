# Mega Admin MCP

One MCP server that gives a Copilot Studio agent the whole Microsoft admin surface, with FinOps and AI tokenomics built in. It runs any REST call you are allowed to run, as you, and it keeps itself up to date as Microsoft adds and removes operations.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FcrowdedLeopard%2Fmega-admin-mcp%2Fmain%2Fdeploy%2Fazuredeploy.json)
[![CI](https://github.com/crowdedLeopard/mega-admin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/crowdedLeopard/mega-admin-mcp/actions/workflows/ci.yml)
[![Daily catalog](https://github.com/crowdedLeopard/mega-admin-mcp/actions/workflows/catalog-daily.yml/badge.svg)](https://github.com/crowdedLeopard/mega-admin-mcp/actions/workflows/catalog-daily.yml)

## Why I built it

Every admin I work with is being asked the same three questions about AI: what is it costing us, where is the money going, and what can we do about it. The answers live in five different admin centres and a handful of APIs that each need their own token. Meanwhile every admin task that is not in a portal menu means PowerShell, a REST call and twenty minutes of finding the right endpoint.

Copilot Studio can now talk to MCP servers. So I built the server I wanted: a runbook. Ask the agent a question, it finds the right Microsoft API, calls it as you, and comes back with the answer. Ask it to change something and it shows you exactly what it is about to do before it does it.

FinOps is not a feature here, it is the point. The headline tools answer Copilot Credits, Microsoft 365 Copilot licences and Azure OpenAI tokens and cost in one turn each.

## What it does

**39 tools, four kinds.**

- **FinOps and tokenomics (19 tools).** Copilot Credits purchased, allocated and consumed, per tenant and per environment. Spend per agent over the last 6 or 12 months with creator and environment. Allocate credits and set per-agent monthly limits. Billing plans joined to their Azure cost. Microsoft 365 Copilot licence waste. Azure OpenAI inventory, tokens per deployment, cost per meter, effective price per million tokens, list price comparison, quota headroom, budgets and alerts.
- **Tenant-wide inventory (5 tools).** Every agent, app, flow, environment and connector in the tenant in one query: "how many agents were created this week, by whom, in which environments", "published agents nobody has used for 90 days", "every flow using SharePoint". Raw KQL over Azure Resource Graph when you need it, and a background job that runs one Dataverse query in every environment.
- **Power Platform administration (4 tools).** Environments, Copilot Studio agents per environment, quarantine or reassign an agent, describe any Dataverse table.
- **The runbook (10 tools).** `search_operations` over a catalog of about 55,000 operations (Microsoft Graph v1.0 and beta, Azure Resource Manager, Power Platform API, Fabric, Dataverse, Power BI, legacy Power Platform admin, Office 365 Management), `describe_operation` for parameters and permissions, `invoke` to run it, plus paging, async polling, catalog status and changes, audit, a `doctor`, and `my_access`, which tells each admin what they can and cannot do here and why.

**Every call is as the signed-in admin.** Copilot Studio signs you in to the server's own app registration; the server exchanges that token on-behalf-of you for Graph, Azure, the Power Platform API, Dataverse or Power BI. No standing credentials, no service account with God rights. A reader stays a reader.

**Straight answers when you don't have access.** If a call is refused, the agent says so kindly and confirms why: the role, app permission, consent or Azure role that is missing, what your account actually holds, and who can grant it. It does not retry or look for a way round. Write previews warn you before you confirm something that will be refused.

**Writes are previewed.** POST, PUT, PATCH and DELETE return a preview until the agent calls again with `confirm=true`, which it is instructed to do only after you say yes. `READ_ONLY=true` switches writes off completely.

**Self-updating.** A GitHub Action rebuilds the operation catalog every day from Microsoft's own published OpenAPI documents and SDKs, publishes it as a release, and every deployed server downloads it within 24 hours. New operations appear in search without a redeploy. `catalog_changes` tells you what Microsoft added or removed this week.

**Self-healing.** Token refresh on 401, Retry-After on throttling, automatic api-version correction for Azure and the Power Platform API, Graph beta fallback, a circuit breaker per host, deprecation warnings, and hints plus the nearest catalog operations when a call still fails. App Service health checks and auto-heal restart it if it ever gets stuck.

## Get going

One command for Azure and Entra, then a page that walks you through Copilot Studio. No code. [docs/01-getting-started.md](docs/01-getting-started.md) has the detail.

```powershell
git clone https://github.com/crowdedLeopard/mega-admin-mcp
cd mega-admin-mcp
az login --tenant <your tenant>
./scripts/install.ps1
```

It finishes by printing `https://<your server>/setup`. That page has every value the Copilot Studio wizard asks for, and shows what the server sees as you connect. Something not working? `./scripts/check-install.ps1` says what to fix.

Then ask the agent: *"Give me the Copilot Credits position and tell me what you would change."*

## Documentation

| Page | What it covers |
| --- | --- |
| [01 Getting started](docs/01-getting-started.md) | The 30 minute path, prerequisites, first prompts |
| [02 App registration](docs/02-app-registration.md) | Why one app does three jobs, the core and full permission tiers, secret versus managed identity, redirect URL |
| [03 Copilot Studio](docs/03-copilot-studio.md) | The MCP wizard screen by screen, agent instructions, testing, connector import |
| [04 FinOps playbook](docs/04-finops-playbook.md) | Every FinOps tool, where its numbers come from, the prompts I use, a monthly review |
| [05 The runbook](docs/05-runbook-any-rest-call.md) | search, describe, invoke; services; writes and confirm; paging; what happens on failure |
| [06 Self-updating and self-healing](docs/06-self-updating-and-self-healing.md) | The daily catalog pipeline and its guards; every healing behaviour; the limits |
| [07 Security and permissions](docs/07-security-and-permissions.md) | Identity model, safety policy, access explanations, audit, hardening, what the server will never do |
| [08 Troubleshooting](docs/08-troubleshooting.md) | The errors you will meet and what to do |
| [09 Tenant-wide inventory](docs/09-tenant-inventory.md) | Agents, apps and flows across the whole tenant in one call, last-used data, raw KQL, background jobs across every environment |

## Running it yourself

```powershell
pip install -e ".[dev]"
az login
$env:AUTH_MODE = "none"            # local only: uses your Azure CLI sign-in for downstream calls
python -m megamcp.app              # http://127.0.0.1:8000/mcp
python scripts/smoke_test.py --live
pytest
```

Rebuild the catalog locally with `python scripts/build_catalog.py` (set `GITHUB_TOKEN` for a comfortable API rate limit). It takes about two minutes.

Container: `docker run -p 8000:8000 -e AUTH_MODE=delegated -e TENANT_ID=... -e CLIENT_ID=... -e CLIENT_SECRET=... ghcr.io/crowdedleopard/mega-admin-mcp:latest`

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `AUTH_MODE` | none | delegated (Copilot Studio OAuth, on-behalf-of), apikey (server identity), none (local dev) |
| `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET` | | The app registration. Or `CLIENT_CERTIFICATE_PATH` + `CLIENT_CERTIFICATE_THUMBPRINT`, or `USE_MANAGED_IDENTITY_FIC=true` |
| `MCP_API_KEY`, `MCP_API_KEY_HEADER` | x-api-key | apikey mode |
| `USE_MANAGED_IDENTITY`, `MANAGED_IDENTITY_CLIENT_ID` | false | apikey mode: downstream calls with the managed identity |
| `READ_ONLY` | false | Disable all writes |
| `REQUIRE_CONFIRM_FOR_WRITES` | true | Preview writes until confirm=true |
| `ALLOW_PATTERNS`, `DENY_PATTERNS` | | Regular expressions matched against `METHOD url`, separated by ; or newline |
| `DEFAULT_SUBSCRIPTION_ID` | | Default scope for the cost tools |
| `DEFAULT_DATAVERSE_URL` | | Default environment for Dataverse calls |
| `CATALOG_URL`, `CHANGELOG_URL` | this repository's latest release | Where the daily catalog comes from |
| `CATALOG_AUTO_REFRESH`, `CATALOG_REFRESH_HOURS` | true, 24 | Runtime catalog refresh |
| `GRAPH_AUTO_BETA_FALLBACK`, `ARM_AUTO_API_VERSION` | true | Healing switches |
| `MAX_RESPONSE_CHARS`, `MAX_RETRIES`, `REQUEST_TIMEOUT_SECONDS` | 60000, 3, 90 | Shaping and resilience |
| `TOOL_TIME_BUDGET_SECONDS` | 100 | Time each tool call may use; Copilot Studio gives up at 120 |
| `JOB_TIME_BUDGET_SECONDS`, `JOB_MAX_RUNNING_PER_USER`, `JOB_RETENTION_HOURS`, `FANOUT_CONCURRENCY` | 1500, 3, 24, 8 | Background jobs that query every environment |
| `PUBLIC_BASE_URL` | | Used in the OAuth protected resource metadata and `/connector.yaml` |
| `PORT`, `HOST`, `MCP_PATH`, `LOG_LEVEL` | 8000, 0.0.0.0, /mcp, INFO | Hosting |

## Repository layout

```
src/megamcp/            the server (auth, catalog, runbook, tools, ASGI app)
src/megamcp/data/       catalog.json.gz, changelog.json, curated/*.json
scripts/                install.ps1, check-install.ps1, setup-entra.ps1, use-managed-identity.ps1, build_catalog.py, permissions.json, smoke_test.py
deploy/                 main.bicep, azuredeploy.json (Deploy to Azure), Copilot Studio connector definition
.github/workflows/      ci.yml, catalog-daily.yml
docs/                   the admin documentation
tests/                  offline tests including the Copilot Studio schema guard
```

## Contributing

Add operations for APIs without a public spec to `src/megamcp/data/curated/`. Add FinOps tools in `src/megamcp/tools/finops.py` and keep parameters to strings, integers, numbers and booleans so Copilot Studio can see them. Run `pytest` before you push; the daily workflow runs it again against every new catalog.

## Licence

MIT. Built by Chris Mathias. Not an official Microsoft product; use it with the same care you would use PowerShell with your admin account, because that is what it is.
