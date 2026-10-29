# Tenant-wide inventory

The first question I asked the agent after it went live was "list all agents created in the last seven days". It got confused. The tools could list agents in one environment, and my dev tenant has 802 environments, so the agent stopped and told me what tool it wished it had. Fair enough. This page is that tool, and a few more.

## The idea

Power Platform keeps an inventory of everything makers build: every Copilot Studio agent (including Agent Builder agents), app, flow, Power Pages site, environment, environment group and connector in the tenant. It lives in a table called `PowerPlatformResources` in Azure Resource Graph and changes show up within about 15 to 20 minutes. One KQL query over that table answers questions that would otherwise take a call per environment.

The difference is not subtle. In my dev tenant, "agents created in the last seven days, with environment names and who created them" is one query that comes back in under three seconds: 319 agents across 208 environments. The per-environment route would have been 802 Dataverse calls and a timeout.

The inventory also carries usage records with the last day each agent, app or flow was used. Microsoft does not document those yet, but they are there, and they turn "which agents has nobody touched for 90 days" into a filter.

## The tools

**`pp_agents_tenant`** is the one you will use most. Every agent in the tenant, filtered by any mix of:

- created in the last N days, or between two dates
- environment (id, or part of the name)
- name contains
- owner or creator (object id, UPN or display name)
- published, or draft only
- no recorded usage in the last N days
- shared with the entire tenant
- authentication mode (None, Microsoft Entra, Generic OAuth 2.0)
- created in Copilot Studio or Agent Builder
- quarantined

It always returns the exact total and a summary of everything that matched: how many are published, used in the last 30 days, shared with the whole tenant, have no authentication, are quarantined, were built in Agent Builder, were created by the system rather than a person, and how many environments and creators are involved. Add `group_by` for counts by environment, environment type, owner, creator, day, month, authentication, model and more. Rows come back newest first with environment names and owner names resolved from Entra; `include_details` adds channels, sharing, model, orchestration and connector counts.

**`pp_inventory`** does the same for everything else: apps (canvas, model-driven, code, managed), flows (cloud, agent, workflow), Power Pages sites, environments, environment groups and connectors. Its extra trick is the connector filter: "every cloud flow that uses SharePoint" is `resource_type=cloud_flows, connector=sharepoint`. `group_by=connector` ranks connectors by how many apps, flows and agents use them, which is the question you want answered before a DLP change or a connector deprecation.

**`resource_graph_query`** takes your own KQL when the first two cannot express the question. The tool description carries a cheat sheet of the table's types and fields so the agent can write sensible queries, and if the query is wrong the error now includes the parser's line, position and token so the agent can fix it. It also works on the Azure tables (`resources`, `resourcecontainers`, `advisorresources`, `policyresources`).

**`pp_query_all_environments`** and **`job_status`** cover what the inventory does not: data that only exists inside Dataverse. Classic bots, conversation transcripts, solutions, security roles, row counts of any table. It runs one read-only query in every environment (or the ones you filter to) as a background job. If it finishes within about 70 seconds you get the answer straight away; otherwise you get a job id and the agent polls `job_status`. Counting bots across my 13 personal environments took ten seconds.

## Prompts that work

- "How many agents were created in the last seven days? Break it down by environment and tell me who created the most."
- "Show me published agents nobody has used for 90 days, grouped by month created."
- "Which agents are shared with the entire tenant and have no authentication?"
- "List everything Jane Smith owns: agents, apps and flows."
- "Which connectors are used by the most apps and flows?"
- "Every cloud flow that uses SharePoint and was modified this month."
- "How many environments do we have of each type?"
- "Count conversation transcripts in every production environment." (background job)

## Things I learned building it

**Most new agents are not made by people.** In my dev tenant, 2,317 of the 2,589 agents created in the last 30 days have an all-zero creator id: the platform created them (for example, the Email Assist Agent that appears when certain Dynamics 365 features are switched on). The summary reports `system_created` separately so a spike in "new agents" does not send you chasing makers who did nothing.

**Agent Builder agents are recorded as "Copilot Studio Lite".** The schema documentation says Agent Builder; the data says Lite. The `created_in` filter accepts either.

**Usage data is sparse.** About 3,800 of 24,000 agents in my tenant have a last-used date. Empty means no usage recorded, not proof of zero use; treat "not used for 90 days" as a list to review, not a list to delete.

**Classic bots are missing.** The inventory only tracks the current Copilot Studio runtime, not Power Virtual Agents V1 bots. Use `pp_query_all_environments` with `dataverse_path=/bots` if you still have them.

**Configuration fields describe the published version.** An agent with unpublished changes shows its last published configuration.

## Permissions

The inventory is governed by Entra roles, not Power Platform security roles:

| Role | Sees |
| --- | --- |
| Global Administrator, Power Platform Administrator, Dynamics 365 Administrator, Global Reader | Everything |
| AI Administrator, AI Reader | Agents, agentic apps, agent flows, environments and environment groups |

The server tries Azure Resource Graph first (it takes raw KQL and needs the Azure Service Management permission, which is in the core tier). If Resource Graph refuses, it falls back to the Power Platform API inventory endpoint, which takes the same query as structured clauses and needs only the Power Platform API permission. Both are delegated-only: they do not work with a service principal, which is fine because Copilot Studio always calls as you. The tool response says which route answered.

If Conditional Access demands MFA for Azure Resource Manager, the inventory may refuse until you sign in again; Microsoft documents the same limitation for the admin centre.

`pp_query_all_environments` uses your own Dataverse access in each environment. Environments where you are not a user, or where your role cannot read the table, are listed under `failures` with the reason, so the totals never silently leave anything out.

## Limits

- Rows are capped at 500 per call and trimmed to keep answers readable; totals and summaries always cover every match. Follow `next_skip_token` for the next page.
- Background jobs live in memory on the instance that started them for 24 hours. A restart forgets them, and if you scale out to several instances a poll can land on the wrong one. The template deploys one instance; keep it that way or turn on session affinity.
- A user can run three jobs at a time (`JOB_MAX_RUNNING_PER_USER`), each for up to 25 minutes (`JOB_TIME_BUDGET_SECONDS`), querying eight environments at once (`FANOUT_CONCURRENCY`).
