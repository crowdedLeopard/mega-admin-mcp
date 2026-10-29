# FinOps and tokenomics playbook

FinOps is the reason this server exists. Admins are being asked "what are we spending on AI, where, and is it worth it" and the answer is spread across the Power Platform admin centre, the Microsoft 365 admin centre, Azure Cost Management, Azure Monitor and a pricing page. The `finops_*` tools pull those together into answers an agent can give in one turn. This page explains what each one does, where the numbers come from, and the prompts I use.

## The three currencies you are managing

| Currency | Where it is bought | Where it is consumed | Where the data is |
| --- | --- | --- | --- |
| Copilot Credits (entitlement `MCSMessages`) | Prepaid packs, prepurchase plans, pay-as-you-go billing plans linked to Azure subscriptions | Copilot Studio agents (all harnesses), Copilot Chat agents, agent flows, AI Builder when AI credits run out | Power Platform API licensing namespace; Azure Cost Management for the pay-as-you-go side |
| Microsoft 365 Copilot licences | Per user per month | Nothing to meter, the waste is unused seats | Graph subscribedSkus and the Copilot usage report |
| Azure OpenAI and Foundry tokens | Pay-as-you-go per million tokens, or PTU reservations | Every call your apps and agents make | Azure Monitor metrics per deployment; Azure Cost Management per meter; Azure Retail Prices for list price |

## Copilot Credits

**`finops_copilot_credits_overview`** is the headline. It reads the tenant currency report (purchased, allocated, consumed month to date for every currency) and the billing plans, and tells you whether any plan has the Copilot Studio meter enabled. Insights are plain sentences: "less than 5% of purchased credits are allocated", "no billing plan has the Copilot Studio meter enabled so environments cannot overflow to pay-as-you-go".

Prompt: *"Give me the Copilot Credits position and tell me what you would change."*

**`finops_environment_credits`** zooms into one environment: allocation, whether it draws from the tenant pool (the `TenantPool` enforcement rule), consumption against the allocation, the linked billing plan and any per-agent limits. Pass an environment name, id or URL.

Prompt: *"How are Copilot Credits set up for the Contoso Production environment?"*

**`finops_allocate_copilot_credits`** sets the allocation and the tenant pool rule. It shows what is available to allocate first and returns a preview; nothing changes until you confirm.

Prompt: *"Allocate 250,000 credits to Contoso Production and stop it drawing from the tenant pool."*

**`finops_agent_limits`** and **`finops_set_agent_limit`** are the Manage Agents page. The first lists every agent with month-to-date consumption, limit, notification threshold and hard stop. The second sets a monthly limit on one agent with a notification percentage and an optional hard stop.

Prompt: *"Which agents have used the most credits this month and which have no limit?"* then *"Put a 20,000 credit monthly limit with an 80% alert on the top one."*

**`finops_agent_credits_history`** answers the question every finance conversation starts with: what have we spent, on which agents, over the last six or twelve months? It returns billed and non-billable Copilot Credits per agent with the agent name, creator, environment, number of users, top features and last activity, plus a month-by-month tenant trend. `group_by` switches the view to environment, creator, user or feature, and `environment` or `name_contains` narrow it. Any period up to 24 months works (`months`, or `from_date` and `to_date`).

How it gets there matters, because the obvious API does not work. The tenant-wide per-agent operation (`/licensing/entitlements/MCSMessages/resources`) answers 403 for a Power Platform Administrator, and the licensing report endpoint answers 404. What does work is per user: one call lists everyone who consumed credits in the period, and a call per user returns their usage per agent and feature. The server adds those up. Each user's agent rows add up exactly to that user's total, and the result shows `coverage`: how much of the tenant's billed total it could attribute to agents, and which users could not be broken down. My dev tenant has data back to May 2025, so twelve months is real history, not an estimate.

The scan touches every consuming user, so the first run for a period is a background job: in my tenant about 220 users and a few minutes. The agent gets a job id, polls `job_status`, and shows the answer when it is ready. The result is cached for six hours, so the follow-up questions ("now by environment", "just the Sales environment", "who created the top five") come back instantly.

Prompt: *"Copilot Credits spend over the last six months by agent, with creator and environment."* then *"Same for twelve months, grouped by environment."*

**`finops_billing_plans`** joins the admin centre with the Azure bill. For each billing plan: status, region, linked environments, Azure subscription and resource group, which meters are enabled, and month-to-date cost from Cost Management split by meter (you will see "Pay As You Go Copilot Credit" as a meter when the credits meter is spinning).

**`finops_link_environment_billing_plan`** links or unlinks an environment to a plan, with preview.

