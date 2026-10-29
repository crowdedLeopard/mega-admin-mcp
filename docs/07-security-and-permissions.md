# Security and permissions

An MCP server that can "perform any admin action" is only acceptable if it cannot do anything the person in front of it could not do, if every action is attributable, and if the dangerous ones are gated. This page is what I would want to read before letting it near my tenant.

## Identity model

**Delegated mode (`AUTH_MODE=delegated`), the default and the one for Copilot Studio.**

1. Copilot Studio signs the admin in with the app registration and sends the server an Entra access token whose audience is the app (`api://<client id>`).
2. The server validates the token on every request: RS256 signature against the tenant's published keys, issuer (`login.microsoftonline.com/<tenant>/v2.0` or `sts.windows.net/<tenant>/`), audience, expiry. Tokens from another tenant are rejected when `TENANT_ID` is set.
3. For each downstream call the server exchanges that token on-behalf-of the admin for the target API (Graph, Azure, Power Platform API, the Dataverse environment, Power BI). The downstream token carries the admin's identity and only the permissions both the admin and the app hold.
4. Downstream tokens are cached per admin and service until five minutes before expiry and never written to disk.

Consequences: the server has no standing access to anything. If the admin loses a role, the server loses it with them. Every Microsoft audit log (Entra sign-ins, Azure Activity Log, Dataverse audit, Power Platform admin audit) shows the admin, with the app as the client application.

**API key mode (`AUTH_MODE=apikey`).** For scheduled jobs: the endpoint is protected by a header secret and downstream calls use the server's own identity (managed identity or client credentials). This needs application permissions or RBAC role assignments and is a conscious choice; the documentation tells you to prefer delegated mode.

**None (`AUTH_MODE=none`).** Local development only. The server refuses nothing and uses your Azure CLI login. Never expose it.

## The safety policy

- Every POST, PUT, PATCH and DELETE returns a preview unless `confirm=true`. The agent instructions tell the model to show the preview and obtain approval. Read-like POSTs (Cost Management query, Resource Graph, Graph $batch, validation and list-keys style endpoints) are treated as reads.
- `READ_ONLY=true` disables writes entirely. Good for a reporting agent.
- `ALLOW_PATTERNS` and `DENY_PATTERNS` are regular expressions matched against `METHOD url`. Deny wins. Examples: deny everything under `/users/` for DELETE; allow only the Power Platform API and Cost Management.
- `invoke_url` only follows URLs whose host belongs to a known Microsoft service. Nothing can make the server call an arbitrary host with your token.
- Secrets never appear in responses or logs: Authorization and API key headers are redacted in previews, tokens are not logged.
- The tenant-wide inventory tools are read-only and respect the inventory's Entra role model: Power Platform Administrator and Global Reader see everything, AI Administrator sees agents only. `resource_graph_query` runs KQL, which cannot change anything. `pp_query_all_environments` only ever sends GET.
- Background jobs run as the admin who started them, with that admin's delegated token, and only that admin can see or cancel them.

## When you don't have access

Admins ask agents for things they are not allowed to do all the time, usually without knowing it. A bare "403 Forbidden" helps nobody, and an agent that quietly tries five other tools to get round it is worse. So every tool result goes through one check: if any call was refused for access reasons, the result carries `access_denied` (nothing came back) or `access_partial` (some of it did), plus an explanation the agent is told to relay kindly and without retrying.

The explanation says what you were trying to do, why it was refused, exactly what is missing, what your account has, and who can fix it. Where the server can confirm the cause it does, rather than guessing:

| Cause | How it is confirmed | Who can fix it |
| --- | --- | --- |
| Missing Entra role (for example Power Platform Administrator for Copilot Credit allocation) | Your active directory roles, read from Graph `/me/transitiveMemberOf` (needs only User.Read) | A Global Administrator or Privileged Role Administrator, or you, by activating an eligible role in PIM |
| The app was never granted the permission (for example Reports.Read.All for Copilot usage reports) | The scopes actually present in your Graph token, against the permission the operation needs in the catalog | A Global Administrator, Privileged Role Administrator or Cloud Application Administrator (admin consent) |
| Missing Azure role (for example Cost Management Contributor to create a budget) | Azure's own message names the action and scope; the server lists your role assignments on that subscription | An Owner or User Access Administrator on the subscription |
| Not a user in a Dataverse environment, or a missing Dataverse privilege | Dataverse's message (the privilege name, such as prvReadBot, is quoted) | A Power Platform Administrator or the environment's System Administrator |
| Conditional Access, MFA, not assigned to the app, consent not given | The Entra error code from the token exchange | You (sign in again) or your Conditional Access or application administrator |
| This server's own policy (READ_ONLY, ALLOW_PATTERNS, DENY_PATTERNS) | The server's configuration | Whoever runs the server; nothing is wrong with your account |

If you already hold a role that normally allows the action, the explanation says so and points at the narrower causes (a role scoped to an administrative unit, a PIM role that is eligible but not active, a resource-level restriction) instead of telling you to get a role you already have.

Write previews get the same treatment before you confirm: if the change needs a Graph permission the app does not have, or a role you do not hold (User Administrator to create users, Power Platform Administrator for credit allocation), the preview carries `access_precheck` and the agent warns you first.

**`my_access`** answers the question before it is asked: your active roles, the permissions the app holds for you in Graph, Azure, the Power Platform API and Power BI, your Azure roles on up to eight subscriptions, and every capability of the server marked available, limited or unavailable with the reason and who can grant it. `whoami` shows your roles too. Roles you are only eligible for in Privileged Identity Management are not counted until you activate them.

## Audit

`audit_recent` returns the last `AUDIT_LOG_SIZE` calls (default 500): timestamp, admin, method, URL, status, duration, whether it was a write, and what self-healing did. Each entry is also written as a JSON log line to stdout, which App Service captures and which you can ship to Log Analytics with a diagnostic setting. Combine it with the Microsoft side logs for a complete trail.

## Hosting hardening

The Bicep template enables HTTPS only, TLS 1.2 minimum, FTPS disabled, a system-assigned managed identity, health checks and auto-heal. Beyond that:

- Put the client secret in Key Vault and reference it from the App Service setting, or switch to the managed identity federated credential (`USE_MANAGED_IDENTITY_FIC=true`) and delete the secret.
- Restrict inbound traffic. Copilot Studio reaches the server through Power Platform connector infrastructure; if you front it with API Management or Front Door, point the connector at that.
- Keep the app registration single tenant (the script does). Multi-tenant is not supported by design.
- Set `READ_ONLY=true` on any instance whose only job is reporting.

## Data handling

The server is a pass-through. It does not persist responses, it holds the audit ring in memory only, and it truncates large responses before returning them to the agent. Copilot Studio stores conversation transcripts according to your tenant's settings; the server adds nothing to that.

## Data loss prevention

Access from Copilot Studio goes through a Power Platform custom connector. Your DLP policies apply to it like any connector, so you can classify it as Business, Non-business or Blocked per environment, and you can use connector endpoint filtering to allow only your server's host.

## What the server will never do

- Call a host that is not a known Microsoft API.
- Execute a write without `confirm=true`.
- Act with more rights than the signed-in admin in delegated mode.
- Store tokens or responses on disk.
- Change its own tool list at runtime (the catalog changes, the tools do not).
