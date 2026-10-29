"""Explain, kindly and precisely, why the signed-in admin cannot do something.

Every tool result passes through `enrich()`. When a call failed because of
access (a missing Entra role, a permission the app was never consented, an
Azure RBAC role, Dataverse membership, Conditional Access, or this server's own
safety policy), the result gains:

    access_denied | access_partial : true
    access : {kind, what, reason, missing, your_roles, who_can_fix, how_to_fix,
              evidence, user_message}

`user_message` is a ready, polite sentence the agent can relay. The reason is
confirmed, not guessed, wherever the server can check: it reads the admin's
active directory roles (Graph /me/transitiveMemberOf, needs only User.Read),
the scopes actually granted in the downstream token, and the admin's Azure role
assignments on the subscription that refused.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import jwt

from .auth.entra import Principal, current_principal
from .config import settings

log = logging.getLogger("megamcp.access")

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# ---------------------------------------------------------------------------
# what each tool needs (used to say *what* the user cannot do and *what it takes*)
# ---------------------------------------------------------------------------
PP_ADMIN = ["Power Platform Administrator", "Dynamics 365 Administrator", "Global Administrator"]
INVENTORY_ROLES = ["Power Platform Administrator", "Dynamics 365 Administrator", "Global Administrator", "Global Reader",
                   "AI Administrator (agents only)", "AI Reader (agents only)"]


@dataclass
class Need:
    what: str
    roles: list[str] = field(default_factory=list)  # Entra roles, any one of
    azure: str = ""  # Azure RBAC role needed on the scope
    other: str = ""  # anything else (consent, environment membership)


TOOL_NEEDS: dict[str, Need] = {
    "pp_agents_tenant": Need("the tenant-wide agent inventory", INVENTORY_ROLES),
    "pp_inventory": Need("the tenant-wide Power Platform inventory", INVENTORY_ROLES, other="AI Administrator and AI Reader see agents, agentic apps, agent flows and environments only"),
    "resource_graph_query": Need("Azure Resource Graph queries", INVENTORY_ROLES, azure="Reader on the subscriptions (for Azure tables)"),
    "pp_environments": Need("the list of all Power Platform environments", PP_ADMIN, other="without an admin role you only see environments you belong to"),
    "pp_copilot_studio_agents": Need("agents inside a Dataverse environment", other="a user account with a security role in that environment"),
    "pp_agent_admin_action": Need("Copilot Studio agent admin actions (quarantine, reassign, delete)", PP_ADMIN, other="the CopilotStudio.AdminActions.Invoke permission on the app"),
    "dataverse_describe_table": Need("Dataverse table metadata", other="a user account with a security role in that environment"),
    "pp_query_all_environments": Need("Dataverse data across environments", other="a user account with a security role in each environment"),
    "finops_copilot_credits_overview": Need("tenant Copilot Credits and capacity", PP_ADMIN),
    "finops_environment_credits": Need("Copilot Credits for an environment", PP_ADMIN),
    "finops_allocate_copilot_credits": Need("Copilot Credit allocation", ["Power Platform Administrator", "Dynamics 365 Administrator", "Global Administrator"]),
    "finops_agent_limits": Need("per-agent Copilot Credit limits", PP_ADMIN),
    "finops_agent_credits_history": Need("Copilot Credits spend history per agent", PP_ADMIN),
    "finops_set_agent_limit": Need("changing per-agent Copilot Credit limits", PP_ADMIN),
    "finops_billing_plans": Need("pay-as-you-go billing plans", PP_ADMIN, azure="Cost Management Reader (or Reader) on the linked subscriptions for the cost figures"),
    "finops_link_environment_billing_plan": Need("linking environments to billing plans", PP_ADMIN, azure="Contributor on the billing plan's resource group"),
    "finops_copilot_studio_consumption_report": Need("Copilot Studio consumption reports", PP_ADMIN),
    "finops_azure_ai_inventory": Need("the Azure OpenAI / Foundry inventory", azure="Reader on the subscriptions"),
    "finops_azure_ai_tokens": Need("Azure OpenAI token metrics", azure="Monitoring Reader (or Reader) on the account"),
    "finops_azure_ai_cost": Need("Azure AI cost", azure="Cost Management Reader (or Reader) on the scope"),
    "finops_tokenomics": Need("Azure OpenAI tokenomics", azure="Cost Management Reader and Monitoring Reader (Reader covers both) on the subscription"),
    "finops_azure_ai_quota": Need("Azure OpenAI quota", azure="Reader on the subscription"),
    "finops_m365_copilot_licences": Need("Microsoft 365 Copilot licence and usage data", ["Reports Reader", "Global Reader", "AI Administrator", "Usage Summary Reports Reader", "Global Administrator"],
                                         other="the Reports.Read.All and Organization.Read.All permissions consented for the app (full tier)"),
    "finops_create_budget": Need("creating Azure budgets", azure="Cost Management Contributor (or Contributor) on the scope"),
    "finops_cost_alerts": Need("Azure cost alerts and budgets", azure="Cost Management Reader (or Reader) on the scope"),
    "finops_subscriptions": Need("your Azure subscriptions", azure="any role on a subscription"),
    "invoke": Need("this Microsoft API call"),
    "invoke_url": Need("this Microsoft API call"),
    "poll_operation": Need("this long-running operation"),
}
NO_ENRICH = {"whoami", "doctor", "my_access", "audit_recent", "catalog_status", "catalog_changes", "search_operations", "describe_operation", "job_status"}

# ---------------------------------------------------------------------------
# Graph path -> directory roles that typically allow it
# ---------------------------------------------------------------------------
GRAPH_ROLE_HINTS: list[tuple[str, str, list[str]]] = [
    (r"/reports/.*copilot", "read", ["AI Administrator", "Reports Reader", "Global Reader", "Usage Summary Reports Reader"]),
    (r"/reports", "read", ["Reports Reader", "Global Reader", "Usage Summary Reports Reader"]),
    (r"/auditlogs", "read", ["Reports Reader", "Security Reader", "Global Reader"]),
    (r"/identity/conditionalaccess|/policies", "read", ["Security Reader", "Conditional Access Administrator", "Global Reader"]),
    (r"/identity/conditionalaccess|/policies", "write", ["Conditional Access Administrator", "Security Administrator"]),
    (r"/rolemanagement|/directoryroles", "write", ["Privileged Role Administrator"]),
    (r"/rolemanagement|/directoryroles", "read", ["Directory Readers", "Global Reader", "Privileged Role Administrator"]),
    (r"/subscribedskus|/assignlicense|/licensedetails", "write", ["License Administrator", "User Administrator"]),
    (r"/users", "write", ["User Administrator"]),
    (r"/groups", "write", ["Groups Administrator", "User Administrator"]),
    (r"/applications|/serviceprincipals|/oauth2permissiongrants", "write", ["Application Administrator", "Cloud Application Administrator"]),
    (r"/security", "read", ["Security Reader", "Global Reader"]),
    (r"/security", "write", ["Security Administrator", "Security Operator"]),
    (r"/devicemanagement|/deviceappmanagement", "read", ["Intune Administrator", "Global Reader"]),
    (r"/devicemanagement|/deviceappmanagement", "write", ["Intune Administrator"]),
    (r"/teamwork|/teams", "write", ["Teams Administrator"]),
    (r"/admin/sharepoint|/sites", "write", ["SharePoint Administrator"]),
]

AZURE_ACTION_HINTS: list[tuple[str, str]] = [
    (r"microsoft\.(costmanagement|consumption|billing)/.*(write|delete)", "Cost Management Contributor (or Contributor)"),
    (r"microsoft\.(costmanagement|consumption|billing)/", "Cost Management Reader (or Reader)"),
    (r"microsoft\.insights/metrics", "Monitoring Reader (or Reader)"),
    (r"microsoft\.cognitiveservices/accounts/listkeys", "Cognitive Services Contributor"),
    (r"microsoft\.cognitiveservices/.*(write|delete)", "Cognitive Services Contributor (or Contributor)"),
    (r"microsoft\.authorization/", "User Access Administrator or Owner"),
    (r"microsoft\.powerplatform/.*(write|delete|action)", "Contributor on the billing plan's resource group"),
    (r"/read$", "Reader"),
    (r"(write|delete|action)$", "Contributor (or the resource's own contributor role)"),
]

FIX_ENTRA_ROLE = ("A Global Administrator or Privileged Role Administrator can assign the role. "
                  "If you are eligible for it through Privileged Identity Management, activate it and sign in to the connection again.")
FIX_CONSENT = ("A Global Administrator, Privileged Role Administrator or Cloud Application Administrator can grant admin consent for the app "
               "(Entra admin centre > App registrations > the app > API permissions), or re-run setup-entra.ps1 with -Tier full -GrantAdminConsent.")
FIX_AZURE = "An Owner or User Access Administrator on the subscription (or resource group) can assign the Azure role."
FIX_DATAVERSE = "A Power Platform Administrator or a System Administrator of that environment can add you as a user with a security role."


# ---------------------------------------------------------------------------
# facts about the caller (cached)
# ---------------------------------------------------------------------------
_facts_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_role_names_cache: dict[str, str] = {}
FACTS_TTL = 600


_ROLE_NAMES: dict[str, str] | None = None


def role_name(template_id: Any) -> str | None:
    """Built-in Entra role name from its role template id (the same in every tenant)."""
    global _ROLE_NAMES
    if _ROLE_NAMES is None:
        try:
            import json

            from .config import DATA_DIR
            _ROLE_NAMES = {k.lower(): v for k, v in json.loads((DATA_DIR / "entra_role_templates.json").read_text("utf-8")).get("roles", {}).items()}
        except Exception:  # noqa: BLE001
            log.warning("entra_role_templates.json could not be read; role names fall back to Graph displayName")
            _ROLE_NAMES = {}
    return _ROLE_NAMES.get(str(template_id or "").lower())


def directory_role_names(members: list[dict[str, Any]]) -> list[str]:
    """Active directory role names from /me/transitiveMemberOf.

    With only User.Read (the core tier) Graph returns directoryRole objects with displayName null but
    roleTemplateId set, so the name comes from the template id. Custom or unknown templates keep their id.
    """
    names: set[str] = set()
    for v in members:
        if not str(v.get("@odata.type", "")).endswith("directoryRole"):
            continue
        tid = v.get("roleTemplateId")
        name = v.get("displayName") or role_name(tid) or (f"Entra role {tid}" if tid else None)
        if name:
            names.add(name)
    return sorted(names)


async def caller_facts(principal: Principal | None = None, force: bool = False) -> dict[str, Any]:
    """Active directory roles and object id of the signed-in admin. Never raises; cached for ten minutes."""
    from .runbook.invoke import runbook  # late import: runbook imports this module

    p = principal or current_principal()
    key = p.cache_key
    hit = _facts_cache.get(key)
    if hit and not force and time.time() - hit[0] < FACTS_TTL:
        return hit[1]
    facts: dict[str, Any] = {"roles": None, "object_id": p.oid if GUID.match(p.oid or "") else "", "upn": p.upn, "error": ""}
    if p.mode == "apikey":
        facts["error"] = "server identity (apikey mode): directory roles of a person do not apply"
        _facts_cache[key] = (time.time(), facts)
        return facts
    me_r, roles_r = await asyncio.gather(
        runbook.call("graph", "GET", "/v1.0/me", query={"$select": "id,userPrincipalName,displayName"}, principal=p, max_chars=20_000, retry_wait_budget=3),
        runbook.call("graph", "GET", "/v1.0/me/transitiveMemberOf", query={"$select": "id,displayName,roleTemplateId", "$top": "999"},
                     principal=p, max_chars=400_000, retry_wait_budget=3),
        return_exceptions=True)
    if isinstance(me_r, dict) and me_r.get("ok"):
        b = me_r.get("body") or {}
        facts["object_id"] = facts["object_id"] or str(b.get("id") or "")
        facts["upn"] = facts["upn"] or b.get("userPrincipalName") or ""
        facts["name"] = b.get("displayName")
    if isinstance(roles_r, dict) and roles_r.get("ok"):
        vals = list((roles_r.get("body") or {}).get("value", []))
        next_link, pages = roles_r.get("next_link"), 1
        while next_link and pages < 5:  # people in more than 999 groups
            more = await runbook.call("graph", "GET", next_link, principal=p, max_chars=400_000, retry_wait_budget=3)
            if not (isinstance(more, dict) and more.get("ok")):
                break
            vals += (more.get("body") or {}).get("value", [])
            next_link, pages = more.get("next_link"), pages + 1
        facts["roles"] = directory_role_names(vals)
    else:
        facts["error"] = (roles_r.get("message") if isinstance(roles_r, dict) else str(roles_r))[:200] if roles_r else "role lookup failed"
    _facts_cache[key] = (time.time(), facts)
    return facts


async def granted_scopes(service_name: str, principal: Principal | None = None) -> list[str] | None:
    """Delegated scopes present in the caller's token for a service (None when unknown)."""
    from .auth.tokens import broker
    from .catalog.services import SERVICES

    p = principal or current_principal()
    svc = SERVICES.get(service_name)
    if not svc or not svc.scope:
        return None
    try:
        token = await broker.get_token(svc.scope, p)
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except Exception:  # noqa: BLE001
        return None
    scp = claims.get("scp")
    if isinstance(scp, str):
        return sorted(scp.split())
    roles = claims.get("roles")
    return sorted(roles) if isinstance(roles, list) else []


