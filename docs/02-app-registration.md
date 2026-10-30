# The app registration

One Entra app registration does the whole job. I want to explain why, because it is the part admins ask about most, and because getting it right is what makes the server safe to put in front of a Copilot Studio agent.

## Three jobs, one app

1. **It is the API.** The app exposes a scope, `api://<client id>/access_as_user`. When you sign in to the agent's connection, Copilot Studio asks Entra for a token for that scope. The token's audience is the app, so the server can validate it against your tenant's signing keys and know who is calling.
2. **It is the OAuth client.** Copilot Studio's Manual OAuth configuration takes a client id and secret. We give it this same app. That is why the "token to self" works without an extra consent prompt for the API scope itself.
3. **It holds the delegated permissions.** Microsoft Graph, Azure Service Management, Power Platform API, Dataverse, Power BI, Power Automate and Power Apps are all listed as delegated permissions on the app. When a tool needs to call Azure, the server takes your incoming token and exchanges it on your behalf (the OAuth on-behalf-of flow) for an Azure token. You get a token for Graph the same way, and so on.

Because every downstream call is on-behalf-of you, the server can never do something you could not do yourself in the portal. A reader stays a reader. A Power Platform admin gets Power Platform admin. The audit trail in every Microsoft service shows your name, not a service account.

## Why `.default` in the Copilot Studio scopes box

The scopes field in the Copilot Studio wizard is `<client id>/.default offline_access`: the bare GUID followed by `/.default`, a space, and `offline_access`. The `.default` scope tells Entra "consent to everything statically configured on this app". That is how one sign-in consents to the API scope and all the downstream permissions in one go. If you type individual scopes there instead, the downstream exchange fails later with a consent error.

`offline_access` is the one I got wrong first time. Without it Entra hands the connection an access token but no refresh token, so the connection works for about an hour and then fails with "Failed to refresh access token ... Missing refresh token", and the agent shows "No tools available". Five days after go-live my test agent did exactly that.

Why the GUID and not `api://<client id>/.default`: the same app is both the OAuth client and the resource, so it is requesting a token for itself. Entra only allows that when the resource is identified by its GUID; the URI form fails at sign-in with `AADSTS90009: Application is requesting a token for itself`. I found that out the hard way in my own tenant, so the setup script prints the GUID form.

## Tiers

The permissions live in [scripts/permissions.json](../scripts/permissions.json). The setup script resolves names to ids against your tenant at run time, so you can add any scope name to the file.

**core**: every scope is one a user can consent to themselves. No Global Administrator needed. This covers:

| API | Scopes | What it unlocks |
| --- | --- | --- |
| Azure Service Management | user_impersonation | Cost Management, Azure OpenAI deployments and quota, Monitor token metrics, budgets, Resource Graph, anything in ARM |
| Power Platform API | Licensing.*, EnvironmentManagement.*, CopilotStudio.AdminActions.Invoke, CopilotGovernance.*, Connectivity.*, Authorization.RoleAssignments.Read, PowerApps.Apps.Read, PowerAutomate.Flows.Read, PowerPages.Websites.Read, ResourceQuery.Resources.Read, Analytics.AdvisorRecommendations.Read, AppManagement.ApplicationPackages.Read, Security.Recommendations.Read, Governance.CrossTenantConnectionReports.Read | Copilot Credits, billing plans, environments, agent quarantine and ownership, governance |
| Dataverse | user_impersonation | Agents (bot table), flows, solutions, users, roles, settings per environment |
| Power BI Service | Workspace.Read.All, Capacity.Read.All, Report.Read.All, Dataset.Read.All, Dashboard.Read.All | Workspaces and capacities you can see |
| Microsoft Flow Service | Flows.Read.All, Activity.Read.All | Legacy flow admin API |
| PowerApps Service | User | Legacy BAP and Power Apps admin API |
| Microsoft Graph | openid, profile, offline_access, User.Read, User.ReadBasic.All, Sites.Read.All, Team.ReadBasic.All | Sign in, who am I, refresh tokens |

**full** adds the scopes that need admin consent:

