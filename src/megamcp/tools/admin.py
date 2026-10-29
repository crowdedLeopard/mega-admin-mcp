"""Power Platform and Copilot Studio administration tools."""
from __future__ import annotations

import re
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..config import settings
from ..runbook.invoke import RunbookError, runbook
from .common import READ, WRITE, err

GUID = re.compile(r"^[0-9a-fA-F-]{36}$")


async def resolve_environment(environment: str) -> dict[str, Any]:
    """Accept an environment id, display name or Dataverse URL and return the environment record."""
    environment = (environment or "").strip().rstrip("/")
    envs = await runbook.paged("powerplatform", "/environmentmanagement/environments", api_version=settings.powerplatform_api_version, max_pages=20)
    if not environment:
        raise RunbookError({"status": 0, "error": "missing_environment", "message": "environment is required (id, display name or URL)"})
    low = environment.lower()
    for e in envs:
        if e.get("id", "").lower() == low or (e.get("url") or "").lower().rstrip("/") == low or (e.get("displayName") or "").lower() == low:
            return e
    partial = [e for e in envs if low in (e.get("displayName") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    raise RunbookError({"status": 404, "error": "environment_not_found", "message": f"No environment matched '{environment}'",
                        "candidates": [{"id": e.get("id"), "displayName": e.get("displayName"), "url": e.get("url")} for e in partial[:10]]})


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "List Power Platform environments in the tenant (id, name, type, region, Dataverse URL, state, admin mode, created by). "
        "Optional text filter on the display name. Use the id or URL with the other tools."))
    async def pp_environments(
        filter_text: Annotated[str, Field(description="Case-insensitive text to match in the environment display name")] = "",
        top: Annotated[int, Field(description="Maximum environments to return (1-500)")] = 100,
    ) -> dict[str, Any]:
        try:
            envs = await runbook.paged("powerplatform", "/environmentmanagement/environments", api_version=settings.powerplatform_api_version, max_pages=30)
        except RunbookError as e:
            return e.result
        if filter_text:
            envs = [e for e in envs if filter_text.lower() in (e.get("displayName") or "").lower()]
        rows = [{
            "id": e.get("id"), "displayName": e.get("displayName"), "type": e.get("type"), "geo": e.get("geo"), "azureRegion": e.get("azureRegion"),
            "url": e.get("url"), "state": e.get("state"), "adminMode": e.get("adminMode"), "version": e.get("version"),
            "createdDateTime": e.get("createdDateTime"), "createdBy": (e.get("createdBy") or {}).get("id"),
        } for e in envs]
        return {"ok": True, "count": len(rows), "returned": min(len(rows), top), "environments": rows[: max(1, min(int(top), 500))]}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "List Copilot Studio agents (bots) in ONE environment with owner, authentication mode, published date and state, read from Dataverse. "
        "environment can be an environment id, display name or Dataverse URL. For questions about agents across the tenant use pp_agents_tenant instead."))
    async def pp_copilot_studio_agents(
        environment: Annotated[str, Field(description="Environment id, display name or https://org.crm.dynamics.com URL")],
        top: Annotated[int, Field(description="Maximum agents to return (1-500)")] = 100,
    ) -> dict[str, Any]:
        try:
            env = await resolve_environment(environment)
            url = env.get("url")
            if not url:
                return err("Environment has no Dataverse database, so it cannot host Copilot Studio agents", environment=env.get("displayName"))
            r = await runbook.call(
                "dataverse", "GET", "/bots",
                query={"$select": "name,botid,schemaname,publishedon,authenticationmode,statecode,createdon,modifiedon,_ownerid_value,language,configuration",
                       "$top": max(1, min(int(top), 500)), "$orderby": "modifiedon desc"},
                dataverse_org_url=url, raise_on_error=True, max_chars=400_000)
        except RunbookError as e:
            return e.result
        body = r.get("body") or {}
        auth_modes = {1: "No authentication", 2: "Authenticate with Microsoft (Entra ID)", 3: "Manual (any identity provider)", 4: "Authenticate with Microsoft (Entra ID)"}
        agents = []
        for b in body.get("value", []):
            cfg = b.get("configuration") or ""
            agents.append({
                "name": b.get("name"), "botid": b.get("botid"), "schemaname": b.get("schemaname"),
                "owner": b.get("_ownerid_value@OData.Community.Display.V1.FormattedValue") or b.get("_ownerid_value"),
                "authentication": auth_modes.get(b.get("authenticationmode"), b.get("authenticationmode")),
                "state": "Active" if b.get("statecode") == 0 else "Inactive", "publishedon": b.get("publishedon"),
                "createdon": b.get("createdon"), "modifiedon": b.get("modifiedon"), "language": b.get("language"),
                "generative_orchestration": ("\"isGenerativeOrchestrationEnabled\":true" in cfg) if isinstance(cfg, str) else None,
            })
        return {"ok": True, "environment": {"id": env.get("id"), "displayName": env.get("displayName"), "url": url}, "count": len(agents), "agents": agents}

    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "Administrative action on a Copilot Studio agent via the Power Platform API: quarantine (blocks the agent), unquarantine, "
        "reassign ownership (new_owner_object_id required) or delete. Returns a preview unless confirm=true."))
    async def pp_agent_admin_action(
        environment_id: Annotated[str, Field(description="Environment id (GUID)")],
        bot_id: Annotated[str, Field(description="Agent (bot) id GUID from pp_copilot_studio_agents")],
        action: Annotated[str, Field(description="quarantine | unquarantine | reassign | delete | status")],
        new_owner_object_id: Annotated[str, Field(description="Entra object id of the new owner (reassign only)")] = "",
        confirm: Annotated[bool, Field(description="Set true to execute after the admin approved the preview")] = False,
    ) -> dict[str, Any]:
        base = f"/copilotstudio/environments/{environment_id}/bots/{bot_id}/api"
        a = action.lower().strip()
        if a == "status":
            return await runbook.call("powerplatform", "GET", f"{base}/botQuarantine", api_version=settings.powerplatform_api_version)
        if a == "quarantine":
            return await runbook.call("powerplatform", "POST", f"{base}/botQuarantine/SetAsQuarantined", api_version=settings.powerplatform_api_version, confirm=confirm, body={})
        if a == "unquarantine":
            return await runbook.call("powerplatform", "POST", f"{base}/botQuarantine/SetAsUnquarantined", api_version=settings.powerplatform_api_version, confirm=confirm, body={})
        if a == "reassign":
            if not new_owner_object_id:
                return err("new_owner_object_id is required for reassign")
            return await runbook.call("powerplatform", "POST", f"{base}/botAdminOperations/reassign", api_version=settings.powerplatform_api_version, confirm=confirm,
                                      body={"newOwnerObjectId": new_owner_object_id})
        if a == "delete":
            return await runbook.call("powerplatform", "DELETE", f"{base}/botAdminOperations", api_version=settings.powerplatform_api_version, confirm=confirm)
        return err("action must be one of: status, quarantine, unquarantine, reassign, delete")

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Describe a Dataverse table (entity) in an environment: logical name, primary key, columns with types. "
        "Use it before querying tables with invoke service=dataverse."))
    async def dataverse_describe_table(
        dataverse_org_url: Annotated[str, Field(description="Environment URL, e.g. https://org.crm.dynamics.com")],
        table_logical_name: Annotated[str, Field(description="Logical name, e.g. bot, systemuser, workflow, solution")],
        max_columns: Annotated[int, Field(description="Maximum columns to list (10-500)")] = 120,
    ) -> dict[str, Any]:
        name = table_logical_name.strip().lower()
        if name.endswith("s") and name not in ("bots",):
            pass
        try:
            r = await runbook.call(
                "dataverse", "GET", f"/EntityDefinitions(LogicalName='{name}')",
                query={"$select": "LogicalName,EntitySetName,PrimaryIdAttribute,PrimaryNameAttribute,DisplayName,Description,IsCustomEntity,OwnershipType",
                       "$expand": "Attributes($select=LogicalName,AttributeType,DisplayName,IsPrimaryId,IsPrimaryName,IsValidForRead,IsCustomAttribute)"},
                dataverse_org_url=dataverse_org_url, raise_on_error=True, max_chars=800_000)
        except RunbookError as e:
            return e.result
        b = r.get("body") or {}

        def label(x: Any) -> str:
            try:
                return x["UserLocalizedLabel"]["Label"]
            except Exception:  # noqa: BLE001
                return ""

        cols = [{"name": a.get("LogicalName"), "type": a.get("AttributeType"), "label": label(a.get("DisplayName")),
                 "primary_id": a.get("IsPrimaryId"), "primary_name": a.get("IsPrimaryName")} for a in b.get("Attributes", []) if a.get("IsValidForRead")]
        return {"ok": True, "table": b.get("LogicalName"), "entity_set": b.get("EntitySetName"), "label": label(b.get("DisplayName")),
                "description": label(b.get("Description")), "primary_id": b.get("PrimaryIdAttribute"), "primary_name": b.get("PrimaryNameAttribute"),
                "ownership": b.get("OwnershipType"), "custom": b.get("IsCustomEntity"), "column_count": len(cols),
                "columns": cols[: max(10, min(int(max_columns), 500))],
                "query_example": {"tool": "invoke", "service": "dataverse", "method": "GET", "path": f"/{b.get('EntitySetName')}",
                                  "query_json": "{\"$select\": \"" + ",".join(c["name"] for c in cols[:5] if c["name"]) + "\", \"$top\": 50}",
                                  "dataverse_org_url": dataverse_org_url}}
