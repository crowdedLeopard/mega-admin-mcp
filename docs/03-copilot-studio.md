# Connecting Copilot Studio

Copilot Studio talks to MCP servers over Streamable HTTP through a Power Platform custom connector. The MCP onboarding wizard builds that connector for you; this page walks through it screen by screen and explains the choices.

## Before you open the wizard

- The server is deployed and `https://<app name>.azurewebsites.net/ready` says ready.
- You have the client id and secret from `setup-entra.ps1`.
- You are in a Copilot Studio environment that allows custom connectors. The default environment is often locked down by a tenant DLP policy; use a developer or sandbox environment first.
- You are signing in to Copilot Studio with the account that holds the admin roles you want the agent to use. The agent will never have more rights than that account.

## The wizard

1. Open the agent, select **Tools**, then **Add a tool**, **New tool**, **Model Context Protocol**.
2. **Server name**: `Mega Admin MCP`.
3. **Server description**: the orchestrator uses this sentence to decide whether to call the server at all, so make it describe the job: `Runs any Microsoft admin REST call as the signed-in administrator and answers FinOps questions about Copilot Credits, Microsoft 365 Copilot licences and Azure OpenAI tokens and cost.`
4. **Server URL**: `https://<app name>.azurewebsites.net/mcp`
5. **Authentication**: OAuth 2.0, then **Manual**. Dynamic discovery and Dynamic will not work because Entra does not do dynamic client registration.
6. Fill in:

   | Field | Value |
   | --- | --- |
   | Client ID | your client id |
   | Client secret | the secret |
   | Authorization URL | `https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/authorize` |
   | Token URL template | `https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/token` |
   | Refresh URL | `https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/token` |
   | Scopes | `<client id>/.default` (GUID form; the `api://` form fails with AADSTS90009) |

7. Select **Create**. Copilot Studio creates the connector and shows a **redirect URL**. Copy it.
8. Add the redirect URL to the app registration: `./scripts/setup-entra.ps1 -AppId <client id> -AddRedirectUri "<url>"` (or Entra admin centre, App registrations, Authentication, Add a platform, Web).
9. Back in the wizard select **Next**, then **Create a new connection**. A sign-in window opens. Sign in and accept the consent prompt; it lists the permissions from the tier you chose. On the core tier any admin can accept it; on the full tier a Global Administrator must have consented first or the prompt says admin approval is needed.
10. Select **Add to agent**. The tools page now lists 37 tools from the server.

## Making the agent use it well

The server sends instructions to the model with every connection (how to search, describe, invoke, and to always show write previews). Two things on the agent side help:

- In the agent instructions add a line like: *Use Mega Admin MCP for any question about Copilot Credits, licences, Azure OpenAI tokens or cost, Power Platform environments and agents, Entra, Azure or Microsoft 365 administration. Prefer the finops tools for cost questions. Always show write previews and ask before confirming.*
- Turn on generative orchestration for the agent (it is the default for new agents). The tool descriptions are written so the orchestrator can pick the right one from a natural question.

## Testing

In the test pane:

1. *"Run whoami."* You should see your user principal name under caller, the auth mode delegated, and the policy.
2. *"Run doctor."* Every token check should be ok. If one fails, the advice list tells you which permission or role is missing.
3. *"Copilot Credits overview."*
4. *"Search for operations about conditional access policies and describe the first one."*
5. *"Allocate 1000 credits to <an environment> but do not confirm."* You get the preview and a question. That is the behaviour you want.

Use the activity map (the icon in the test pane) to see which tool was called with what arguments and what came back.

## Importing the connector yourself instead

If your organisation requires reviewed connector definitions, import [deploy/copilot-studio-connector.yaml](../deploy/copilot-studio-connector.yaml) in Power Apps (Custom connectors, New custom connector, Import an OpenAPI file). The server also serves a filled-in copy at `/connector.yaml`. The key line is `x-ms-agentic-protocol: mcp-streamable-1.0` on the POST operation; that is what tells Copilot Studio the endpoint speaks MCP. Then add it to the agent as a custom connector tool.

A solution-aware connector can be moved between environments with the usual ALM. The connection (the sign-in) is per environment and per user and does not travel.

## Known behaviour

- Copilot Studio asks the server for its tool list each time you open the tool (and there is a refresh button on the tool's Tools tab). When I deployed version 1.1.0 with five new tools, they appeared in the agent without re-adding the server. New Microsoft operations do not need even that; they arrive through the catalog.
- **Every tool call has a hard 120 second limit** in the connector infrastructure. When it is hit the agent shows `ConnectorTimeoutError: The connector ... has timed out after 120 seconds`. The server knows this: every request gets a time budget (`TOOL_TIME_BUDGET_SECONDS`, default 100) and the runbook will not sleep on a `Retry-After` that would overrun it; instead the call returns at once with `retry_after_seconds` so the agent can tell you to try again. Fan-out tools (`finops_tokenomics` across a subscription, `finops_billing_plans`) return what they have when the budget runs low and say what they skipped. I found this the hard way: the first tokenomics run honoured a 60 second Retry-After from the Retail Prices API and Copilot Studio gave up before the answer came back.
- The orchestrator will happily loop a tool over 40 resources if you let it. Tool descriptions steer it towards the one-call forms (`finops_tokenomics` with `subscription_id` and no `resource_id` analyses every account in a subscription with a single Cost Management query). If you write your own prompts, ask for the subscription view first and drill into one account afterwards.
- Retail list prices are cached in the server for twelve hours. prices.azure.com throttles aggressively (a 60 second Retry-After is normal) and the prices do not change by the minute.
- Responses over about 60,000 characters are truncated by the server with a hint on how to narrow the call.
- DLP policies apply to the connector like any other. If the agent says the tool is blocked, check the environment's DLP classification for the connector.