async def azure_roles_at(subscription_id: str, object_id: str, principal: Principal | None = None) -> list[str] | None:
    """Names of the Azure roles assigned to the user (directly or through groups) at or above a subscription."""
    from .runbook.invoke import runbook

    if not (subscription_id and object_id and GUID.match(object_id)):
        return None
    p = principal or current_principal()
    r = await runbook.call("arm", "GET", f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleAssignments",
                           query={"$filter": f"assignedTo('{object_id}')"}, api_version="2022-04-01", principal=p, max_chars=400_000, retry_wait_budget=3)
    if not r.get("ok"):
        return None
    def_ids = sorted({str((a.get("properties") or {}).get("roleDefinitionId") or "") for a in (r.get("body") or {}).get("value", [])} - {""})
    names: list[str] = []

    async def name_of(def_id: str) -> None:
        if def_id in _role_names_cache:
            names.append(_role_names_cache[def_id])
            return
        d = await runbook.call("arm", "GET", def_id, api_version="2022-04-01", principal=p, max_chars=50_000, retry_wait_budget=3)
        n = ((d.get("body") or {}).get("properties") or {}).get("roleName") if d.get("ok") else None
        if n:
            _role_names_cache[def_id] = n
            names.append(n)

    await asyncio.gather(*(name_of(d) for d in def_ids[:20]))
    return sorted(set(names))


