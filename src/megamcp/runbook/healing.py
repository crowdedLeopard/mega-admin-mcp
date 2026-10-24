"""Self-healing helpers: turn raw API failures into recoveries or precise advice.

Nothing here is magic. Each helper looks at a failed response and either
returns a corrected request to retry (api-version drift, Graph beta fallback)
or returns human-readable hints and the catalog operations that are closest to
what the caller tried.
"""
from __future__ import annotations

import re
from typing import Any

from ..catalog.store import catalog

_SUPPORTED_VERSIONS = re.compile(r"supported api-versions are '([^']+)'", re.IGNORECASE)
_SUPPORTED_VERSIONS2 = re.compile(r"api-version[^'\"]*?(?:are|is)[:\s]*['\"]?([0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?(?:\s*,\s*[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?)*)", re.IGNORECASE)


def _error_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        import json

        try:
            return json.dumps(body)[:4000]
        except Exception:  # noqa: BLE001
            return str(body)[:4000]
    return str(body)[:4000]


_VERSION_LIST = re.compile(r"[Ss]upported (?:API |api-)?[Vv]ersions? (?:are|is)[:\s]+([0-9]{4}-[0-9]{2}-[0-9]{2}(?:-[a-z]+)?(?:\s*,\s*[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-[a-z]+)?)*)")


def _order(versions: list[str]) -> list[str]:
    stable = sorted([v for v in versions if "preview" not in v.lower()], reverse=True)
    preview = sorted([v for v in versions if "preview" in v.lower()], reverse=True)
    return stable + preview


def supported_versions_from_text(text: str) -> list[str]:
    """Parse 'Supported API versions are: a, b, c' style messages (ARM and Power Platform)."""
    m = _SUPPORTED_VERSIONS.search(text) or _VERSION_LIST.search(text) or _SUPPORTED_VERSIONS2.search(text)
    if not m:
        return []
    return _order([v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()])


def arm_supported_versions(body: Any) -> list[str]:
    return supported_versions_from_text(_error_text(body))


def graph_beta_candidate(method: str, path: str) -> bool:
    """True when a v1.0 path exists only in the beta catalog."""
    clean = re.sub(r"^/v1\.0", "", path.split("?")[0])
    v1 = catalog.match_path("graph", method.upper(), clean, version="v1.0")
    beta = catalog.match_path("graph", method.upper(), clean, version="beta")
    return bool(beta) and not v1


def graph_permissions_for(method: str, path: str) -> dict[str, list[str]]:
    clean = re.sub(r"^/(v1\.0|beta)", "", path.split("?")[0])
    for op in catalog.match_path("graph", method.upper(), clean):
        perms = op.get("perms") or {}
        if perms:
            return perms
    return {}


