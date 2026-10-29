"""my_access: what the signed-in admin can and cannot do with this server, and why."""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..access import FIX_AZURE, FIX_CONSENT, FIX_ENTRA_ROLE, PP_ADMIN, azure_roles_at, caller_facts, granted_scopes
from ..auth.entra import current_principal
from ..auth.tokens import TokenError, broker
from ..catalog.services import SERVICES
from ..config import settings
from ..runbook.invoke import runbook
from .common import READ

FULL_INVENTORY = {"Power Platform Administrator", "Dynamics 365 Administrator", "Global Administrator", "Global Reader"}
AI_INVENTORY = {"AI Administrator", "AI Reader"}
REPORT_ROLES = {"Reports Reader", "Global Reader", "AI Administrator", "Usage Summary Reports Reader", "Global Administrator"}
AZ_READ = {"Reader", "Contributor", "Owner", "Cost Management Reader", "Cost Management Contributor", "Monitoring Reader", "Billing Reader"}
AZ_BUDGET = {"Contributor", "Owner", "Cost Management Contributor"}


def _cap(area: str, status: str, why: str, fix: str = "") -> dict[str, Any]:
    return {"area": area, "status": status, "why": why, **({"how_to_get_it": fix} if fix and status != "available" else {})}


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "What can I do here? Checks the signed-in admin's active Entra roles, the permissions the app holds for them in Graph, Azure and the "
        "Power Platform API, and their Azure roles, then lists every capability of this server as available, limited or unavailable with the "
        "reason and who can grant it. Use when the user asks what they can do, why something failed, or before attempting admin changes."))
    async def my_access(
        include_azure: Annotated[bool, Field(description="Also check Azure role assignments on up to 8 subscriptions")] = True,
    ) -> dict[str, Any]:
        p = current_principal()
        facts_t = caller_facts(p, force=True)
        scope_t = asyncio.gather(*(granted_scopes(s, p) for s in ("graph", "arm", "powerplatform", "powerbi")))
        facts, (g_scopes, arm_scopes, pp_scopes, pbi_scopes) = await asyncio.gather(facts_t, scope_t)
        roles = set(facts.get("roles") or [])
        roles_known = facts.get("roles") is not None

        async def token_ok(name: str) -> bool:
            svc = SERVICES.get(name)
            if not svc or not svc.scope:
                return False
            try:
                await broker.get_token(svc.scope, p)
                return True
            except TokenError:
                return False

        o365_ok, devops_ok = await asyncio.gather(token_ok("o365mgmt"), token_ok("devops"))

        subs: list[dict[str, Any]] = []
        if include_azure:
            r = await runbook.call("arm", "GET", "/subscriptions", api_version="2022-12-01", principal=p, max_chars=400_000, retry_wait_budget=3)
            if r.get("ok"):
                subs = [{"id": s.get("subscriptionId"), "name": s.get("displayName")} for s in (r.get("body") or {}).get("value", [])]
            oid = facts.get("object_id", "")
            got = await asyncio.gather(*(azure_roles_at(s["id"], oid, p) for s in subs[:8]))
            for s, rl in zip(subs[:8], got):
                s["your_roles"] = rl if rl is not None else "could not be read"
        az_roles = {x for s in subs for x in (s.get("your_roles") if isinstance(s.get("your_roles"), list) else [])}

        def has(*names: str) -> bool:
            return any(n in roles for n in names)

        caps: list[dict[str, Any]] = []
        # Tenant inventory
        if roles & FULL_INVENTORY:
            caps.append(_cap("Tenant-wide inventory: agents, apps, flows, environments, connectors", "available", f"you hold {', '.join(sorted(roles & FULL_INVENTORY))}"))
        elif roles & AI_INVENTORY:
            caps.append(_cap("Tenant-wide inventory: agents, apps, flows, environments, connectors", "limited",
                             f"{', '.join(sorted(roles & AI_INVENTORY))} sees agents, agentic apps, agent flows and environments only (not canvas or model-driven apps or cloud flows)",
                             "Power Platform Administrator or Global Reader for everything. " + FIX_ENTRA_ROLE))
        else:
            caps.append(_cap("Tenant-wide inventory: agents, apps, flows, environments, connectors", "unavailable" if roles_known else "unknown",
                             "needs Power Platform Administrator, Dynamics 365 Administrator, Global Reader, Global Administrator, or AI Administrator / AI Reader for agents",
                             FIX_ENTRA_ROLE))
        # Power Platform admin (credits, billing, agent actions)
        pp = has(*PP_ADMIN)
        caps.append(_cap("Copilot Credits, capacity, billing plans and per-agent limits (read)", "available" if pp else ("unavailable" if roles_known else "unknown"),
                         f"you hold {', '.join(sorted(set(PP_ADMIN) & roles))}" if pp else "needs Power Platform Administrator, Dynamics 365 Administrator or Global Administrator",
                         FIX_ENTRA_ROLE))
        caps.append(_cap("Allocate Copilot Credits, set agent limits, quarantine / reassign / delete agents (write)",
                         "available" if pp and not settings.read_only else ("unavailable" if roles_known or settings.read_only else "unknown"),
                         "this server is READ_ONLY" if settings.read_only else (f"you hold {', '.join(sorted(set(PP_ADMIN) & roles))}; every change is previewed first" if pp else "needs Power Platform Administrator"),
                         "" if settings.read_only else FIX_ENTRA_ROLE))
        # Azure
        if include_azure:
            readable = [s["name"] for s in subs[:8] if isinstance(s.get("your_roles"), list) and set(s["your_roles"]) & AZ_READ]
            budget = [s["name"] for s in subs[:8] if isinstance(s.get("your_roles"), list) and set(s["your_roles"]) & AZ_BUDGET]
            if subs:
                caps.append(_cap("Azure OpenAI / Foundry inventory, tokens, cost, tokenomics and quota", "available" if readable else "limited",
                                 f"readable subscriptions (of {len(subs)} visible): {', '.join(readable[:6]) or 'none of the first 8 grant Reader-level access directly or through groups'}",
                                 "Reader, Cost Management Reader or Monitoring Reader on the subscription. " + FIX_AZURE))
                caps.append(_cap("Create Azure budgets", "available" if budget and not settings.read_only else "unavailable",
                                 f"Contributor-level roles on: {', '.join(budget[:6])}" if budget else "needs Cost Management Contributor, Contributor or Owner on the scope",
                                 FIX_AZURE))
            else:
                caps.append(_cap("Azure OpenAI / Foundry inventory, tokens, cost, tokenomics and quota", "unavailable",
                                 "your account cannot see any Azure subscription", "Ask for Reader (or Cost Management Reader) on the subscriptions that host AI. " + FIX_AZURE))
        # Microsoft 365 Copilot reports
        g = set(g_scopes or [])
        rep_scope = "Reports.Read.All" in g
        rep_role = bool(roles & REPORT_ROLES)
        status = "available" if rep_scope and rep_role else "unavailable"
        why = []
        if not rep_scope:
            why.append("the app has not been granted Reports.Read.All (admin consent, full tier)")
        if not rep_role:
            why.append("your account has no reporting role (Reports Reader, Global Reader, AI Administrator or Usage Summary Reports Reader)")
        caps.append(_cap("Microsoft 365 Copilot licence usage and waste", status, "; ".join(why) or "permission and role in place",
                         FIX_CONSENT if not rep_scope else FIX_ENTRA_ROLE))
        # Entra administration through Graph
        entra_scopes = sorted(g & {"Directory.Read.All", "User.Read.All", "Group.Read.All", "Policy.Read.All", "AuditLog.Read.All", "RoleManagement.Read.Directory", "Application.Read.All"})
        caps.append(_cap("Entra administration through Graph (users, groups, policies, audit, roles)", "available" if entra_scopes else "limited",
                         f"app permissions granted: {', '.join(entra_scopes)}" if entra_scopes else "the app has only basic Graph permissions (User.Read, User.ReadBasic.All); admin reads need the full tier with admin consent",
                         FIX_CONSENT))
        # Power BI / Fabric admin
        pbi = set(pbi_scopes or [])
        fabric_role = has("Fabric Administrator", "Power BI Administrator", "Global Administrator")
        caps.append(_cap("Power BI / Fabric admin APIs", "available" if fabric_role and "Tenant.Read.All" in pbi else ("limited" if pbi else "unavailable"),
                         ("you hold Fabric Administrator" if fabric_role else "needs Fabric Administrator") + ("" if "Tenant.Read.All" in pbi else "; Tenant.Read.All not granted to the app (full tier)"),
                         FIX_ENTRA_ROLE))
        caps.append(_cap("Office 365 Management activity feed", "available" if o365_ok else "unavailable",
                         "token obtained" if o365_ok else "the app has no consented permission for the Office 365 Management API (optional, full tier)", FIX_CONSENT))
        caps.append(_cap("Dataverse data inside environments", "depends on the environment",
                         "you need a user record with a security role in each environment; pp_query_all_environments reports where you lack it", ""))
        caps.append(_cap("Any other Microsoft admin API (search_operations + invoke)", "depends on the operation",
                         "runs as you: if a call is refused, the response explains which role or permission is missing", ""))

        unavailable = [c["area"] for c in caps if c["status"] in ("unavailable", "limited")]
        return {
            "ok": True,
            "you": {"name": facts.get("name") or p.name, "upn": facts.get("upn") or p.upn, "object_id": facts.get("object_id")},
            "your_entra_roles": sorted(roles) if roles_known else f"could not be read ({facts.get('error')})",
            "roles_note": "Only roles active right now are listed. Roles you are merely eligible for in Privileged Identity Management do not count until you activate them.",
            "app_permissions_granted": {"graph": g_scopes, "azure": arm_scopes, "power_platform": pp_scopes, "power_bi": pbi_scopes,
                                        "office365_management": "yes" if o365_ok else "no", "azure_devops": "yes" if devops_ok else "no"},
            "azure_subscriptions": subs[:8] + ([{"note": f"{len(subs) - 8} more not checked"}] if len(subs) > 8 else []) if include_azure else "not checked",
            "server_policy": {"read_only": settings.read_only, "writes_need_confirmation": settings.require_confirm_for_writes},
            "capabilities": caps,
            "summary": ("You can use everything this server offers." if not unavailable else
                        f"You can't fully use: {'; '.join(unavailable)}. Each entry says why and who can grant it."),
        }