# ---------------------------------------------------------------------------
# finding and classifying access problems
# ---------------------------------------------------------------------------
def _is_problem(d: dict[str, Any]) -> bool:
    s = d.get("status")
    return (s in (401, 403)) or d.get("error") == "token_acquisition_failed" or s == "blocked"


def find_problems(obj: Any, depth: int = 0, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Collect runbook-style failure dicts that are about access, anywhere in a tool result (bounded walk)."""
    out = [] if out is None else out
    if depth > 5 or len(out) >= 20:
        return out
    if isinstance(obj, dict):
        if _is_problem(obj):
            out.append(obj)
            return out
        for k, v in obj.items():
            if k in ("body", "response_headers", "request", "rows", "items", "agents", "value", "data"):
                continue
            find_problems(v, depth + 1, out)
    elif isinstance(obj, list):
        for v in obj[:200]:
            find_problems(v, depth + 1, out)
    return out


def _text(problem: dict[str, Any]) -> str:
    body = problem.get("body")
    parts = [str(problem.get("message") or ""), str(problem.get("description") or ""), str(problem.get("error_code") or ""),
             str(problem.get("aad_error") or ""), str(problem.get("reason") or "")]
    if isinstance(body, dict):
        err = body.get("error") or body
        if isinstance(err, dict):
            parts += [str(err.get("code") or ""), str(err.get("message") or "")]
            inner = err.get("innerError") or err.get("innererror") or {}
            if isinstance(inner, dict):
                parts.append(str(inner.get("code") or ""))
        else:
            parts.append(str(err))
    elif isinstance(body, str):
        parts.append(body[:1500])
    return " ".join(p for p in parts if p)


def _graph_roles_for(method: str, path: str) -> list[str]:
    kind = "read" if method.upper() == "GET" else "write"
    low = path.lower()
    for rx, k, roles in GRAPH_ROLE_HINTS:
        if k == kind and re.search(rx, low):
            return roles
    return ["Global Reader"] if kind == "read" else []


def _azure_role_for(action: str) -> str:
    a = action.lower()
    for rx, role in AZURE_ACTION_HINTS:
        if re.search(rx, a):
            return role
    return "an Azure role that allows this action"


def _service_title(service: str) -> str:
    return {"graph": "Microsoft Graph", "arm": "Azure Resource Manager", "powerplatform": "the Power Platform API", "dataverse": "Dataverse",
            "powerbi": "Power BI", "fabric": "Microsoft Fabric", "bap": "the Power Platform admin API", "powerapps": "the Power Apps admin API",
            "flow": "the Power Automate admin API", "o365mgmt": "the Office 365 Management API", "devops": "Azure DevOps"}.get(service, service or "the service")


async def classify(problem: dict[str, Any], tool: str = "", principal: Principal | None = None) -> dict[str, Any]:
    """Turn one failed call into a confirmed, plain-English access explanation."""
    p = principal or current_principal()
    need = TOOL_NEEDS.get(tool)
    what = need.what if need else "this action"
    service = str(problem.get("service") or "")
    status = problem.get("status")
    url = str(problem.get("url") or (problem.get("request") or {}).get("url") or "")
    method = str(problem.get("method") or (problem.get("request") or {}).get("method") or "GET")
    path = urlparse(url).path if url.startswith("http") else url
    if not service and url.startswith("http"):
        try:
            from .catalog.services import service_for_url
            svc = service_for_url(url)
            service = svc.name if svc else ""
        except Exception:  # noqa: BLE001
            service = ""
    text = _text(problem)
    low = text.lower()
    inventory_call = tool in ("pp_agents_tenant", "pp_inventory") or "/resourcequery/" in url.lower()
    a: dict[str, Any] = {"has_access": False, "what": what}
    evidence = {k: v for k, v in {"service": service, "status": status, "method": method, "url": url[:300],
                                  "message": str(problem.get("message") or problem.get("reason") or "")[:300]}.items() if v not in (None, "")}

    # 1. this server's own safety policy
    if status == "blocked":
        a.update(kind="server_policy", reason=f"This Mega Admin MCP server is configured to block it: {problem.get('reason')}.",
                 missing="nothing on your account; the server's own policy (READ_ONLY, ALLOW_PATTERNS or DENY_PATTERNS)",
                 who_can_fix="Whoever runs this Mega Admin MCP server (the App Service settings).",
                 how_to_fix="Ask them to change READ_ONLY / ALLOW_PATTERNS / DENY_PATTERNS if this action should be allowed.")
    # 2. token could not be obtained
    elif problem.get("error") == "token_acquisition_failed":
        scope = str(problem.get("scope") or "")
        svc_name = next((n for n in ("graph", "management.azure", "powerplatform", "powerbi", "manage.office", "dynamics", "crm", "devops", "fabric") if n in scope), scope)
        if problem.get("server_credential_problem"):
            a.update(kind="server_credential", reason="The Mega Admin MCP server could not prove its own identity to Microsoft Entra, so it cannot "
                     "call any Microsoft API for anyone right now. This is not about your roles or permissions.",
                     missing="nothing on your account; the server's own credential (client secret, certificate or managed identity)",
                     who_can_fix="Whoever runs this Mega Admin MCP server (the App Service settings and the app registration).",
                     how_to_fix=str(problem.get("fix") or "Run doctor and read server_credential."))
        elif "AADSTS65001" in text or problem.get("suberror") == "consent_required" or "consent" in low:
            a.update(kind="consent_missing", reason=f"The app has not been granted permission to call {scope.split('/.default')[0] or 'this API'} on your behalf yet.",
                     missing=f"consent for the app's permissions to {svc_name}", who_can_fix=FIX_CONSENT,
                     how_to_fix="If the permission is user-consentable, sign out of the Copilot Studio connection and sign in again; otherwise ask for admin consent.",
                     admin_consent_url=problem.get("admin_consent_url"))
        elif "AADSTS50105" in text:
            a.update(kind="not_assigned_to_app", reason="Your account is not assigned to the Mega Admin MCP app, and the app requires assignment.",
                     missing="assignment to the enterprise application", who_can_fix="A Cloud Application Administrator or Application Administrator.",
                     how_to_fix="Entra admin centre > Enterprise applications > the app > Users and groups > add yourself or your group.")
        elif "AADSTS53003" in text or "AADSTS53000" in text or "AADSTS53001" in text:
            a.update(kind="conditional_access", reason="A Conditional Access policy blocked the sign-in for this API (for example a compliant-device or location rule).",
                     missing="a sign-in that satisfies Conditional Access", who_can_fix="Your Conditional Access administrator.",
                     how_to_fix="Use a compliant device or allowed location, or ask for the policy to include this app.")
        elif problem.get("claims_challenge") or problem.get("aad_error") == "interaction_required" or "AADSTS50076" in text or "AADSTS50079" in text:
            a.update(kind="reauthentication_required", reason="Conditional Access wants you to sign in again (usually multi-factor authentication) before this API can be used.",
                     missing="a fresh sign-in with MFA", who_can_fix="You.", how_to_fix="Sign out of the connection in Copilot Studio and sign in again, completing MFA.")
        else:
            a.update(kind="token_failed", reason=f"Could not get a token for {svc_name}: {str(problem.get('description') or problem.get('message'))[:200]}",
                     missing="a valid token", who_can_fix="You (sign in again) or the server administrator (app configuration).",
                     how_to_fix=str(problem.get("fix") or "Sign out of the Copilot Studio connection and sign in again; if it persists run doctor."))
    # 3. the API answered 401 / 403
    elif inventory_call and status in (401, 403):
        a.update(kind="role_missing", reason="Power Platform inventory refused the query for your account. Inventory access comes from Entra roles, not Power Platform security roles.",
                 missing="an inventory role: Power Platform Administrator, Dynamics 365 Administrator, Global Reader or Global Administrator (AI Administrator / AI Reader for agents only)",
                 needed_roles=INVENTORY_ROLES, who_can_fix=FIX_ENTRA_ROLE,
                 how_to_fix="Ask for Power Platform Administrator or Global Reader, then sign in again. If Conditional Access demands MFA for Azure Resource Manager, sign in again first.")
    elif service == "graph":
        perms = {}
        try:
            from .runbook.healing import graph_permissions_for
            perms = graph_permissions_for(method, path)
        except Exception:  # noqa: BLE001
            pass
        needed = perms.get("del") or []
        granted = await granted_scopes("graph", p)
        explicit = bool(needed) and granted is not None and any(s in granted for s in needed)
        broad = granted is not None and "Directory.AccessAsUser.All" in granted
        roles = _graph_roles_for(method, path)
        facts = await caller_facts(p)
        mine = facts.get("roles")
        holds = [r for r in roles if mine and r in mine]
        perm_text = ", ".join(needed[:4]) or "the permission shown by describe_operation"
        if (needed and granted is not None and not explicit and not broad) or "s2sunauthorized" in low or "invalid permission" in low:
            a.update(kind="app_permission_missing",
                     reason=(f"The Mega Admin MCP app has not been granted any of the Graph permissions this needs ({perm_text}). "
                             "Your role is not the problem here; the app's consent is."),
                     missing=f"Graph delegated permission: {needed[0] if needed else 'the operation permission'} (admin consent)",
                     granted_graph_permissions=granted, who_can_fix=FIX_CONSENT,
                     how_to_fix="Add the permission to the app registration (it is in the full tier of scripts/permissions.json) and grant admin consent; then sign in to the connection again.")
        elif needed and not explicit and broad:
            a.update(kind="app_permission_missing",
                     reason=(f"The app does not hold the specific Graph permission this API checks for ({perm_text}). It holds Directory.AccessAsUser.All, "
                             "which covers most directory data but not every API: Privileged Identity Management, reports and policies need their own permissions."
                             + ("" if holds or not roles else f" You would also need a role such as {', '.join(roles[:3])}.")),
                     missing=f"Graph delegated permission: {needed[0]} (admin consent)" + ("" if holds or not roles else f", and a role such as {roles[0]}"),
                     needed_roles=[] if holds else roles, granted_graph_permissions=granted, who_can_fix=FIX_CONSENT,
                     how_to_fix="Add the permission to the app registration and grant admin consent; then sign in to the connection again.")
        elif holds:
            a.update(kind="role_missing",
                     reason=(f"Microsoft Graph refused {method.upper()} {path}. The app's permission is in place and you hold {', '.join(holds)}, which normally allows it, "
                             "so the refusal is narrower: a scoped or administrative-unit role, a role that is only eligible in PIM, or a resource-level restriction."),
                     missing="a role assignment that covers this specific resource", who_can_fix=FIX_ENTRA_ROLE,
                     how_to_fix="Check the role is active (not just eligible) and assigned at tenant scope, then sign in again.")
        else:
            a.update(kind="role_missing",
                     reason=f"Microsoft Graph refused because your account does not hold a directory role that allows {method.upper()} {path}. The app's permission is in place.",
                     missing=f"an Entra role such as {', '.join(roles[:3])}" if roles else "an Entra role that allows this action",
                     needed_roles=roles, who_can_fix=FIX_ENTRA_ROLE, how_to_fix="Ask for one of those roles (time-bound through PIM is ideal), then sign in again.")
    elif service == "arm":
        m = re.search(r"perform action '([^']+)' over scope '([^']+)'", text)
        action, scope = (m.group(1), m.group(2)) if m else ("", "")
        role = _azure_role_for(action or path)
        sub = re.search(r"/subscriptions/([0-9a-fA-F-]{36})", scope or url)
        facts = await caller_facts(p)
        mine = await azure_roles_at(sub.group(1), facts.get("object_id", ""), p) if sub else None
        a.update(kind="azure_rbac_missing",
                 reason=(f"Azure refused '{action or method.upper() + ' ' + path}'" + (f" on {scope}" if scope else "") + "."),
                 missing=f"the Azure role {role}" + (f" on subscription {sub.group(1)}" if sub else ""),
                 your_azure_roles_on_subscription=mine if mine is not None else "could not be read",
                 who_can_fix=FIX_AZURE, how_to_fix=f"Ask for {role} on that scope. Azure role changes can take a few minutes to apply; sign in again afterwards.")
    elif service == "powerplatform" and "insufficientdelegatedpermissions" in low:
        ns = path.strip("/").split("/")[0] if path else ""
        a.update(kind="app_permission_missing", reason=f"The app is missing the Power Platform API permission for the '{ns}' area.",
                 missing=f"a Power Platform API delegated permission for '{ns}' (for example CopilotStudio.AdminActions.Invoke or Licensing.Allocations.ReadWrite)",
                 who_can_fix="Whoever manages the app registration; these permissions are user-consentable, so you can consent yourself after they are added.",
                 how_to_fix="Add the permission (scripts/permissions.json, core tier), then sign out of the Copilot Studio connection and sign in again.")
    elif service in ("powerplatform", "bap", "powerapps", "flow"):
        roles = need.roles if need and need.roles else PP_ADMIN
        a.update(kind="role_missing", reason=f"{_service_title(service)} refused: this needs a Power Platform admin role.",
                 missing="one of these Entra roles: " + ", ".join(roles), needed_roles=roles, who_can_fix=FIX_ENTRA_ROLE,
                 how_to_fix="Ask for Power Platform Administrator (time-bound through PIM is ideal), then sign in again.")
    elif service == "dataverse":
        host = urlparse(url).hostname or "the environment"
        priv = re.search(r"\b(prv[A-Za-z]+)\b", text)
        if "not a member" in low or "user is disabled" in low or "does not exist in" in low:
            a.update(kind="environment_membership", reason=f"You are not a user in the Dataverse environment {host}.",
                     missing=f"a user record with a security role in {host}", who_can_fix=FIX_DATAVERSE,
                     how_to_fix="Power Platform admin centre > the environment > Settings > Users > add yourself with a security role (System Administrator for admin work).")
        else:
            a.update(kind="dataverse_privilege_missing",
                     reason=f"Your security role in {host} does not include the privilege needed" + (f" ({priv.group(1)})." if priv else "."),
                     missing=(priv.group(1) if priv else "a Dataverse privilege") + f" in {host}", who_can_fix=FIX_DATAVERSE,
                     how_to_fix="Ask for a security role that includes it (System Administrator or System Customizer for agent work).")
    elif service in ("powerbi", "fabric"):
        a.update(kind="role_missing", reason=f"{_service_title(service)} admin APIs refused your account.", missing="the Fabric Administrator role (and Tenant.Read.All consented)",
                 needed_roles=["Fabric Administrator", "Global Administrator"], who_can_fix=FIX_ENTRA_ROLE, how_to_fix="Ask for Fabric Administrator, then sign in again.")
    else:
        a.update(kind="access_denied", reason=f"{_service_title(service)} returned {status}: {evidence.get('message', '')}",
                 missing="permission for this action", who_can_fix="Your administrator for that service.", how_to_fix="Check the role or permission the service requires.")

    # Confirm against the caller's actual roles where roles matter
    if a.get("kind") in ("role_missing",) or (need and need.roles and a.get("kind") not in ("server_policy", "server_credential", "consent_missing", "app_permission_missing")):
        facts = await caller_facts(p)
        if facts.get("roles") is not None:
            a["your_roles"] = facts["roles"] or ["(no admin roles active)"]
            wanted = [r.split(" (")[0] for r in (a.get("needed_roles") or (need.roles if need else []))]
            if wanted:
                a["confirmed"] = not any(r in facts["roles"] for r in wanted)
                if not a["confirmed"]:
                    a["note"] = ("You do hold a role that normally allows this, so the refusal is more specific: a scoped or custom role, a PIM role that has expired, "
                                 "or a service-level setting. The evidence below shows exactly what the service said.")
    if need and (need.azure or need.other):
        a["requirement"] = "; ".join(x for x in (", ".join(need.roles) if need.roles else "", need.azure, need.other) if x)
    a["evidence"] = evidence
    a["user_message"] = _user_message(a)
    return a


def roles_text(roles: list[str] | None, limit: int = 6) -> str:
    if not roles:
        return "no admin roles active"
    shown = ", ".join(roles[:limit])
    return shown + (f" and {len(roles) - limit} more" if len(roles) > limit else "")


def _user_message(a: dict[str, Any]) -> str:
    what = a.get("what") or "this"
    if a.get("kind") == "server_policy":
        return (f"Sorry, {what} is switched off on this server: its administrator has set "
                f"{a.get('reason', '').split(': ', 1)[-1].rstrip('.')}. Nothing is wrong with your account. {a.get('how_to_fix', '')}")
    if a.get("kind") == "server_credential":
        return (f"Sorry, I can't do {what} right now because the Mega Admin MCP server itself can't sign in to Microsoft Entra. "
                f"Nothing is wrong with your account or roles. {a.get('who_can_fix', '')} {a.get('how_to_fix', '')}").strip()
    mine = a.get("your_roles")
    has = f" Your account currently has: {roles_text(mine)}." if isinstance(mine, list) and mine else ""
    return (f"Sorry, you don't have access to {what}. {a.get('reason', '')} What's missing: {a.get('missing', '')}.{has} "
            f"{a.get('who_can_fix', '')}").strip()


async def enrich(tool: str, result: Any) -> Any:
    """Add access_denied / access_partial and a confirmed explanation to a tool result. Never raises."""
    if tool in NO_ENRICH or not isinstance(result, dict):
        return result
    try:
        problems = find_problems(result)
        if not problems:
            return result
        primary = problems[0]
        explanation = await classify(primary, tool)
        partial = bool(result.get("ok")) and result.get("status") not in (401, 403, "blocked")
        out = dict(result)
        out["access_partial" if partial else "access_denied"] = True
        if partial:
            explanation["user_message"] = ("Part of this answer is missing because of access. " + explanation["user_message"]).strip()
            explanation["affected_calls"] = len(problems)
        out["access"] = explanation
        return out
    except Exception:  # noqa: BLE001
        log.exception("access enrichment failed for %s", tool)
        return result


async def precheck_write(service: str, method: str, url: str, principal: Principal | None = None) -> dict[str, Any] | None:
    """Before an admin confirms a write: will it probably be refused? Graph permission and Power Platform role checks only."""
    p = principal or current_principal()
    path = urlparse(url).path
    try:
        if service == "graph":
            from .runbook.healing import graph_permissions_for
            needed = (graph_permissions_for(method, path) or {}).get("del") or []
            granted = await granted_scopes("graph", p)
            if needed and granted is not None and "Directory.AccessAsUser.All" not in granted and not any(s in granted for s in needed):
                return {"likely_denied": True, "kind": "app_permission_missing",
                        "message": f"Heads up before you confirm: the app has none of the Graph permissions this needs ({', '.join(needed[:4])}), so Microsoft will refuse it. {FIX_CONSENT}"}
            roles = _graph_roles_for(method, path)
            facts = await caller_facts(p)
            if roles and facts.get("roles") is not None and not any(r in facts["roles"] for r in roles) and "Global Administrator" not in facts["roles"]:
                return {"likely_denied": True, "kind": "role_missing", "your_roles": facts["roles"],
                        "message": f"Heads up before you confirm: this usually needs {', '.join(roles[:3])}, and your account has {roles_text(facts['roles'])}. {FIX_ENTRA_ROLE}"}
        elif service in ("powerplatform", "bap", "powerapps", "flow"):
            facts = await caller_facts(p)
            if facts.get("roles") is not None and not any(r in facts["roles"] for r in PP_ADMIN):
                return {"likely_denied": True, "kind": "role_missing", "your_roles": facts["roles"],
                        "message": f"Heads up before you confirm: this needs Power Platform Administrator (or Dynamics 365 / Global Administrator), and your account has {roles_text(facts['roles'])}. {FIX_ENTRA_ROLE}"}
    except Exception:  # noqa: BLE001
        log.exception("write precheck failed")
    return None
