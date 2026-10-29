# Getting started

This is the 30 minute path from nothing to an agent in Copilot Studio that can answer "how many Copilot Credits do we have left and who is burning them" and then go and fix it. You need three things: an Entra app registration, somewhere to run the server, and a Copilot Studio agent to attach it to.

I have written this for the admin who owns the tenant, not the developer. If you can run the Azure CLI and sign in to Copilot Studio you can do all of it.

## What you need before you start

- An Entra account that can create app registrations in your tenant (any user can by default; if your tenant restricts it you need Application Developer or above).
- For the full permission tier, a Global Administrator to grant admin consent once. The core tier does not need that, so you can start today and widen later.
- An Azure subscription for the App Service (B1 is about the price of a coffee a day). The pay-as-you-go FinOps features need the subscriptions you want to see cost for, not a special one.
- Power Platform Administrator or Dynamics 365 Administrator for the Copilot Credits and environment tools. The server never has more rights than the person signed in to the agent.
- Azure CLI 2.60 or later, signed in with `az login --tenant <your tenant>`.

## Step 1. Create the app registration (5 minutes)

```powershell
git clone https://github.com/crowdedLeopard/mega-admin-mcp
cd mega-admin-mcp
./scripts/setup-entra.ps1 -Name "Mega Admin MCP" -Tier core
```

The script creates one app registration that does three jobs: it is the API Copilot Studio signs you in to, it is the OAuth client Copilot Studio uses, and it holds the delegated permissions the server exchanges on your behalf. It prints the client id, a secret, and the exact values to type into Copilot Studio. Keep that output; you need it in steps 2 and 3.

Pick `-Tier full` if you want Microsoft 365 Copilot licence waste analysis, Entra directory reads and the Graph reporting endpoints. That tier needs admin consent, so add `-GrantAdminConsent` when a Global Administrator runs it, or send them the consent URL the script prints.

Details and the full scope list are in [02-app-registration.md](02-app-registration.md).

## Step 2. Deploy the server (10 minutes)

Option A, the button. It deploys an App Service plan and web app, pulls the code from this repository and starts it.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FcrowdedLeopard%2Fmega-admin-mcp%2Fmain%2Fdeploy%2Fazuredeploy.json)

Fill in: a unique app name, your tenant id, the client id and secret from step 1, and optionally a default Azure subscription id for the cost tools. Leave `authMode` as `delegated`.

Option B, the command line, which is what I use:

```powershell
az group create -n rg-mega-admin-mcp -l uksouth
az deployment group create -g rg-mega-admin-mcp -f deploy/bicep/main.bicep `
  -p appName=<unique name> tenantId=<tenant id> clientId=<client id> clientSecret=<secret> defaultSubscriptionId=<sub id>
```

Two things I hit in my own tenant that you might too. If validation fails with `InternalSubscriptionIsOverQuotaForSku`, the subscription has no quota for that SKU in that region; choose another region or SKU (the plan can live in a different region from the resource group). And the first build on a B1 plan takes six to eight minutes; if a command line deploy reports a gateway timeout, the build is usually still finishing, so check the deployment log before you redeploy.

Either way, when it finishes open `https://<app name>.azurewebsites.net/ready`. You want `"status": "ready"` and a catalog count in the tens of thousands. If `/ready` says degraded it tells you which setting is missing.

Option C is a container: `ghcr.io/crowdedleopard/mega-admin-mcp:latest`, same environment variables, port 8000. Container Apps, AKS or anything else that can run a container works; the server is stateless.

## Step 3. Add it to Copilot Studio (10 minutes)

1. In Copilot Studio open your agent, go to Tools, select Add a tool, then New tool, then Model Context Protocol.
2. Server name `Mega Admin MCP`. Server description, something like `Runs any Microsoft admin REST call as the signed-in administrator and answers FinOps questions about Copilot Credits, Microsoft 365 Copilot licences and Azure OpenAI tokens and cost.` The orchestrator reads this to decide when to call the server, so say what it does in plain words.
3. Server URL `https://<app name>.azurewebsites.net/mcp`.
4. Authentication: OAuth 2.0, type Manual. Paste the values the setup script printed: client id, client secret, authorization URL, token URL, refresh URL, and scopes `<client id>/.default offline_access` (the GUID form, because `api://<client id>/.default` fails with AADSTS90009 when the app requests a token for itself; and `offline_access`, without which the connection stops working after about an hour).
5. Select Create. Copilot Studio shows a redirect URL. Copy it and run:

   ```powershell
   ./scripts/setup-entra.ps1 -AppId <client id> -AddRedirectUri "<the redirect URL>"
   ```

6. Back in the wizard select Next, create a new connection, sign in (you consent to the permissions once), then Add to agent.

The agent now lists 38 tools. Full walkthrough with what every screen looks like in [03-copilot-studio.md](03-copilot-studio.md).

## Step 4. Ask it something

Start with these in the test pane:

- "Run whoami and tell me what you can reach."
- "Give me the Copilot Credits position for the tenant and flag anything I should worry about."
- "Which environment has the most Copilot Credits allocated and is it drawing from the tenant pool?"
- "List my Azure OpenAI deployments and show tokens and cost per deployment for the last 30 days."
- "What would it cost to run gpt-4o in UK South, input and output, global versus regional?"
- "Find the operation that lists DLP policies and run it."

Anything that changes state comes back as a preview first. The agent shows you exactly what it is about to do and asks for a yes. That is deliberate and you cannot switch it off from the chat side.

## What happens next without you doing anything

- Every day at 04:17 UTC the repository rebuilds its operation catalog from Microsoft's published API descriptions and publishes it as a release. Your server downloads it within 24 hours. New Microsoft operations appear in `search_operations` without a redeploy. Ask the agent "what did Microsoft add to the Power Platform API this week" and it will tell you.
- The server retries throttled calls, refreshes tokens, corrects api-versions and falls back from Graph v1.0 to beta when a path only exists there. App Service recycles it if it ever goes unhealthy.

If something is off, ask the agent to run `doctor`. It checks every downstream API, the catalog and the configuration, and tells you what to fix.