def explain(service: str, status: int, body: Any, method: str, url: str, path: str) -> dict[str, Any]:
    """Return hints, likely causes and suggested catalog operations for a failed call."""
    text = _error_text(body)
    hints: list[str] = []
    suggestions: list[dict[str, Any]] = []
    code = ""
    try:
        if isinstance(body, dict):
            err = body.get("error") or body
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("errorCode") or "")
            elif isinstance(err, str):
                code = err
    except Exception:  # noqa: BLE001
        pass

    if status == 401:
        hints.append("The downstream service rejected the token. The token was refreshed and retried once already.")
        if service == "dataverse" and "not a member" in text.lower():
            hints.append("The signed-in admin is not a user in this Dataverse environment. Add them (or give them a security role) in the Power Platform admin centre.")
        elif service == "dataverse":
            hints.append("Check the organisation URL is exactly the environment URL (https://<org>.crm.dynamics.com) and that the app registration has Dynamics CRM user_impersonation.")
        else:
            hints.append("Delegated mode: make sure the app registration has a permission for this API and it has been consented. App mode: make sure the identity has an RBAC role or application permission.")
    elif status == 403:
        hints.append("Authenticated but not authorised.")
        if service == "graph":
            perms = graph_permissions_for(method, path)
            if perms:
                hints.append(f"Graph permissions for this operation: delegated {perms.get('del', [])} / application {perms.get('app', [])}. Add the delegated one to the app registration and grant admin consent.")
            else:
                hints.append("Check the Graph permission for this operation in the catalog (describe_operation) and that the admin holds a directory role that allows it.")
            if "S2SUnauthorized" in text or "Invalid permission" in text:
                hints.append("Reports endpoints need Reports.Read.All consented and a reporting role (Reports Reader, Global Reader, AI Administrator or similar).")
        elif service == "powerplatform":
            if "InsufficientDelegatedPermissions" in text:
                ns = path.strip("/").split("/")[0] if path else ""
                hints.append(f"The app registration is missing the Power Platform API delegated scope for the '{ns}' namespace (for example CopilotStudio.AdminActions.Invoke, Licensing.Allocations.ReadWrite). Add it in Entra (API permissions > Power Platform API), re-consent, then sign in again in Copilot Studio.")
            else:
                hints.append("Power Platform API needs the admin to hold Power Platform Administrator (or Dynamics 365 / Global Administrator) and the app to have the matching Licensing.* / EnvironmentManagement.* delegated scope consented.")
        elif service == "arm":
            hints.append("Azure RBAC: the caller needs at least Reader (Cost Management Reader for cost queries, Monitoring Reader for metrics, Contributor to change resources) on the scope.")
        elif service in ("bap", "powerapps", "flow"):
            hints.append("These admin endpoints need Power Platform Administrator or Global Administrator.")
        elif service in ("powerbi", "fabric"):
            hints.append("Admin endpoints need Fabric Administrator (Power BI admin) and Tenant.Read.All consented.")
    elif status == 404:
        if service == "arm":
            versions = arm_supported_versions(body)
            if versions:
                hints.append(f"The api-version is not supported for this resource. Supported: {', '.join(versions[:8])}. The runbook retries automatically with the newest supported version when ARM_AUTO_API_VERSION is on.")
            elif "ResourceNotFound" in code or "NotFound" in code:
                hints.append("The resource does not exist in this subscription, or the name / resource group is wrong. Use Azure Resource Graph (invoke arm POST /providers/Microsoft.ResourceGraph/resources) to find it.")
            elif "NoRegisteredProviderFound" in code or "InvalidResourceType" in code:
                hints.append("The resource provider is not registered in this subscription or the resource type is misspelt.")
        elif service == "graph":
            if graph_beta_candidate(method, path):
                hints.append("This path exists only in Graph beta. Call it with /beta instead of /v1.0 (the runbook does this automatically for GET when GRAPH_AUTO_BETA_FALLBACK is on).")
            hints.append("Check the path in the catalog; Graph paths are case-sensitive for function names and need the exact segment.")
        elif service == "powerplatform":
            hints.append("Either the environment / resource id is wrong, or this operation needs a different api-version (try 2022-03-01-preview or 2024-10-01). Use describe_operation for the exact shape.")
        suggestions = catalog.similar_paths(service, path.split("?")[0])
    elif status == 400:
        if service == "arm":
            versions = arm_supported_versions(body)
            if versions:
                hints.append(f"Unsupported api-version. Supported: {', '.join(versions[:8])}.")
        if "api version" in text.lower().replace("-", " ") and service == "powerplatform":
            sup = supported_versions_from_text(text)
            hints.append(f"Power Platform API rejected the api-version. Supported: {', '.join(sup) if sup else '2024-10-01 (most operations), 2022-03-01-preview (older ones)'}.")
        if "Invalid filter" in text:
            hints.append("The $filter expression is not accepted by this endpoint. Remove it or use the exact property names from describe_operation.")
        if service == "graph" and "Resource not found for the segment" in text:
            hints.append("A path segment is not a valid Graph resource. Check spelling and whether it is a beta-only segment.")
            suggestions = catalog.similar_paths("graph", path.split("?")[0])
    elif status == 429:
        hints.append("Throttled. The runbook honours Retry-After while it fits in the tool's time budget (TOOL_TIME_BUDGET_SECONDS); when it does not, the call returns at once with retry_after_seconds. Call again after that, reduce page size, add $select, or spread calls out.")
    elif status >= 500:
        hints.append("The service had an internal error. The runbook retried. If it persists, check the Microsoft 365 / Azure service health (search_operations 'service health').")

    return {"error_code": code, "hints": hints, "suggested_operations": suggestions}
