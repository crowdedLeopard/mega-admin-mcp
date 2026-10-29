"""MCP server definition: instructions for the agent and tool registration."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import __version__
from .tools import register_all

INSTRUCTIONS = """You are connected to Mega Admin MCP, a runbook that can execute any Microsoft admin REST call as the signed-in administrator across Microsoft Graph (Entra, Microsoft 365, Intune, Teams, reports), Azure Resource Manager (Cost Management, Azure OpenAI and Foundry, Monitor, quotas, budgets), the Power Platform API (Copilot Credits, billing plans, environments, Copilot Studio admin), Dataverse, Power BI and Fabric.

Working pattern:
1. For FinOps and AI tokenomics questions prefer the finops_* tools; they return normalised answers (Copilot Credits purchased / allocated / consumed, Azure OpenAI tokens and cost per deployment, effective price per million tokens, licence waste).
2. For anything else: search_operations -> describe_operation -> invoke. Never guess a path; look it up.
3. Write operations (POST, PUT, PATCH, DELETE) always return a preview first. Show the preview to the admin, obtain explicit approval, then call again with confirm=true. Never set confirm=true without approval.
4. When a call fails, read the hints and suggested_operations in the response; the server has usually already retried, refreshed tokens or corrected the api-version.
5. Use whoami at the start of a conversation and doctor when anything looks wrong.
6. Tenant-wide questions about agents, apps, flows, environments or connectors (how many, which ones, created or used when, by whom, where, shared how): use pp_agents_tenant (agents) or pp_inventory (everything else). One inventory query covers the whole tenant and returns exact totals. Never loop a per-environment tool over environments. If those tools cannot express the question, write KQL for resource_graph_query against the PowerPlatformResources table.
7. Data that lives only inside Dataverse, across many environments (classic bots, transcripts, solutions, users, any table): pp_query_all_environments runs one read-only query everywhere as a background job; if it returns a job_id, poll job_status until it finishes.
7b. Copilot Credits spend or consumption over any period longer than the current month (last 3, 6 or 12 months, since a date, per agent, environment, creator, user or feature): finops_agent_credits_history. If it returns a job_id, tell the user the scan is running and poll job_status; once it has run, call it again with the same months for other groupings (instant). finops_agent_limits and finops_copilot_credits_overview are month to date only, so say so if you use them.
8. Tool calls have a hard 120 second limit. Prefer one broad call over many narrow ones, and pass next_skip_token or a job_id back rather than repeating work.
9. Access. When a result contains access_denied or access_partial, tell the user kindly and plainly that they do not have access to that thing, and confirm why using the access block: what is missing (role, app permission, consent, Azure role, environment membership or server policy), what their account has (your_roles), who can fix it and how. access.user_message is a ready sentence you may use. Do not retry, do not try other tools to get around it, and never suggest bypassing security; offer what they can do instead. If a write preview carries access_precheck, say so before asking them to confirm. When the user asks what they can do, or you are unsure whether they have the rights for a request, call my_access.
"""


def create_server() -> MCPServer:
    mcp = MCPServer(
        name="Mega Admin MCP",
        title="Mega Admin MCP for Microsoft admins",
        description="Any Microsoft admin REST call, with FinOps and AI tokenomics built in. Self-updating, self-healing.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/crowdedLeopard/mega-admin-mcp",
        version=__version__,
    )
    register_all(mcp)
    return mcp