| API | Scopes | What it unlocks |
| --- | --- | --- |
| Microsoft Graph | Reports.Read.All, Directory.Read.All, Directory.AccessAsUser.All, Organization.Read.All, Application.Read.All, Policy.Read.All, AuditLog.Read.All, RoleManagement.Read.Directory, LicenseAssignment.ReadWrite.All, User.Read.All, Group.Read.All, ServiceHealth.Read.All, ServiceMessage.Read.All, CopilotSettings-LimitedMode.Read, AgentIdentity.Read.All, DeviceManagementConfiguration.Read.All, DeviceManagementManagedDevices.Read.All, TeamSettings.Read.All, Sites.FullControl.All | Microsoft 365 Copilot usage report and licence waste, licences, Entra admin, Intune, Conditional Access, audit |
| Power Platform API | EnvironmentManagement.Settings.ReadWrite, Groups.ReadWrite, Authorization.RoleAssignments.Write, CopilotGovernance.Settings.Write, Features.Execute, Analytics.AdvisorActions.Execute, PowerAutomate.Flows.Write | Writes on environment settings, groups, role assignments, governance |
| Power BI Service | Tenant.Read.All | Power BI admin endpoints |
| Office 365 Management APIs | ActivityFeed.Read, ServiceHealth.Read | Unified audit log feed (application only in practice) |

`Directory.AccessAsUser.All` is the big one. It lets the server act as you in Entra ID, bounded by your directory roles. If your security team would rather not, remove it from the file before running the script and the Graph tools will tell you exactly which scope they are missing when you hit a wall.

## Running the script

```powershell
./scripts/setup-entra.ps1 -Name "Mega Admin MCP" -Tier core
./scripts/setup-entra.ps1 -Name "Mega Admin MCP" -Tier full -GrantAdminConsent
./scripts/setup-entra.ps1 -AppId <client id> -AddRedirectUri "https://global.consent.azure-apim.net/redirect/..."
```

What it does, in order: finds or creates the app, sets `identifierUris` to `api://<client id>`, requests v2 access tokens, adds the `access_as_user` scope, resolves and sets every delegated permission for the tier, creates the enterprise application (service principal), creates a one year secret, and optionally grants admin consent. Re-running it is safe; it updates in place and adds a new secret each time (delete old ones in the portal).

If a resource's service principal does not exist in your tenant yet (the Power Platform API one is often missing), the script creates it with `az ad sp create` and waits for replication.

## Secret, certificate or managed identity

The server needs a credential of its own to perform the on-behalf-of exchange. Three options, in increasing order of how much I like them:

1. **Client secret** in the `CLIENT_SECRET` setting. Simple. Rotate it before it expires (the script defaults to two years). Store it as a Key Vault reference in App Service rather than plain text when you can.
2. **Certificate**: set `CLIENT_CERTIFICATE_PATH` (PEM with private key) and `CLIENT_CERTIFICATE_THUMBPRINT`.
3. **Managed identity as federated credential**: set `USE_MANAGED_IDENTITY_FIC=true`. The App Service's system-assigned identity presents itself to Entra as the app's credential and nothing needs rotating. One command does all of it:

   ```powershell
   ./scripts/use-managed-identity.ps1 -ResourceGroup <rg> -WebAppName <app name> -AppId <client id>
   ```

   It turns on the web app's identity, adds the federated credential to the app registration (Certificates and secrets, Federated credentials, scenario Managed identity), and sets the flag. It never reads or deletes a secret, so you can go back by setting the flag to false. When you are happy, delete the secret.

Use the third one. Five days after go-live my own server stopped working for everyone: after a secret rotation, the value in `CLIENT_SECRET` no longer matched any secret on the app registration. Every tool failed, and the agent quite reasonably blamed the secret. With the managed identity there is no secret on the server to get wrong, expire or leak. The connector in Copilot Studio still needs a secret for the sign-in itself; that one lives in the connector, not on the server.

The server checks its own credential. `/ready` and `doctor` both show `server_credential`: they ask Entra for an app-only Graph token, which needs no permissions and no user, so a failure there means the server cannot authenticate itself at all. The fix text names the Entra error (invalid secret, expired secret, no matching federated credential) and what to change. When a tool hits it, the agent tells the admin it is the server's credential, not their access.

## The redirect URL

Copilot Studio generates a redirect URL per connector (`https://global.consent.azure-apim.net/redirect/<connector name>`). It only appears after you select Create in the wizard, which is why the setup script has a second mode to add it afterwards. Add it as a Web platform redirect URI. Mobile and desktop redirect types will not work because the connector is a confidential client.

## Checking it worked

In Copilot Studio, create the connection and sign in. Then in the agent test pane type "run doctor". You should see a token check per service with status ok. A `failed` entry includes the Entra error code, a description, and when it is a consent problem, the admin consent URL to send to your Global Administrator.
