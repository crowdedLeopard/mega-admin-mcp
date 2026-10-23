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
