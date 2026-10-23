# The app registration

One Entra app registration does the whole job. I want to explain why, because it is the part admins ask about most, and because getting it right is what makes the server safe to put in front of a Copilot Studio agent.

## Three jobs, one app

1. **It is the API.** The app exposes a scope, `api://<client id>/access_as_user`. When you sign in to the agent's connection, Copilot Studio asks Entra for a token for that scope. The token's audience is the app, so the server can validate it against your tenant's signing keys and know who is calling.
2. **It is the OAuth client.** Copilot Studio's Manual OAuth configuration takes a client id and secret. We give it this same app. That is why the "token to self" works without an extra consent prompt for the API scope itself.
3. **It holds the delegated permissions.** Microsoft Graph, Azure Service Management, Power Platform API, Dataverse, Power BI, Power Automate and Power Apps are all listed as delegated permissions on the app. When a tool needs to call Azure, the server takes your incoming token and exchanges it on your behalf (the OAuth on-behalf-of flow) for an Azure token. You get a token for Graph the same way, and so on.

Because every downstream call is on-behalf-of you, the server can never do something you could not do yourself in the portal. A reader stays a reader. A Power Platform admin gets Power Platform admin. The audit trail in every Microsoft service shows your name, not a service account.

## Why `.default` in the Copilot Studio scopes box

The scopes field in the Copilot Studio wizard is `api://<client id>/.default`. The `.default` scope tells Entra "consent to everything statically configured on this app". That is how one sign-in consents to the API scope and all the downstream permissions in one go. If you type individual scopes there instead, the downstream exchange fails later with a consent error.

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

1. **Client secret** in the `CLIENT_SECRET` setting. Simple. Rotate it before it expires (the script defaults to one year). Store it as a Key Vault reference in App Service rather than plain text when you can.
2. **Certificate**: set `CLIENT_CERTIFICATE_PATH` (PEM with private key) and `CLIENT_CERTIFICATE_THUMBPRINT`.
3. **Managed identity as federated credential**: set `USE_MANAGED_IDENTITY_FIC=true`. The App Service's system-assigned identity presents itself to Entra as the app's credential and nothing needs rotating. Configure it on the app registration under Certificates and secrets, Federated credentials, Managed identity, pointing at the web app's identity. Then remove the secret.

## The redirect URL

Copilot Studio generates a redirect URL per connector (`https://global.consent.azure-apim.net/redirect/<connector name>`). It only appears after you select Create in the wizard, which is why the setup script has a second mode to add it afterwards. Add it as a Web platform redirect URI. Mobile and desktop redirect types will not work because the connector is a confidential client.

## Checking it worked

In Copilot Studio, create the connection and sign in. Then in the agent test pane type "run doctor". You should see a token check per service with status ok. A `failed` entry includes the Entra error code, a description, and when it is a consent problem, the admin consent URL to send to your Global Administrator.
