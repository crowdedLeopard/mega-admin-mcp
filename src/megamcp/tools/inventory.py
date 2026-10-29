"""Tenant-wide inventory: every agent, app, flow, environment and connector in one query.

Power Platform inventory keeps a `PowerPlatformResources` table in Azure Resource
Graph with every Copilot Studio agent (including Agent Builder agents), app,
flow, Power Pages site, environment and connector in the tenant, refreshed within
about 15 minutes of a change. It also carries undocumented usage records with
the last day each agent, app or flow was used.

One KQL query answers "how many agents were created in the last seven days" for
the whole tenant in under a second, where the per-environment route needs one
Dataverse call per environment (802 in the dev tenant).

Two routes, tried in order:
1. Azure Resource Graph REST with raw KQL (ARM user_impersonation, core tier).
2. The Power Platform API inventory endpoint, which takes the same query as
   structured clauses. Used automatically when Resource Graph is refused.

Who sees what is decided by Entra roles: Global Administrator, Power Platform
Administrator, Dynamics 365 Administrator and Global Reader see everything; AI
Administrator and AI Reader see agents, agentic apps, agent flows and
environments only.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..runbook.invoke import runbook
from .common import READ, err

ARG_API = "2022-10-01"
PP_INVENTORY_API = "2024-10-01"
TABLE = "PowerPlatformResources"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?)?$")
MAX_OUTPUT_CHARS = 45_000  # keep tool results comfortably inside what the orchestrator will read

AGENT = "microsoft.copilotstudio/agents"
BUILDER_EXPR = "(tostring(properties.createdIn) contains 'Lite' or tostring(properties.createdIn) contains 'Agent Builder')"
BOOL_FIELDS = {"shared_with_entire_tenant", "quarantined", "managed_solution", "managed", "deprecated", "isManagedEnvironment"}
TYPE_NAMES = {
    "microsoft.copilotstudio/agents": "Copilot Studio agent",
    "microsoft.powerapps/canvasapps": "Canvas app",
    "microsoft.powerapps/modeldrivenapps": "Model-driven app",
    "microsoft.powerapps/codeapps": "Code app",
    "microsoft.powerapps/apps": "Managed or App Builder app",
    "microsoft.powerautomate/cloudflows": "Cloud flow",
    "microsoft.powerautomate/agentflows": "Agent flow",
    "microsoft.powerautomate/m365agentflows": "Workflow agent flow",
    "microsoft.powerpages/sites": "Power Pages site",
    "microsoft.powerplatform/environments": "Environment",
    "microsoft.powerplatform/environmentgroups": "Environment group",
    "microsoft.powerplatformconnector/connectors": "Connector",
}
_APPS = ["microsoft.powerapps/canvasapps", "microsoft.powerapps/modeldrivenapps", "microsoft.powerapps/codeapps", "microsoft.powerapps/apps"]
_FLOWS = ["microsoft.powerautomate/cloudflows", "microsoft.powerautomate/agentflows", "microsoft.powerautomate/m365agentflows"]
TYPE_GROUPS: dict[str, list[str]] = {
    "agents": [AGENT],
    "apps": _APPS,
    "canvas_apps": ["microsoft.powerapps/canvasapps"],
    "model_driven_apps": ["microsoft.powerapps/modeldrivenapps"],
    "code_apps": ["microsoft.powerapps/codeapps"],
    "managed_apps": ["microsoft.powerapps/apps"],
    "flows": _FLOWS,
    "cloud_flows": ["microsoft.powerautomate/cloudflows"],
    "agent_flows": ["microsoft.powerautomate/agentflows"],
    "workflows": ["microsoft.powerautomate/m365agentflows"],
    "power_pages": ["microsoft.powerpages/sites"],
    "environments": ["microsoft.powerplatform/environments"],
    "environment_groups": ["microsoft.powerplatform/environmentgroups"],
    "connectors": ["microsoft.powerplatformconnector/connectors"],
    "all": [AGENT, *_APPS, *_FLOWS, "microsoft.powerpages/sites"],
}

NOTES_INVENTORY = [
    "Source: Power Platform inventory (whole tenant, one query). Changes appear within about 15-20 minutes.",
    "last_used is the last day with recorded usage from the inventory usage records; empty means no usage recorded.",
]


# ---------------------------------------------------------------------------
# query builder
# ---------------------------------------------------------------------------
def kql_str(value: str) -> str:
    """Quote user text as a KQL string literal (no injection: quotes, backslashes and control characters are neutralised)."""
    v = re.sub(r"[\x00-\x1f]", " ", str(value))
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


def kql_date(value: str, name: str) -> str:
    v = (value or "").strip()
    if not DATE.match(v):
        raise ValueError(f"{name} must be a date like 2026-10-22 or 2026-10-22T10:45:00Z (got {value!r})")
    return f"datetime({v})"


class Query:
    """A pipeline over PowerPlatformResources rendered to KQL (Azure Resource Graph) or to the
    structured clauses of the Power Platform inventory API (fallback route)."""

    _ENV_RIGHT = ("PowerPlatformResources | where type == 'microsoft.powerplatform/environments' "
                  "| project envKey = tolower(name), environmentName = tostring(properties.displayName), "
                  "environmentType = tostring(properties.environmentType), isManagedEnvironment = tobool(properties.isManaged)")
    _USAGE_KEY = "tostring(split(tolower(id), '/providers/microsoft.powerplatformusage')[0])"

    def __init__(self) -> None:
        self.steps: list[tuple[Any, ...]] = []
        self.kql_only = False

    def where(self, expr: str) -> "Query":
        self.steps.append(("where", expr))
        return self

    def extend(self, name: str, expr: str) -> "Query":
        self.steps.append(("extend", name, expr))
        return self

    def join_environments(self) -> "Query":
        self.steps.append(("join_env",))
        return self

    def join_usage(self) -> "Query":
        self.steps.append(("join_usage",))
        return self

    def project(self, *fields: str) -> "Query":
        self.steps.append(("project", list(fields)))
        return self

    def order(self, field: str, direction: str = "desc") -> "Query":
        self.steps.append(("order", field, direction))
        return self

    def take(self, n: int) -> "Query":
        self.steps.append(("take", int(n)))
        return self

    def summarize_count(self, name: str, by: list[str]) -> "Query":
        self.steps.append(("summarize", name, list(by)))
        return self

    def raw(self, kql_text: str) -> "Query":
        """KQL with no clause equivalent (countif, dcount, mv-expand): Resource Graph only."""
        self.steps.append(("raw", kql_text))
        self.kql_only = True
        return self

    def copy(self) -> "Query":
        q = Query()
        q.steps = list(self.steps)
        q.kql_only = self.kql_only
        return q

    # -- renderers -----------------------------------------------------------
    def kql(self) -> str:
        lines = [TABLE]
        for s in self.steps:
            kind = s[0]
            if kind == "where":
                lines.append(f"| where {s[1]}")
            elif kind == "extend":
                lines.append(f"| extend {s[1]} = {s[2]}")
            elif kind == "join_env":
                lines.append("| extend envKey = tolower(tostring(properties.environmentId))")
                lines.append(f"| join kind=leftouter ({self._ENV_RIGHT}) on envKey")
            elif kind == "join_usage":
                lines.append("| extend usageKey = tolower(id)")
                lines.append("| join kind=leftouter (PowerPlatformResources | where type == 'microsoft.powerplatformusage/usagerecords' "
                             f"| extend usageKey = {self._USAGE_KEY}, lastUsed = todatetime(properties.lastUsed) "
                             "| summarize lastUsed = max(lastUsed) by usageKey) on usageKey")
            elif kind == "project":
                lines.append("| project " + ", ".join(s[1]))
            elif kind == "order":
                lines.append(f"| order by {s[1]} {s[2]}")
            elif kind == "take":
                lines.append(f"| take {s[1]}")
            elif kind == "summarize":
                lines.append(f"| summarize {s[1]} = count()" + (" by " + ", ".join(s[2]) if s[2] else ""))
            elif kind == "raw":
                lines.append(s[1])
        return "\n".join(lines)

    def clauses(self) -> list[dict[str, Any]]:
        if self.kql_only:
            raise ValueError("this query uses KQL the inventory API clauses cannot express")
        out: list[dict[str, Any]] = []
        n = 0
        for s in self.steps:
            kind = s[0]
            if kind == "where":
                # The where clause takes field / operator / values; any boolean expression fits as a computed flag.
                n += 1
                out.append({"$type": "extend", "FieldName": f"_m{n}", "Expression": f"({s[1]})"})
                out.append({"$type": "where", "FieldName": f"_m{n}", "Operator": "==", "Values": ["true"]})
            elif kind == "extend":
                out.append({"$type": "extend", "FieldName": s[1], "Expression": s[2]})
            elif kind == "join_env":
                out.append({"$type": "extend", "FieldName": "envKey", "Expression": "tolower(tostring(properties.environmentId))"})
                out.append({"$type": "join", "JoinKind": "leftouter", "LeftColumnName": "envKey", "RightColumnName": "envKey",
                            "RightTable": {"TableName": TABLE, "Clauses": [
                                {"$type": "where", "FieldName": "type", "Operator": "==", "Values": ["'microsoft.powerplatform/environments'"]},
                                {"$type": "project", "FieldList": ["envKey = tolower(name)", "environmentName = tostring(properties.displayName)",
                                                                   "environmentType = tostring(properties.environmentType)",
                                                                   "isManagedEnvironment = tobool(properties.isManaged)"]}]}})
            elif kind == "join_usage":
                out.append({"$type": "extend", "FieldName": "usageKey", "Expression": "tolower(id)"})
                out.append({"$type": "join", "JoinKind": "leftouter", "LeftColumnName": "usageKey", "RightColumnName": "usageKey",
                            "RightTable": {"TableName": TABLE, "Clauses": [
                                {"$type": "where", "FieldName": "type", "Operator": "==", "Values": ["'microsoft.powerplatformusage/usagerecords'"]},
                                {"$type": "extend", "FieldName": "usageKey", "Expression": self._USAGE_KEY},
                                {"$type": "extend", "FieldName": "lastUsed", "Expression": "todatetime(properties.lastUsed)"},
                                {"$type": "summarize", "SummarizeClauseExpression": {"OperatorName": "argmax", "OperatorFieldName": "lastUsed", "FieldList": ["usageKey"]}},
                                {"$type": "project", "FieldList": ["usageKey", "lastUsed"]}]}})
            elif kind == "project":
                out.append({"$type": "project", "FieldList": s[1]})
            elif kind == "order":
                out.append({"$type": "orderby", "FieldNamesAscDesc": {s[1]: s[2]}})
            elif kind == "take":
                out.append({"$type": "take", "TakeCount": s[1]})
            elif kind == "summarize":
                out.append({"$type": "summarize", "SummarizeClauseExpression": {"OperatorName": "count", "OperatorFieldName": s[1], "FieldList": s[2]}})
        return out


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
ROLE_HINT = ("Power Platform inventory needs one of these Entra roles: Global Administrator, Power Platform Administrator, "
             "Dynamics 365 Administrator or Global Reader (AI Administrator / AI Reader see agents, agentic apps, agent flows and environments only). "
             "Azure Resource Graph also needs the Azure Service Management permission consented; if Conditional Access demands MFA for Azure "
             "Resource Manager, sign in again.")


def _split_token(token: str) -> tuple[str, str]:
    t = (token or "").strip()
    if t.startswith("arg:"):
        return "arg", t[4:]
    if t.startswith("pp:"):
        return "pp", t[3:]
    return ("arg", t) if t else ("", "")


async def run_query(q: Query, top: int = 100, skip_token: str = "") -> dict[str, Any]:
    """Run a Query on Resource Graph, falling back to the Power Platform inventory API. Never raises."""
    top = max(1, min(int(top), 1000))
    route, token = _split_token(skip_token)
    attempts: list[dict[str, Any]] = []
    if route in ("", "arg"):
        options: dict[str, Any] = {"resultFormat": "objectArray", "$top": top}
        if token:
            options["$skipToken"] = token
        r = await runbook.call("arm", "POST", "/providers/Microsoft.ResourceGraph/resources", body={"query": q.kql(), "options": options},
                               api_version=ARG_API, max_chars=8_000_000)
        if r.get("ok"):
            b = r.get("body") or {}
            nxt = b.get("$skipToken") or b.get("skipToken")
            return {"ok": True, "source": "azure_resource_graph", "rows": b.get("data") or [], "total": b.get("totalRecords"),
                    "skip_token": f"arg:{nxt}" if nxt else ""}
        attempts.append({"route": "azure_resource_graph", "service": "arm", "url": r.get("url", ""), "status": r.get("status"),
                         "message": r.get("message") or r.get("error"), **({k: r[k] for k in ("error", "aad_error", "description", "suberror", "scope", "claims_challenge", "fix", "admin_consent_url") if k in r})})
    if not q.kql_only and route in ("", "pp", "arg"):
        options = {"Top": top}
        if token and route == "pp":
            options["SkipToken"] = token
        r = await runbook.call("powerplatform", "POST", "/resourcequery/resources/query",
                               body={"TableName": TABLE, "Clauses": q.clauses(), "Options": options},
                               api_version=PP_INVENTORY_API, max_chars=8_000_000)
        if r.get("ok"):
            b = r.get("body") or {}
            nxt = b.get("skipToken") or b.get("$skipToken")
            return {"ok": True, "source": "power_platform_inventory_api", "rows": b.get("data") or [], "total": b.get("totalRecords"),
                    "skip_token": f"pp:{nxt}" if nxt else ""}
        attempts.append({"route": "power_platform_inventory_api", "service": "powerplatform", "url": r.get("url", ""), "status": r.get("status"),
                         "message": r.get("message") or r.get("error"), **({k: r[k] for k in ("error", "aad_error", "description", "suberror", "scope", "claims_challenge", "fix", "admin_consent_url") if k in r})})
    return {"ok": False, "error": "inventory_query_failed", "attempts": attempts, "kql": q.kql(), "hints": [ROLE_HINT]}


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if v in (None, "", [], {}) or k.startswith(("envKey", "usageKey", "_m")):
            continue
        if k in BOOL_FIELDS and v in (0, 1):
            v = bool(v)
        elif k == "trigger" and isinstance(v, str) and v.startswith("{"):
            try:
                t = json.loads(v)
                conn = t.get("connectorDisplayName") or t.get("connectorId") or "built-in"
                op = t.get("operationDisplayName") or t.get("operationId") or ""
                v = f"{conn}: {op}" if op else conn
            except ValueError:
                pass
        out[k] = v
    return out


def _fit(rows: list[dict[str, Any]], budget_chars: int = MAX_OUTPUT_CHARS) -> tuple[list[dict[str, Any]], int]:
    """Keep the leading rows that fit in budget_chars of JSON; return (rows, trimmed_count)."""
    used, kept = 0, []
    for r in rows:
        size = len(json.dumps(r, default=str)) + 2
        if used + size > budget_chars and kept:
            break
        kept.append(r)
        used += size
    return kept, len(rows) - len(kept)


async def resolve_users(ids: set[str] | list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """Object id -> {name, upn} for users, via Graph (User.ReadBasic.All). Service principals and deleted users stay unresolved."""
    want = sorted({i.lower() for i in ids if i and GUID.match(i) and i != ZERO_GUID})[:450]
    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(6)

    async def chunk(c: list[str]) -> None:
        flt = "id in (" + ",".join(f"'{i}'" for i in c) + ")"
        async with sem:
            r = await runbook.call("graph", "GET", "/v1.0/users", query={"$filter": flt, "$select": "id,displayName,userPrincipalName,mail"},
                                   max_chars=300_000, retry_wait_budget=5)
        if not r.get("ok"):
            errors.append(str(r.get("message") or r.get("status")))
            return
        for u in (r.get("body") or {}).get("value", []):
            out[str(u.get("id", "")).lower()] = {"name": u.get("displayName"), "upn": u.get("userPrincipalName") or u.get("mail")}

    await asyncio.gather(*(chunk(want[i:i + 15]) for i in range(0, len(want), 15)))
    return out, (errors[0] if errors else "")


async def owner_to_id(owner: str) -> tuple[str, dict[str, Any] | None]:
    """Accept an object id, UPN / email or display name; return (object id, error)."""
    o = (owner or "").strip()
    if not o:
        return "", None
    if GUID.match(o):
        return o.lower(), None
    if "@" in o:
        r = await runbook.call("graph", "GET", f"/v1.0/users/{quote(o)}", query={"$select": "id,displayName,userPrincipalName"}, max_chars=20_000)
        if r.get("ok"):
            return str((r.get("body") or {}).get("id", "")).lower(), None
        return "", err(f"No user found for {o}: {r.get('message')}")
    safe = o.replace("'", "''")
    r = await runbook.call("graph", "GET", "/v1.0/users", query={"$filter": f"startswith(displayName,'{safe}') or startswith(userPrincipalName,'{safe}')",
                                                                  "$select": "id,displayName,userPrincipalName", "$top": "6"}, max_chars=50_000)
    users = (r.get("body") or {}).get("value", []) if r.get("ok") else []
    if len(users) == 1:
        return str(users[0]["id"]).lower(), None
    return "", err(f"owner '{o}' matched {len(users)} users; pass the object id or UPN", candidates=[{"id": u.get("id"), "name": u.get("displayName"), "upn": u.get("userPrincipalName")} for u in users])


def _tri(value: str, name: str) -> str:
    v = (value or "any").strip().lower()
    if v in ("", "any", "all", "either"):
        return "any"
    if v in ("yes", "true", "y", "1", "only"):
        return "yes"
    if v in ("no", "false", "n", "0", "none", "exclude"):
        return "no"
    raise ValueError(f"{name} must be any, yes or no (got {value!r})")


def _date_filters(q: Query, created_within_days: int, created_after: str, created_before: str, modified_within_days: int = 0) -> list[str]:
    applied = []
    if int(created_within_days or 0) > 0:
        q.where(f"todatetime(properties.createdAt) >= ago({int(created_within_days)}d)")
        applied.append(f"created in the last {int(created_within_days)} days")
    if created_after:
        q.where(f"todatetime(properties.createdAt) >= {kql_date(created_after, 'created_after')}")
        applied.append(f"created on or after {created_after}")
    if created_before:
        q.where(f"todatetime(properties.createdAt) < {kql_date(created_before, 'created_before')}")
        applied.append(f"created before {created_before}")
    if int(modified_within_days or 0) > 0:
        q.where(f"todatetime(properties.lastModifiedAt) >= ago({int(modified_within_days)}d)")
        applied.append(f"modified in the last {int(modified_within_days)} days")
    return applied


def _environment_filter_pre(q: Query, environment: str) -> str:
    e = (environment or "").strip()
    if e and GUID.match(e):
        q.where(f"tolower(tostring(properties.environmentId)) == {kql_str(e.lower())}")
        return f"environment id {e}"
    return ""


def _environment_filter_post(q: Query, environment: str) -> str:
    e = (environment or "").strip()
    if e and not GUID.match(e):
        q.where(f"tostring(environmentName) contains {kql_str(e)}")
        return f"environment name contains '{e}'"
    return ""


GroupSpec = list[tuple[str, "str | None"]]  # (column, expression) pairs; expression None = existing column

GROUP_FIELDS_AGENT: dict[str, GroupSpec] = {
    "environment": [("environmentName", None), ("environmentId", "tostring(properties.environmentId)")],
    "environment_type": [("environmentType", None)],
    "owner": [("ownerId", "tolower(tostring(properties.ownerId))")],
    "creator": [("createdBy", "tolower(tostring(properties.createdBy))")],
    "day": [("day", "format_datetime(todatetime(properties.createdAt), 'yyyy-MM-dd')")],
    "month": [("month", "format_datetime(todatetime(properties.createdAt), 'yyyy-MM')")],
    "created_in": [("createdIn", "tostring(properties.createdIn)")],
    "authentication": [("authentication", "tostring(properties.authentication)")],
    "harness": [("harness", "tostring(properties.harness)")],
    "model": [("model", "tostring(properties.model)")],
    "orchestration": [("orchestration", "tostring(properties.orchestration)")],
    "location": [("location", None)],
    "published": [("published", "iff(isnotempty(tostring(properties.lastPublishedAt)), 'published', 'draft only')")],
}


def _group_query(base: Query, group_by: str, count_name: str, fields: dict[str, GroupSpec]) -> Query | None:
    spec = fields.get(group_by)
    if not spec:
        return None
    q = base.copy()
    for col, expr in spec:
        if expr:
            q.extend(col, expr)
    by = [col for col, _ in spec]
    q.summarize_count(count_name, by)
    if group_by in ("day", "month"):
        q.order(by[0], "asc").take(400)
    else:
        q.order(count_name, "desc").take(25)
    return q


async def _with_names(rows: list[dict[str, Any]], keys: tuple[str, ...], resolve: bool) -> str:
    """Add <key>_name for user object ids in rows; returns a note when resolution failed."""
    if not resolve or not rows:
        return ""
    ids = {str(r.get(k)) for r in rows for k in keys if r.get(k)}
    names, error = await resolve_users(ids)
    for r in rows:
        for k in keys:
            v = str(r.get(k) or "").lower()
            if v == ZERO_GUID:
                r[k + "_name"] = "(system / no owner)"
            elif v in names:
                n = names[v]
                r[k + "_name"] = f"{n['name']} <{n['upn']}>" if n.get("upn") else n.get("name")
    return f"Owner names could not be resolved: {error}" if error else ""


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
AGENT_ROW = [
    "agent_id = name", "name = tostring(properties.displayName)", "environment = environmentName", "environment_id = tostring(properties.environmentId)",
    "environment_type = environmentType", "created = todatetime(properties.createdAt)", "created_by = tostring(properties.createdBy)",
    "owner = tostring(properties.ownerId)", "last_published = todatetime(properties.lastPublishedAt)", "last_used = lastUsed",
    "created_in = tostring(properties.createdIn)",
]
AGENT_DETAIL = [
    "harness = tostring(properties.harness)", "authentication = tostring(properties.authentication)", "orchestration = tostring(properties.orchestration)",
    "model = tostring(properties.model)", "channels = properties.channels", "shared_with_entire_tenant = tobool(properties.sharedWithViewers.entireTenant)",
    "viewer_users = toint(properties.sharedWithViewers.userCount)", "viewer_groups = toint(properties.sharedWithViewers.groupCount)",
    "editor_users = toint(properties.sharedWithEditors.userCount)", "connectors = toint(properties.capabilitiesCounts.distinctPowerPlatformConnectors)",
    "quarantined = tobool(properties.isQuarantined)", "managed_solution = tobool(properties.isManaged)", "schema_name = tostring(properties.schemaName)",
    "entra_agent_id = tostring(properties.entraAgentId)", "region = location",
]
AGENT_STATS = ("| summarize total = count(), published = countif(isnotempty(tostring(properties.lastPublishedAt))), "
               "used_last_30_days = countif(lastUsed >= ago(30d)), shared_with_entire_tenant = countif(coalesce(tobool(properties.sharedWithViewers.entireTenant), false)), "
               "no_authentication = countif(tostring(properties.authentication) =~ 'None'), quarantined = countif(coalesce(tobool(properties.isQuarantined), false)), "
               "agent_builder = countif(tostring(properties.createdIn) contains 'Lite' or tostring(properties.createdIn) contains 'Agent Builder'), "
               "system_created = countif(tostring(properties.createdBy) == '00000000-0000-0000-0000-000000000000'), environments = dcount(tostring(properties.environmentId)), "
               "creators = dcount(tostring(properties.createdBy))")


def register(mcp: MCPServer) -> None:  # noqa: C901
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Copilot Studio agents across the WHOLE TENANT in ONE call (Power Platform inventory, includes Agent Builder agents). Use for any "
        "tenant-wide agent question: how many / which agents were created in the last N days, by whom, in which environments, published or draft, "
        "not used for N days, shared with the entire tenant, no authentication. Returns exact totals, a summary, optional group_by counts and agent rows "
        "with environment and owner names. Never loop pp_copilot_studio_agents over environments for these questions."))
    async def pp_agents_tenant(
        created_within_days: Annotated[int, Field(description="Only agents created in the last N days (0 = any time)")] = 0,
        created_after: Annotated[str, Field(description="Only agents created on or after this date, e.g. 2026-10-22")] = "",
        created_before: Annotated[str, Field(description="Only agents created before this date")] = "",
        environment: Annotated[str, Field(description="Environment id, or text contained in the environment name")] = "",
        name_contains: Annotated[str, Field(description="Text contained in the agent name")] = "",
        owner: Annotated[str, Field(description="Owner or creator: object id, UPN / email, or display name")] = "",
        published: Annotated[str, Field(description="any | yes (published at least once) | no (draft only)")] = "any",
        not_used_within_days: Annotated[int, Field(description="Only agents with no recorded usage in the last N days (0 = ignore)")] = 0,
        shared_with_entire_tenant: Annotated[str, Field(description="any | yes | no")] = "any",
        authentication: Annotated[str, Field(description="any | None | Microsoft Entra | Generic OAuth 2.0")] = "any",
        created_in: Annotated[str, Field(description="any | Copilot Studio | Agent Builder (recorded as Copilot Studio Lite)")] = "any",
        quarantined: Annotated[str, Field(description="any | yes | no")] = "any",
        group_by: Annotated[str, Field(description="none | environment | environment_type | owner | creator | day | month | created_in | authentication | published | harness | model | orchestration | location")] = "none",
        top: Annotated[int, Field(description="Agent rows to return (0-500). Totals and summary always cover every match")] = 50,
        include_details: Annotated[bool, Field(description="Add configuration fields: authentication, orchestration, model, channels, sharing, connectors, quarantine")] = False,
        resolve_owner_names: Annotated[bool, Field(description="Look up owner and creator names in Entra")] = True,
        skip_token: Annotated[str, Field(description="next_skip_token from a previous call, for the next page of rows")] = "",
    ) -> dict[str, Any]:
        try:
            pub, shared, quar = _tri(published, "published"), _tri(shared_with_entire_tenant, "shared_with_entire_tenant"), _tri(quarantined, "quarantined")
            owner_id, owner_err = await owner_to_id(owner)
            if owner_err:
                return owner_err
            base = Query().where(f"type == '{AGENT}'")
            applied = _date_filters(base, created_within_days, created_after, created_before)
            if x := _environment_filter_pre(base, environment):
                applied.append(x)
            if name_contains.strip():
                base.where(f"tostring(properties.displayName) contains {kql_str(name_contains.strip())}")
                applied.append(f"name contains '{name_contains.strip()}'")
            if owner_id:
                base.where(f"tolower(tostring(properties.ownerId)) == '{owner_id}' or tolower(tostring(properties.createdBy)) == '{owner_id}'")
                applied.append(f"owned or created by {owner}")
            if pub != "any":
                base.where(f"{'isnotempty' if pub == 'yes' else 'isempty'}(tostring(properties.lastPublishedAt))")
                applied.append("published" if pub == "yes" else "draft only (never published)")
            if shared != "any":
                base.where(f"coalesce(tobool(properties.sharedWithViewers.entireTenant), false) == {'true' if shared == 'yes' else 'false'}")
                applied.append(f"shared with entire tenant: {shared}")
            if authentication.strip().lower() not in ("", "any"):
                base.where(f"tostring(properties.authentication) =~ {kql_str(authentication.strip())}")
                applied.append(f"authentication = {authentication.strip()}")
            ci = created_in.strip().lower()
            if ci not in ("", "any"):
                base.where(BUILDER_EXPR if ("builder" in ci or "lite" in ci) else f"not({BUILDER_EXPR})")
                applied.append(f"created in {created_in.strip()}")
            if quar != "any":
                base.where(f"coalesce(tobool(properties.isQuarantined), false) == {'true' if quar == 'yes' else 'false'}")
                applied.append(f"quarantined: {quar}")
            base.join_environments()
            if x := _environment_filter_post(base, environment):
                applied.append(x)
            base.join_usage()
            if int(not_used_within_days or 0) > 0:
                base.where(f"isnull(lastUsed) or lastUsed < ago({int(not_used_within_days)}d)")
                applied.append(f"no recorded usage in the last {int(not_used_within_days)} days")
        except ValueError as e:
            return err(str(e))

        gb = (group_by or "none").strip().lower()
        if gb not in ("", "none") and gb not in GROUP_FIELDS_AGENT:
            return err(f"group_by must be one of: none, {', '.join(GROUP_FIELDS_AGENT)}")
        n_rows = max(0, min(int(top), 500))
        rows_q = base.copy().project(*(AGENT_ROW + (AGENT_DETAIL if include_details else []))).order("created", "desc")
        stats_q = base.copy().raw(AGENT_STATS)
        group_q = _group_query(base, gb, "agents", GROUP_FIELDS_AGENT) if gb not in ("", "none") else None

        tasks = [run_query(rows_q, max(1, n_rows), skip_token), run_query(stats_q, 1)]
        if group_q:
            tasks.append(run_query(group_q, 400))
        results = await asyncio.gather(*tasks)
        rows_r, stats_r = results[0], results[1]
        group_r = results[2] if group_q else None
        if not rows_r.get("ok"):
            return rows_r

        rows = [_clean(r) for r in rows_r.get("rows", [])] if n_rows else []
        notes = list(NOTES_INVENTORY) + [
            "Classic (Power Virtual Agents V1) bots are not in the inventory; use pp_copilot_studio_agents for one environment or pp_query_all_environments with dataverse_path=/bots.",
            "Configuration fields describe the last published version of each agent.",
        ]
        groups = None
        if group_r is not None:
            if group_r.get("ok"):
                groups = [_clean(g) for g in group_r.get("rows", [])]
                if gb in ("owner", "creator"):
                    note = await _with_names(groups, ("ownerId" if gb == "owner" else "createdBy",), resolve_owner_names)
                    if note:
                        notes.append(note)
            else:
                groups = {"error": group_r.get("attempts")}
        note = await _with_names(rows, ("created_by", "owner"), resolve_owner_names)
        if note:
            notes.append(note)
        rows, trimmed = _fit(rows)
        total = rows_r.get("total")
        summary = _clean((stats_r.get("rows") or [{}])[0]) if stats_r.get("ok") else {"total": total, "note": "detailed summary needs Azure Resource Graph"}
        if total is not None and n_rows and total > len(rows):
            notes.append(f"Showing {len(rows)} of {total} matching agents, newest first. Pass next_skip_token for more, or use group_by for counts.")
        if trimmed:
            notes.append(f"{trimmed} rows were left out to keep the answer readable; lower top or narrow the filters.")
        return {"ok": True, "scope": "whole tenant", "source": rows_r.get("source"), "filters": applied or ["none (all agents)"],
                "total_matching": total, "summary": summary, **({"group_by": gb, "groups": groups} if groups is not None else {}),
                "returned": len(rows), "agents": rows, "next_skip_token": rows_r.get("skip_token") or "", "kql": rows_q.kql(), "notes": notes}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Tenant-wide Power Platform inventory in ONE call: apps (canvas, model-driven, code, managed), flows (cloud, agent, workflow), agents, Power Pages "
        "sites, environments, environment groups and connectors. Filter by created / modified date, environment, name, owner, connector used, "
        "no usage for N days, quarantine; group_by type, environment, owner, day, month or connector. Exact totals plus rows with environment and owner names."))
    async def pp_inventory(
        resource_type: Annotated[str, Field(description="all | agents | apps | canvas_apps | model_driven_apps | code_apps | managed_apps | flows | cloud_flows | agent_flows | workflows | power_pages | environments | environment_groups | connectors (comma separated allowed)")] = "all",
        created_within_days: Annotated[int, Field(description="Only items created in the last N days (0 = any time)")] = 0,
        created_after: Annotated[str, Field(description="Created on or after this date, e.g. 2026-10-01")] = "",
        created_before: Annotated[str, Field(description="Created before this date")] = "",
        modified_within_days: Annotated[int, Field(description="Only items modified in the last N days (0 = ignore)")] = 0,
        environment: Annotated[str, Field(description="Environment id, or text contained in the environment name")] = "",
        name_contains: Annotated[str, Field(description="Text contained in the display name")] = "",
        owner: Annotated[str, Field(description="Owner or creator: object id, UPN / email, or display name")] = "",
        connector: Annotated[str, Field(description="Only items using this connector, e.g. shared_sharepointonline or sharepoint")] = "",
        not_used_within_days: Annotated[int, Field(description="Only items with no recorded usage in the last N days (0 = ignore)")] = 0,
        quarantined: Annotated[str, Field(description="any | yes | no")] = "any",
        group_by: Annotated[str, Field(description="none | type | environment | environment_type | owner | creator | day | month | location | connector")] = "none",
        top: Annotated[int, Field(description="Rows to return (0-500). Totals always cover every match")] = 50,
        resolve_owner_names: Annotated[bool, Field(description="Look up owner and creator names in Entra")] = True,
        skip_token: Annotated[str, Field(description="next_skip_token from a previous call")] = "",
    ) -> dict[str, Any]:
        wanted: list[str] = []
        for part in re.split(r"[,;\s]+", (resource_type or "all").strip().lower()):
            if not part:
                continue
            key = part.replace("-", "_")
            if key not in TYPE_GROUPS and key.rstrip("s") + "s" in TYPE_GROUPS:
                key = key.rstrip("s") + "s"
            if key not in TYPE_GROUPS:
                return err(f"Unknown resource_type '{part}'. Use one of: {', '.join(TYPE_GROUPS)}")
            wanted += [t for t in TYPE_GROUPS[key] if t not in wanted]
        is_env = set(wanted) <= {"microsoft.powerplatform/environments", "microsoft.powerplatform/environmentgroups"}
        is_conn = wanted == ["microsoft.powerplatformconnector/connectors"]
        try:
            quar = _tri(quarantined, "quarantined")
            owner_id, owner_err = await owner_to_id(owner)
            if owner_err:
                return owner_err
            base = Query().where("type in~ (" + ", ".join(f"'{t}'" for t in wanted) + ")")
            applied = [f"types: {', '.join(TYPE_NAMES.get(t, t) for t in wanted)}"]
            if not is_conn:
                applied += _date_filters(base, created_within_days, created_after, created_before, modified_within_days)
            if name_contains.strip():
                base.where(f"tostring(properties.displayName) contains {kql_str(name_contains.strip())}")
                applied.append(f"name contains '{name_contains.strip()}'")
            if is_env:
                e = environment.strip()
                if e:
                    base.where(f"tolower(name) == {kql_str(e.lower())}" if GUID.match(e) else f"tostring(properties.displayName) contains {kql_str(e)}")
                    applied.append(f"environment '{e}'")
            elif not is_conn:
                if x := _environment_filter_pre(base, environment):
                    applied.append(x)
            if owner_id:
                base.where(f"tolower(tostring(properties.ownerId)) == '{owner_id}' or tolower(tostring(properties.createdBy)) == '{owner_id}'")
                applied.append(f"owned or created by {owner}")
            if connector.strip():
                base.where(f"tostring(properties.powerPlatformConnectors) contains {kql_str(connector.strip())}")
                applied.append(f"uses connector matching '{connector.strip()}'")
            if quar != "any":
                base.where(f"coalesce(tobool(properties.isQuarantined), false) == {'true' if quar == 'yes' else 'false'}")
                applied.append(f"quarantined: {quar}")
            if not is_env and not is_conn:
                base.join_environments()
                if x := _environment_filter_post(base, environment):
                    applied.append(x)
                base.join_usage()
                if int(not_used_within_days or 0) > 0:
                    base.where(f"isnull(lastUsed) or lastUsed < ago({int(not_used_within_days)}d)")
                    applied.append(f"no recorded usage in the last {int(not_used_within_days)} days")
        except ValueError as e:
            return err(str(e))

        if is_env:
            fields = ["id = name", "type", "name = tostring(properties.displayName)", "environment_type = tostring(properties.environmentType)",
                      "managed = tobool(properties.isManaged)", "environment_group = tostring(properties.environmentGroup)",
                      "created = todatetime(properties.createdAt)", "created_by = tostring(properties.createdBy)",
                      "last_modified = todatetime(properties.lastModifiedAt)", "region = location", "description = tostring(properties.description)"]
            order_field, name_keys = "created", ("created_by",)
        elif is_conn:
            fields = ["connector_id = tostring(properties.connectorId)", "name = tostring(properties.displayName)", "publisher = tostring(properties.publisher)",
                      "tier = tostring(properties.tier)", "release = tostring(properties.releaseTag)", "deprecated = tobool(properties.isDeprecated)",
                      "operations = array_length(properties.operations)"]
            order_field, name_keys = "name", ()
        else:
            fields = ["id = name", "type", "name = tostring(properties.displayName)", "environment = environmentName", "environment_id = tostring(properties.environmentId)",
                      "environment_type = environmentType", "created = todatetime(properties.createdAt)", "created_by = tostring(properties.createdBy)",
                      "owner = tostring(properties.ownerId)", "last_modified = todatetime(properties.lastModifiedAt)", "last_used = lastUsed",
                      "sub_type = tostring(properties.subType)", "trigger = tostring(properties.trigger)", "quarantined = tobool(properties.isQuarantined)",
                      "connectors = array_length(properties.powerPlatformConnectors)", "region = location"]
            order_field, name_keys = "created", ("created_by", "owner")

        group_fields: dict[str, GroupSpec] = {
            "type": [("type", None)],
            "location": [("location", None)],
            "day": [("day", "format_datetime(todatetime(properties.createdAt), 'yyyy-MM-dd')")],
            "month": [("month", "format_datetime(todatetime(properties.createdAt), 'yyyy-MM')")],
            "creator": [("createdBy", "tolower(tostring(properties.createdBy))")],
        }
        if is_env:
            group_fields["environment_type"] = [("environmentType", "tostring(properties.environmentType)")]
        elif is_conn:
            group_fields = {"publisher": [("publisher", "tostring(properties.publisher)")], "tier": [("tier", "tostring(properties.tier)")]}
        else:
            group_fields.update({"environment": [("environmentName", None), ("environmentId", "tostring(properties.environmentId)")],
                                 "environment_type": [("environmentType", None)],
                                 "owner": [("ownerId", "tolower(tostring(properties.ownerId))")]})
        gb = (group_by or "none").strip().lower()
        if gb not in ("", "none", "connector") and gb not in group_fields:
            return err(f"group_by for these types must be one of: none, {', '.join(group_fields)}" + ("" if is_env or is_conn else ", connector"))
        if gb == "connector" and (is_env or is_conn):
            return err("group_by=connector applies to apps, flows and agents")

        n_rows = max(0, min(int(top), 500))
        rows_q = base.copy().project(*fields).order(order_field, "desc" if order_field == "created" else "asc")
        type_q = base.copy().summarize_count("items", ["type"]).order("items", "desc")
        group_q: Query | None = None
        if gb == "connector":
            group_q = base.copy().raw("| mv-expand c = properties.powerPlatformConnectors | extend connector = tostring(c.connectorId) "
                                      "| where isnotempty(connector) | summarize resources = dcount(name) by connector | order by resources desc | take 25")
        elif gb not in ("", "none"):
            group_q = _group_query(base, gb, "items", group_fields)

        tasks = [run_query(rows_q, max(1, n_rows), skip_token), run_query(type_q, 50)]
        if group_q:
            tasks.append(run_query(group_q, 400))
        results = await asyncio.gather(*tasks)
        rows_r, type_r = results[0], results[1]
        group_r = results[2] if group_q else None
        if not rows_r.get("ok"):
            return rows_r
        rows = [_clean(r) for r in rows_r.get("rows", [])] if n_rows else []
        for r in rows:
            if r.get("type"):
                r["type"] = TYPE_NAMES.get(str(r["type"]).lower(), r["type"])
        notes = list(NOTES_INVENTORY)
        by_type = {TYPE_NAMES.get(str(t.get("type")).lower(), t.get("type")): t.get("items") for t in type_r.get("rows", [])} if type_r.get("ok") else None
        groups = None
        if group_r is not None:
            if group_r.get("ok"):
                groups = [_clean(g) for g in group_r.get("rows", [])]
                for g in groups:
                    if g.get("type"):
                        g["type"] = TYPE_NAMES.get(str(g["type"]).lower(), g["type"])
                if gb in ("owner", "creator"):
                    note = await _with_names(groups, ("ownerId" if gb == "owner" else "createdBy",), resolve_owner_names)
                    if note:
                        notes.append(note)
            else:
                groups = {"error": group_r.get("attempts"), "note": "group_by=connector needs Azure Resource Graph" if gb == "connector" else ""}
        note = await _with_names(rows, name_keys, resolve_owner_names)
        if note:
            notes.append(note)
        rows, trimmed = _fit(rows)
        total = rows_r.get("total")
        if total is not None and n_rows and total > len(rows):
            notes.append(f"Showing {len(rows)} of {total} matching items. Pass next_skip_token for more, or use group_by for counts.")
        if trimmed:
            notes.append(f"{trimmed} rows were left out to keep the answer readable; lower top or narrow the filters.")
        return {"ok": True, "scope": "whole tenant", "source": rows_r.get("source"), "filters": applied, "total_matching": total,
                "by_type": by_type, **({"group_by": gb, "groups": groups} if groups is not None else {}),
                "returned": len(rows), "items": rows, "next_skip_token": rows_r.get("skip_token") or "", "kql": rows_q.kql(), "notes": notes}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Run your own KQL on Azure Resource Graph, tenant wide. Table PowerPlatformResources holds every agent, app, flow, environment and connector: "
        "type values microsoft.copilotstudio/agents, microsoft.powerapps/canvasapps, microsoft.powerautomate/cloudflows, microsoft.powerplatform/environments, "
        "microsoft.powerplatformusage/usagerecords (properties.lastUsed); fields under properties: displayName, createdAt, createdBy, ownerId, environmentId, "
        "lastPublishedAt, lastModifiedAt, powerPlatformConnectors. Azure tables (resources, resourcecontainers, advisorresources, policyresources) work too. "
        "Use when pp_agents_tenant and pp_inventory cannot express the question."))
    async def resource_graph_query(
        kql: Annotated[str, Field(description="KQL, e.g. PowerPlatformResources | where type == 'microsoft.copilotstudio/agents' | summarize count() by tostring(properties.createdIn)")],
        top: Annotated[int, Field(description="Rows per page (1-1000)")] = 100,
        skip_token: Annotated[str, Field(description="next_skip_token from a previous call")] = "",
        subscription_ids: Annotated[str, Field(description="Optional comma-separated subscription ids for Azure tables; default all you can see")] = "",
    ) -> dict[str, Any]:
        text = (kql or "").strip()
        if not text:
            return err("kql is required")
        if text.startswith("|"):
            text = TABLE + "\n" + text
        options: dict[str, Any] = {"resultFormat": "objectArray", "$top": max(1, min(int(top), 1000))}
        _, token = _split_token(skip_token)
        if token:
            options["$skipToken"] = token
        body: dict[str, Any] = {"query": text, "options": options}
        subs = [s.strip() for s in (subscription_ids or "").split(",") if s.strip()]
        if subs:
            body["subscriptions"] = subs
        r = await runbook.call("arm", "POST", "/providers/Microsoft.ResourceGraph/resources", body=body, api_version=ARG_API, max_chars=8_000_000)
        if not r.get("ok"):
            r.setdefault("hints", []).extend([
                "Cast property values: tostring(properties.displayName), todatetime(properties.createdAt), tobool(...).",
                "Schema: https://learn.microsoft.com/power-platform/admin/inventory-schema and https://learn.microsoft.com/microsoft-copilot-studio/admin-agent-inventory",
                ROLE_HINT])
            r["kql"] = text
            return r
        b = r.get("body") or {}
        rows, trimmed = _fit(b.get("data") or [])
        nxt = b.get("$skipToken") or b.get("skipToken")
        out: dict[str, Any] = {"ok": True, "total": b.get("totalRecords"), "returned": len(rows), "rows": rows,
                               "next_skip_token": f"arg:{nxt}" if nxt else "", "kql": text}
        if trimmed:
            out["note"] = f"{trimmed} rows were left out to keep the answer readable; project fewer columns or summarize."
        return out
