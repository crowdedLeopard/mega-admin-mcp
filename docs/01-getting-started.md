# Getting started

This is the path from nothing to an agent in Copilot Studio that can answer "how many Copilot Credits do we have left and who is burning them" and then go and fix it. One script does the Azure and Entra side. The server's own setup page walks you through Copilot Studio.

I have written this for the admin who owns the tenant, not the developer. If you can run the Azure CLI and sign in to Copilot Studio you can do all of it.

## What you need before you start

- An Entra account that can create app registrations in your tenant (any user can by default; if your tenant restricts it you need Application Developer or above).
- For the full permission tier, a Global Administrator to grant admin consent once. The core tier does not need that, so you can start today and widen later.
- An Azure subscription for the App Service (B1 is about the price of a coffee a day). The pay-as-you-go FinOps features need the subscriptions you want to see cost for, not a special one.
- Power Platform Administrator or Dynamics 365 Administrator for the Copilot Credits and environment tools. The server never has more rights than the person signed in to the agent.
- Azure CLI 2.60 or later, signed in with `az login --tenant <your tenant>`.

## Step 1. Run the installer

```powershell
git clone https://github.com/crowdedLeopard/mega-admin-mcp
cd mega-admin-mcp
az login --tenant <your tenant>
./scripts/install.ps1
```

It needs PowerShell 7 and the Azure CLI. Azure Cloud Shell has both, plus git.

The script creates the app registration, deploys an App Service running the published container, makes the web app's managed identity the server's credential, and waits until the server says it is ready. Then it checks the install from the outside and prints two things: the address of your setup page, and a client secret for the Copilot Studio wizard. The secret is shown once and only the connector uses it. The server holds no secret at all.

Every step is safe to repeat. If it stops, it says why; fix that and run it again. If the region you asked for has no quota for the plan, it moves on to the next region by itself, which is a real problem: my own subscription has no B1 quota in UK South.

The useful options are `-ResourceGroup`, `-Location`, `-AppName`, `-Sku` and `-SubscriptionId`. Add `-Tier full` if you want Microsoft 365 Copilot licence waste analysis, Entra directory reads and the Graph reporting endpoints. That tier needs admin consent, so add `-GrantAdminConsent` when a Global Administrator runs it. Details and the full scope list are in [02-app-registration.md](02-app-registration.md).

To update later, run `git pull` and the same command again.

## Step 2. Open the setup page

Go to `https://<app name>.azurewebsites.net/setup`. It shows whether the server is ready and lists every value the Copilot Studio wizard asks for, filled in for your server, with copy buttons. Follow it from top to bottom. In short:

1. In Copilot Studio open your agent, then Tools, Add a tool, New tool, Model Context Protocol.
2. Paste the server name, description and URL. Choose OAuth 2.0, then Manual, and paste the rest, with the client secret from the installer.
3. Select Create. Copilot Studio shows a redirect URL, and you register it:

   ```powershell
   ./scripts/setup-entra.ps1 -AppId <client id> -AddRedirectUri "<the redirect URL>"
   ```

4. Select Next, create a new connection, sign in, then Add to agent. The agent now lists 39 tools.

Keep the page open while you do this. Copilot Studio tends to report a failed connection as a generic error, but the server can see why it refused, so the page shows the reason and what to change. The commonest ones are a scope typed wrongly and an account from another tenant.

If the sign-in prompt says it needs admin approval, your tenant doesn't let users consent for themselves. The setup page has a consent link to send to a Global Administrator. It brings them back to the page, which confirms it worked.

Leave **Credentials to use** on **End user credentials**. With Maker-provided credentials, everyone who can talk to the agent acts with your admin roles. That is true of any connector, but this one can do a lot.

Full walkthrough in [03-copilot-studio.md](03-copilot-studio.md).

## Step 3. If something is off

```powershell
./scripts/check-install.ps1 -AppId <client id> -ResourceGroup rg-mega-admin-mcp -WebAppName <app name>
```

It changes nothing. It checks the app registration, the secret's expiry, the redirect URL, consent, the managed identity and the server, and says what to fix for each. Run it whenever a connection that used to work stops working.

## Other ways to deploy

**Deploy to Azure button.** Handy for a quick look. It deploys the same container, but you still have to run the app registration and managed identity steps yourself, so I use the installer.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FcrowdedLeopard%2Fmega-admin-mcp%2Fmain%2Fdeploy%2Fazuredeploy.json)

**Your own container platform.** `ghcr.io/crowdedleopard/mega-admin-mcp:<version>`, port 8000, with `TENANT_ID`, `CLIENT_ID` and a credential. Container Apps, AKS or anything else that runs a container works. Started with no settings it refuses every call and explains why on `/setup`, rather than running open. Set `STATE_DIR` to a path that survives restarts to keep the audit log. Background jobs are held in memory, so run one instance.

**From source.** The template can still build from GitHub (`containerImage=''` and `deployFromGitHub=true`), but the build takes six to eight minutes on B1 and is one more thing to fail.

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
