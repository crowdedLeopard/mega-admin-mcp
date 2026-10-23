"""Registry of the Microsoft REST surfaces the runbook can call.

Each service knows its base URL, the token scope to request, which hosts it
owns (so absolute URLs and next links can be routed safely) and how API
versioning works for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class Service:
    name: str
    title: str
    base_url: str
    scope: str | None
    hosts: tuple[str, ...] = ()
    versioning: str = "none"  # none | path (graph) | query (arm, powerplatform)
    default_version: str = ""
    api_version_param: str = "api-version"
    notes: str = ""
    dynamic_host: bool = False  # dataverse: per-organisation URL

    def owns_host(self, host: str) -> bool:
        host = host.lower()
        if self.dynamic_host:
            return host.endswith(".dynamics.com") or host.endswith(".crm.dynamics.com")
        return host in self.hosts or host.endswith(tuple("." + h for h in self.hosts))


SERVICES: dict[str, Service] = {
    s.name: s
    for s in [
        Service(
            "graph", "Microsoft Graph", "https://graph.microsoft.com", "https://graph.microsoft.com/.default",
            ("graph.microsoft.com",), versioning="path", default_version="v1.0",
            notes="Entra ID, Microsoft 365 admin, Intune, Teams, SharePoint, reports, Copilot usage, licences.",
        ),
        Service(
            "arm", "Azure Resource Manager", "https://management.azure.com", "https://management.azure.com/.default",
            ("management.azure.com",), versioning="query", default_version="",
            notes="All Azure resource providers: Cost Management, Azure OpenAI / Foundry, Monitor metrics, quotas, budgets, RBAC.",
        ),
        Service(
            "powerplatform", "Power Platform API", "https://api.powerplatform.com", "https://api.powerplatform.com/.default",
            ("api.powerplatform.com",), versioning="query", default_version="2024-10-01",
            notes="Licensing (Copilot Credits, billing plans, FinOps), environment management, Copilot Studio admin, governance.",
        ),
        Service(
            "bap", "Business Application Platform admin API", "https://api.bap.microsoft.com", "https://service.powerapps.com/.default",
            ("api.bap.microsoft.com",), versioning="query", default_version="2021-04-01",
            notes="Legacy Power Platform admin API: environments, tenant settings, DLP policies.",
        ),
        Service(
            "powerapps", "Power Apps admin API", "https://api.powerapps.com", "https://service.powerapps.com/.default",
            ("api.powerapps.com",), versioning="query", default_version="2016-11-01",
            notes="Apps, connections and connectors per environment (admin scope).",
        ),
        Service(
            "flow", "Power Automate admin API", "https://api.flow.microsoft.com", "https://service.flow.microsoft.com/.default",
            ("api.flow.microsoft.com",), versioning="query", default_version="2016-11-01",
            notes="Cloud flows per environment (admin scope).",
        ),
        Service(
            "dataverse", "Dataverse Web API", "", None, (), versioning="none",
            notes="Per-environment OData API: bots (Copilot Studio agents), solutions, users, roles, flows, settings. Needs the organisation URL.",
            dynamic_host=True,
        ),
        Service(
            "powerbi", "Power BI REST API", "https://api.powerbi.com", "https://analysis.windows.net/powerbi/api/.default",
            ("api.powerbi.com",), versioning="none",
            notes="Power BI admin: workspaces, capacities, activity events, users.",
        ),
        Service(
            "fabric", "Microsoft Fabric REST API", "https://api.fabric.microsoft.com", "https://api.fabric.microsoft.com/.default",
            ("api.fabric.microsoft.com",), versioning="none",
            notes="Fabric admin: tenant settings, capacities, workspaces, domains, items.",
        ),
        Service(
            "o365mgmt", "Office 365 Management APIs", "https://manage.office.com", "https://manage.office.com/.default",
            ("manage.office.com",), versioning="none",
            notes="Audit activity feed and service communications. Application permissions only.",
        ),
        Service(
            "devops", "Azure DevOps REST API", "https://dev.azure.com", "499b84ac-1321-427f-aa17-267ca6975798/.default",
            ("dev.azure.com", "vssps.dev.azure.com"), versioning="query", default_version="7.1",
            notes="Optional. Organisation, project and pipeline administration.",
        ),
        Service(
            "retailprices", "Azure Retail Prices API", "https://prices.azure.com", None,
            ("prices.azure.com",), versioning="none",
            notes="Public, unauthenticated list prices for every Azure meter including Azure OpenAI tokens.",
        ),
    ]
}

ALIASES = {
    "msgraph": "graph", "azure": "arm", "management": "arm", "pp": "powerplatform", "ppapi": "powerplatform",
    "power-platform": "powerplatform", "dv": "dataverse", "crm": "dataverse", "pbi": "powerbi", "prices": "retailprices",
    "o365": "o365mgmt", "ado": "devops",
}


def resolve_service(name: str) -> Service:
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in SERVICES:
        raise KeyError(f"Unknown service '{name}'. Known services: {', '.join(sorted(SERVICES))}")
    return SERVICES[key]


def service_for_url(url: str) -> Service | None:
    host = urlparse(url).hostname or ""
    for s in SERVICES.values():
        if s.owns_host(host):
            return s
    return None


def scope_for(service: Service, url: str | None = None) -> str | None:
    if service.name == "dataverse":
        if not url:
            raise ValueError("Dataverse calls need the organisation URL (https://<org>.crm.dynamics.com)")
        p = urlparse(url)
        return f"{p.scheme}://{p.hostname}/.default"
    return service.scope