**`finops_copilot_studio_consumption_report`** calls the Power Platform API that generates the downloadable consumption report (by environment, agent, product, feature, channel, model). In October 2026 it returned 404 in my tenant for every report name, including `FeatureDetailsPerResource` and `BillingDetailsPerEnvironment` from the SDK, on every supported API version; the tool tells you that honestly and points at `finops_agent_credits_history`. It will start working when Microsoft lights it up, because the catalog and the tool are already there.

## Microsoft 365 Copilot licences

**`finops_m365_copilot_licences`** lists every SKU with Copilot in the name (purchased, assigned, unassigned) and, if the app has `Reports.Read.All`, reads the Copilot usage report to count licensed users with no activity in N days. It multiplies that by a list price you can override to give a monthly waste figure. Unassigned seats are flagged separately.

Prompt: *"How many Microsoft 365 Copilot licences are we wasting?"*

## Azure OpenAI and Foundry

**`finops_azure_ai_inventory`** uses Azure Resource Graph to find every Cognitive Services account (kind OpenAI or AIServices) across subscriptions, then lists deployments: model, version, SKU (GlobalStandard, DataZoneStandard, Standard, ProvisionedManaged), capacity, rate limits, who created it and when.

**`finops_azure_ai_tokens`** reads Azure Monitor metrics `ProcessedPromptTokens`, `GeneratedTokens` and `TokenTransaction` split by deployment, daily or hourly, for the last N days.

**`finops_azure_ai_cost`** runs a Cost Management query for a scope and timeframe, filtered to Cognitive Services accounts (or any resource type), grouped by resource and meter, and classifies meters as input, output, cached input, cache write, provisioned, images, audio or fine tuning.

**`finops_tokenomics`** is the one to show your finance partner. It has two modes. Leave `resource_id` empty and it analyses **every OpenAI and AI Services account in a subscription in one call**: one Cost Management query for the subscription, token metrics and deployments for the top accounts by cost in parallel, other Cognitive Services kinds (Translator, Speech, Vision) listed with their cost but not analysed. Pass `resource_id` and it goes deep on one account. Both join tokens and cost over the same window and give, per deployment:

- input, output and total tokens, active days, output share
- cost split into input, output, cached and cache write meters
- effective price per million tokens: input (all prompt tokens, including cached, because that is what the metric counts), output, blended
- how much of the input cost is coming through cache meters
- daily run rate and a projected 30 day cost
- the matching Azure retail list prices for the model in that region (global, data zone, regional) for comparison
- recommendations in plain words: idle deployments holding capacity, low cache use on heavy input workloads, output heavy workloads, candidates for provisioned throughput

How it attributes cost: Cost Management meters carry the model name in the meter name (for example `gpt 4o 1120 Inp glbl 1M Tokens`). The tool matches meter names to deployment model names. Anything it cannot attribute is listed under `unassigned_meters` and stays in the account total, so nothing disappears.

Caveats I want you to know: Cost Management lags usage by up to 24 hours and can be re-rated until the invoice closes; token metrics are near real time. The tool uses yesterday as the end of the window for that reason. Cached tokens are counted in `ProcessedPromptTokens` but billed at the cached rate, which is why the effective input price can look very low on a workload with a stable system prompt.

Prompts: *"Run tokenomics for subscription Contoso-AI for the last 30 days and tell me where the money goes."* then *"Now drill into the account with the highest cost."* The subscription form matters in Copilot Studio: a tool call has 120 seconds, and the first time I asked for "tokenomics for all my deployments" the orchestrator looped the single-account tool over 40 accounts and timed out. One call, ranked by cost, is what you want.

**`finops_azure_ai_quota`** shows tokens-per-minute quota, PTU limits, usage and headroom per region. **`finops_price_lookup`** returns list prices for a model, per direction and deployment type, in any currency. **`finops_create_budget`** puts an Azure budget with 80% actual and 100% forecast alerts on a subscription or resource group. **`finops_cost_alerts`** lists active cost alerts and budgets with current spend.

## A monthly review in six prompts

1. "Copilot Credits overview, then environment credits for every production environment that has an allocation."
2. "Agent limits: top ten by consumption, and which have no limit."
3. "Billing plans with month-to-date Azure cost."
4. "Microsoft 365 Copilot licence waste at 30 days."
5. "Tokenomics for the AI subscription, last 30 days, then drill into any account with an effective price above list."
6. "Create a budget on the AI resource group at 110% of last month's cost with alerts to finops@contoso.com." (preview, then confirm)

Every write comes back as a preview first. Read it, then say yes.
