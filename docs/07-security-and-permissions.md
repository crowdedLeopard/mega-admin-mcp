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
